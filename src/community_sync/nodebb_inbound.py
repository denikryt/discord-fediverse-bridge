"""Trusted NodeBB Group distribution admission before canonical Discord writes.

The signed Announce authorizes Group distribution, not a purported Person
signature. Network dereferences are bounded by the existing secure NodeBB
HTTP adapter; claimed note authors are pinned to immutable canonical rows.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..activitypub_models import ActivityPubEvent, RemoteGroupAnnounceEvent
from ..inbound_activity_outcomes import InboundActivityOutcome
from ..nodebb_discovery import AP_ACCEPT, NodeBBDiscoveryError, SecureNodeBBJSON, _origin
from .inbound_mapping import get_accepted_subscriptions
from .nodebb_backfill import _uri, fetch_verified_historical_root

MAX_PARENT_DEPTH = 5


def _addressed_to_group(note: dict[str, Any], group_id: str) -> bool:
    """Require both declared Group audience and effective delivery addressing."""
    if _uri(note.get("audience")) != group_id:
        return False
    return group_id in [
        _uri(value) for field in ("to", "cc")
        for value in (note.get(field) if isinstance(note.get(field), list) else [note.get(field)])
    ]


def _confirmed_note(note: dict[str, Any], object_id: str, group_id: str,
                    context: str | None = None) -> bool:
    """Validate immutable object identity, claimed owner and Group context."""
    if not isinstance(note, dict) or note.get("type") not in {"Note", "Article", "Page"}:
        return False
    if note.get("id") != object_id or not _addressed_to_group(note, group_id):
        return False
    try:
        _origin(object_id)
        _origin(_uri(note.get("attributedTo")) or "")
        topic = _uri(note.get("context"))
        if not topic or (context is not None and topic != context):
            return False
        _origin(topic)
    except (ValueError, NodeBBDiscoveryError):
        return False
    return True


def _normalized_event(
    note: dict[str, Any], group_id: str, announce_id: str,
    *, root_id: str | None = None, parent_id: str | None = None,
    source: dict[str, Any] | None = None, source_activity_id: str | None = None,
) -> ActivityPubEvent:
    """Map a checked Note to the established Python community-sync contract."""
    owner = _uri(note["attributedTo"])
    assert owner is not None  # Validated by _confirmed_note before use.
    ap_id = note["id"]
    posted = note.get("published") if isinstance(note.get("published"), str) else datetime.now(timezone.utc).isoformat()
    markdown_source = note.get("source")
    content = (markdown_source.get("content") if isinstance(markdown_source, dict) else None)
    if not isinstance(content, str):
        content = note.get("content") if isinstance(note.get("content"), str) else None
    return ActivityPubEvent.model_validate({
        "event_type": "comment.created" if root_id else "post.created",
        "delivery_id": announce_id,
        "occurred_at": posted,
        "community_actor_id": group_id,
        "actor_id": owner,
        "source_activity_json": source,
        "source_activity_id": source_activity_id or announce_id,
        "source_announce_id": announce_id if source else None,
        "object": {
            "ap_id": ap_id, "kind": "comment" if root_id else "post",
            "lemmy_id": 0, "post_lemmy_id": 0 if root_id else None,
            "post_ap_id": root_id, "parent_ap_id": parent_id,
            "attributed_to_actor_id": owner, "author_name": owner.rstrip("/").split("/")[-1],
            "title": note.get("name") if isinstance(note.get("name"), str) else None,
            "body_markdown": content, "url": ap_id, "published_at": posted,
        },
    })


async def handle_nodebb_announcement(event: RemoteGroupAnnounceEvent, runtime: object):
    """Admit one verified Group Announce then call the existing canonical runtime.

    Missing historical membership and parent surfaces defer without creating
    a thread from unverified context. No external network request runs before
    the Group subscription/profile and policy have been checked.
    """
    from ..activitypub_handlers import HandlerResult
    from ..nodebb_discovery import NodeBBDiscoveryError

    def deny(reason: str, *, deferred: bool = False) -> HandlerResult:
        """Give a stable, side-effect-free denial or deferred outcome."""
        return HandlerResult(
            "deferred" if deferred else "skipped",
            InboundActivityOutcome.DEFERRED_MISSING_DEPENDENCY if deferred
            else InboundActivityOutcome.IGNORED_UNMAPPED_CONTEXT,
            reason,
        )

    db = runtime.database
    group_id = event.community_actor_id
    follow = db.bridge_actor_follows.get_bridge_actor_follow(group_id)
    if (follow is None or follow.status != "accepted"
            or follow.cancel_requested or follow.remote_software_profile != "nodebb"
            or not follow.community_inbox_url or not follow.follow_activity_id
            or not get_accepted_subscriptions(db, group_id)):
        return deny("verified accepted NodeBB Follow required")

    try:
        group_origin = _origin(group_id)
        group_fetch = SecureNodeBBJSON(group_origin, runtime.bridge_policy_service)
        actor = await group_fetch(group_id, AP_ACCEPT)
        if (actor.get("type") != "Group" or actor.get("id") != group_id
                or _uri(actor.get("inbox")) != follow.community_inbox_url):
            return deny("Group actor or original inbox changed")
        object_id = event.object.ap_id
        object_fetch = SecureNodeBBJSON(_origin(object_id), runtime.bridge_policy_service)
        note = event.object.inline_object or await object_fetch(object_id, AP_ACCEPT)
        if not _confirmed_note(note, object_id, group_id):
            return deny("remote Group Note attribution, context or audience mismatch")
        # A verified distributor does not override the bridge's independent
        # policy for an author on a different origin.
        if not runtime.bridge_policy_service.federation_decision(
            _uri(note.get("attributedTo"))
        ).allowed:
            return deny("remote Note author denied by federation policy")
        inner = event.source_activity_json.get("object")
        if isinstance(inner, dict) and inner.get("type") == "Create":
            if _uri(inner.get("actor")) != _uri(note.get("attributedTo")):
                return deny("embedded Create author does not match Note attribution")
        parent_id = _uri(note.get("inReplyTo"))
        source_id = event.object.inner_create_id or event.delivery_id
        if parent_id is None:
            post = _normalized_event(note, group_id, event.delivery_id,
                                     source=event.source_activity_json, source_activity_id=source_id)
            return await runtime.community_runtime.handle_inbound_post(post, runtime)

        context = _uri(note.get("context"))
        current = parent_id
        seen = {object_id}
        root_note = None
        # Only authenticated, exactly addressed parent chain objects may
        # determine the root: context alone cannot select a foreign topic.
        for _ in range(MAX_PARENT_DEPTH):
            if current in seen:
                return deny("cyclic NodeBB reply ancestry")
            seen.add(current)
            parent_fetch = SecureNodeBBJSON(_origin(current), runtime.bridge_policy_service)
            parent = await parent_fetch(current, AP_ACCEPT)
            if not _confirmed_note(parent, current, group_id, context):
                return deny("reply ancestor disagrees with verified Group or context")
            if not runtime.bridge_policy_service.federation_decision(
                _uri(parent.get("attributedTo"))
            ).allowed:
                return deny("reply ancestor author denied by federation policy")
            next_id = _uri(parent.get("inReplyTo"))
            if next_id is None:
                root_note = parent
                break
            current = next_id
        if root_note is None:
            return deny("bounded NodeBB reply ancestry exceeded", deferred=True)
        root_id = root_note["id"]
        repository = db.discord_fanout_groups
        existing_root = repository.get_thread_group_by_ap_object(root_id, community_actor_id=group_id)
        if existing_root is None:
            proof = await fetch_verified_historical_root(
                root_id=root_id, group_actor_id=group_id,
                reply_context=context, policy=runtime.bridge_policy_service,
            )
            if (proof is None or not _confirmed_note(proof, root_id, group_id, context)
                    or _uri(proof.get("attributedTo")) != _uri(root_note.get("attributedTo"))):
                return deny("deferred_unsupported_historical_root", deferred=True)
            root = _normalized_event(proof, group_id, root_id,
                                     source_activity_id=root_id)
            await runtime.community_runtime.handle_inbound_post(root, runtime)
            existing_root = repository.get_thread_group_by_ap_object(root_id, community_actor_id=group_id)
            if existing_root is None:
                return deny("verified historical root Discord placement not confirmed", deferred=True)
        reply = _normalized_event(note, group_id, event.delivery_id,
                                  root_id=root_id, parent_id=parent_id,
                                  source=event.source_activity_json, source_activity_id=source_id)
        return await runtime.community_runtime.handle_inbound_comment(reply, runtime)
    except (NodeBBDiscoveryError, OSError, ValueError, KeyError, TypeError):
        return deny("NodeBB Group or parent unavailable for verified bounded fetch", deferred=True)
