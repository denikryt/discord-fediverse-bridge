"""Verified remote Community Group display mutations and atomic wire receipts.

The Group distributor may change only its own Discord-facing projection of an
already verified Create. It does not sign for the Note's Person author. All
canonical revisions and their payload-bound terminal receipts share one SQLite
BEGIN IMMEDIATE transaction; Discord I/O is deliberately outside this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Callable, Awaitable

from sqlalchemy import select

from ..activitypub_models import ActivityPubEvent
from ..inbound_activity_outcomes import InboundActivityOutcome
from ..models import (ActivityPubEventReceipt, BridgeActorFollow,
                      ChannelCommunitySubscription, CommunityThreadGroup,
                      CommunityMessageGroup, utcnow)
from ..nodebb_discovery import AP_ACCEPT, SecureNodeBBJSON, _origin

MUTATIONS = frozenset(('post.updated', 'comment.updated', 'post.deleted', 'comment.deleted'))


def _activity_id(value: object) -> str | None:
    """Accept an ActivityStreams identifier, not an arbitrary display URL."""
    if isinstance(value, str) and value:
        return value
    if isinstance(value, dict) and isinstance(value.get('id'), str):
        return value['id']
    return None


@dataclass(frozen=True, slots=True)
class ProjectionReceipt:
    """One committed revision or a non-mutating signed-delivery outcome."""

    status: str
    outcome: str
    detail: str
    revision: int | None = None


class GroupMutationConflict(ValueError):
    """The same authenticated wire ID was reused for another event payload."""


class GroupMutationDenied(ValueError):
    """No verified Group or owner may modify this immutable Create."""


def _targets(raw: object) -> set[str]:
    """Collect exact Actor URIs from an ActivityStreams address or address list."""
    result: set[str] = set()
    for candidate in raw if isinstance(raw, list) else [raw]:
        actor = _activity_id(candidate)
        if actor:
            result.add(actor)
    return result


def _normalized_payload(event: ActivityPubEvent) -> dict[str, Any]:
    """Bind one authenticated wire ID to semantically relevant canonical data.

    Transport-only JSON-LD context or key ordering must not change replay
    identity. In particular Delete has no meaningful synthetic `published_at`.
    """
    source = event.source_activity_json or {}
    raw_object = source.get('object')
    object_data = raw_object if isinstance(raw_object, dict) else {}
    presentation_keys = ('type', 'id', 'attributedTo', 'inReplyTo', 'name',
                         'content', 'source', 'summary', 'sensitive', 'url',
                         'attachment', 'mediaType', 'published', 'updated')
    presentation = {key: object_data[key] for key in presentation_keys
                    if key in object_data}
    return {
        'wire_id': event.delivery_id,
        'event_type': event.event_type,
        'source_activity_id': event.source_activity_id,
        'source_announce_id': event.source_announce_id,
        'verified_sender': event.verified_sender_actor_id,
        'group': event.community_actor_id,
        'object': event.object.ap_id,
        'kind': event.object.kind,
        'actor': event.actor_id,
        'object_owner': event.object.attributed_to_actor_id,
        'reply_root': event.object.post_ap_id,
        'reply_parent': event.object.parent_ap_id,
        'normalized_body': event.object.body_markdown,
        'normalized_title': event.object.title,
        'activity_actor': _activity_id(source.get('actor')),
        'activity_object': _activity_id(raw_object),
        'activity_time': source.get('updated') or source.get('published'),
        'to': sorted(_targets(source.get('to'))),
        'cc': sorted(_targets(source.get('cc'))),
        'audience': sorted(_targets(source.get('audience'))),
        'object_to': sorted(_targets(object_data.get('to'))),
        'object_cc': sorted(_targets(object_data.get('cc'))),
        'object_audience': sorted(_targets(object_data.get('audience'))),
        'object_content': presentation,
    }


def _fingerprint(event: ActivityPubEvent) -> str:
    """Hash canonicalized validated mutation data without volatile receivedAt."""
    canonical = json.dumps(_normalized_payload(event), sort_keys=True,
                           separators=(',', ':'), ensure_ascii=False)
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()



def _authenticated_update_time(source: dict[str, Any]) -> datetime | None:
    """Read the signed Note.updated instant, or the signed activity timestamp.

    A timezone-free timestamp is not an ActivityPub instant. It must not
    bypass ordering by being silently interpreted as local system time.
    """
    obj = source.get('object')
    value = (obj.get('updated') if isinstance(obj, dict) else None) or source.get('updated')
    if value is None:
        return None
    if not isinstance(value, str):
        raise GroupMutationDenied('Group Update timestamp is not an RFC3339 string')
    try:
        instant = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as exc:
        raise GroupMutationDenied('invalid Group Update timestamp') from exc
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise GroupMutationDenied('Group Update timestamp lacks timezone offset')
    return instant.astimezone(timezone.utc)



def _first_create_verified(group: object, *, kind: str) -> bool:
    """Require retained author, exact object and immutable original Note kind."""
    saved = getattr(group, 'inbound_create_snapshot_json', None)
    if not isinstance(saved, dict):
        return False
    return (getattr(group, 'source_channel_id', None) is None
            and (getattr(group, 'source_thread_id', None) if kind == 'post'
                 else getattr(group, 'source_message_id', None)) is None
            and bool(getattr(group, 'source_owner_actor_id', None))
            and saved.get('ap_id') == getattr(group, 'ap_object_id', None)
            and saved.get('kind') == kind
            and isinstance(getattr(group, 'display_object_json', None), dict))


def _verified_inner(event: ActivityPubEvent, group: object, *, group_signed: bool,
                    expected_parent: str | None) -> bool:
    """Reject changed owner, kind, parent and inconsistent nested activity IDs."""
    source = event.source_activity_json
    operation = 'Update' if event.event_type.endswith('.updated') else 'Delete'
    owner = getattr(group, 'source_owner_actor_id', None)
    if not isinstance(source, dict) or source.get('type') != operation:
        return False
    if not _activity_id(source.get('id')) or _activity_id(source.get('id')) != event.source_activity_id:
        return False
    if _activity_id(source.get('actor')) != event.actor_id:
        return False
    if event.actor_id != owner:
        return False
    if event.object.attributed_to_actor_id not in (None, owner):
        return False
    object_data = source.get('object')
    if _activity_id(object_data) != event.object.ap_id:
        return False
    if operation == 'Update':
        if not isinstance(object_data, dict) or object_data.get('type') not in ('Note', 'Page', 'Article'):
            return False
    elif isinstance(object_data, dict) and object_data.get('type') not in (None, 'Tombstone', 'Note', 'Page', 'Article'):
        return False
    if isinstance(object_data, dict):
        attribution = _activity_id(object_data.get('attributedTo'))
        if attribution is not None and attribution != owner:
            return False
        parent = _activity_id(object_data.get('inReplyTo'))
        if parent is not None and parent != expected_parent:
            return False
        if expected_parent is None and parent is not None:
            return False
        explicit_group = _activity_id(object_data.get('audience'))
        if explicit_group is not None and explicit_group != group.community_actor_id:
            return False
    if (event.object.parent_ap_id is not None
            and event.object.parent_ap_id != expected_parent):
        return False
    # A reply's root and parent are checked against its immutable Group row
    # by the caller; a root cannot gain ancestry during Update/Delete.
    return True


def _group_addresses(source: dict[str, Any]) -> set[str]:
    """Keep direct-owner multi-Group distribution limited to declared targets."""
    object_data = source.get('object')
    return (_targets(source.get('audience')) | _targets(source.get('to'))
            | _targets(source.get('cc'))
            | (_targets(object_data.get('audience')) | _targets(object_data.get('to'))
               | _targets(object_data.get('cc')) if isinstance(object_data, dict) else set()))


class GroupViewMutationService:
    """Verify the distributor at the network boundary, commit receipt and view."""

    def __init__(self, database: object, policy: object,
                 actor_reader: Callable[[str], Awaitable[dict]] | None = None) -> None:
        """A test may inject the secure remote Actor GET edge, never signer proof."""
        self.database = database
        self.policy = policy
        self.actor_reader = actor_reader

    @staticmethod
    def recognizes(event: object, database: object) -> bool:
        """Intercept only mutations of already mapped remote Group content."""
        if not isinstance(event, ActivityPubEvent) or event.event_type not in MUTATIONS:
            return False
        if event.target is not None and event.target.kind == 'personal_feed':
            return False
        group = event.community_actor_id
        if not group:
            return False
        return (database.discord_fanout_groups.get_thread_group_by_ap_object(
            event.object.ap_id, community_actor_id=group) is not None
            or database.discord_fanout_groups.get_message_group_by_ap_object(
                event.object.ap_id, community_actor_id=group) is not None)

    async def _verified_group_actor(self, group: str, inbox: str) -> bool:
        """Require the currently followed NodeBB Group's exact actor and inbox."""
        if self.actor_reader is None:
            record = await SecureNodeBBJSON(_origin(group), self.policy)(group, AP_ACCEPT)
        else:
            record = await self.actor_reader(group)
        return (isinstance(record, dict) and record.get('type') == 'Group'
                and record.get('id') == group
                and _activity_id(record.get('inbox')) == inbox)

    async def apply(self, event: ActivityPubEvent) -> ProjectionReceipt:
        """Validate all proof and atomically commit one current Group projection."""
        group = event.community_actor_id
        signed_group = bool(event.source_announce_id)
        if not group or not isinstance(event.source_activity_json, dict):
            raise GroupMutationDenied('missing Group or signed mutation body')
        if signed_group:
            if (event.source_announce_id != event.delivery_id
                    or event.verified_sender_actor_id != group):
                raise GroupMutationDenied('Group Announce lacks verified outer signer')
        elif event.verified_sender_actor_id != event.actor_id:
            raise GroupMutationDenied('direct Person mutation lacks verified sender')
        fingerprint = _fingerprint(event)
        # An already accepted wire event remains idempotent after its Group
        # Follow is cancelled. This read is only an optimization: the writer
        # transaction repeats the exact same receipt check before admission.
        with self.database.session() as session:
            prior = session.scalar(select(ActivityPubEventReceipt).where(
                ActivityPubEventReceipt.delivery_id == event.delivery_id,
                ActivityPubEventReceipt.verified_destination_group_actor_id.is_(None),
            ))
            if prior is not None:
                if prior.mutation_fingerprint != fingerprint:
                    raise GroupMutationConflict('authenticated activity ID has different payload')
                if prior.status != 'processed':
                    raise GroupMutationConflict('legacy incomplete receipt requires manual audit')
                return ProjectionReceipt('duplicate', prior.outcome or 'applied',
                                         prior.detail or 'already applied', prior.group_view_revision)
        if not self.policy.federation_decision(group).allowed or not self.policy.federation_decision(event.actor_id).allowed:
            raise GroupMutationDenied('Group or actor denied by federation policy')
        # Network validation uses the currently configured, accepted Follow as
        # its expected inbox. It is rechecked in the writer below before commit.
        if signed_group:
            with self.database.session() as session:
                follow = session.scalar(select(BridgeActorFollow).where(BridgeActorFollow.community_actor_id == group))
                inbox = (follow.community_inbox_url if follow is not None and
                         follow.status == 'accepted' and not follow.cancel_requested and
                         follow.remote_software_profile == 'nodebb' and
                         follow.follow_activity_id else None)
            if inbox is None or not await self._verified_group_actor(group, inbox):
                raise GroupMutationDenied('unverified active NodeBB Group or inbox')
        with self.database.immediate_session() as session:
            # Old in_progress receipts cannot be proved to have committed a
            # projection. Leave them for the explicit pre-v3 audit, not an
            # unsafe automatic reset or a forged duplicate acknowledgment.
            receipt = session.scalar(select(ActivityPubEventReceipt).where(
                ActivityPubEventReceipt.delivery_id == event.delivery_id,
                ActivityPubEventReceipt.verified_destination_group_actor_id.is_(None),
            ))
            if receipt is not None:
                if receipt.mutation_fingerprint != fingerprint:
                    raise GroupMutationConflict('authenticated activity ID has different payload')
                if receipt.status != 'processed':
                    raise GroupMutationConflict('legacy incomplete receipt requires manual audit')
                return ProjectionReceipt('duplicate', receipt.outcome or 'applied',
                                         receipt.detail or 'already applied', receipt.group_view_revision)
            if not session.scalar(select(ChannelCommunitySubscription.id).where(
                ChannelCommunitySubscription.lemmy_community_actor_id == group,
                ChannelCommunitySubscription.status == 'accepted',
            )):
                raise GroupMutationDenied('no currently accepted channel for this Group')
            if signed_group:
                follow = session.scalar(select(BridgeActorFollow).where(BridgeActorFollow.community_actor_id == group))
                if (follow is None or follow.status != 'accepted' or follow.cancel_requested
                        or follow.remote_software_profile != 'nodebb' or
                        not follow.follow_activity_id or not follow.community_inbox_url):
                    raise GroupMutationDenied('NodeBB Follow was revoked before commit')
                if follow.community_inbox_url != inbox:
                    raise GroupMutationDenied('Group inbox changed while validating')
            model = CommunityThreadGroup if event.object.kind == 'post' else CommunityMessageGroup
            wrong = CommunityMessageGroup if model is CommunityThreadGroup else CommunityThreadGroup
            addressed = {group} if signed_group else _group_addresses(event.source_activity_json)
            if group not in addressed:
                raise GroupMutationDenied('signed direct mutation does not address Group')
            # One author-signed Activity can address several independently
            # accepted Groups. Inspect every declared, already-admitted mapping
            # under the same receipt transaction, never broaden using only a
            # canonical Note ID and never adopt an unverified first Create.
            candidates = {group: session.scalar(select(model).where(
                model.community_actor_id == group,
                model.ap_object_id == event.object.ap_id,
            ))}
            if not signed_group:
                for recipient in addressed - {group}:
                    if not self.policy.federation_decision(recipient).allowed:
                        continue
                    if session.scalar(select(ChannelCommunitySubscription.id).where(
                            ChannelCommunitySubscription.lemmy_community_actor_id == recipient,
                            ChannelCommunitySubscription.status == 'accepted',
                    )) is None:
                        continue
                    row = session.scalar(select(model).where(
                        model.community_actor_id == recipient,
                        model.ap_object_id == event.object.ap_id,
                    ))
                    if row is not None:
                        candidates[recipient] = row
            verified_rows = []
            for recipient, group_row in sorted(candidates.items()):
                if session.scalar(select(wrong.id).where(
                        wrong.community_actor_id == recipient,
                        wrong.ap_object_id == event.object.ap_id)) is not None:
                    if recipient == group:
                        raise GroupMutationDenied('ambiguous root/reply object identity')
                    continue
                if group_row is None or not _first_create_verified(group_row, kind=event.object.kind):
                    if recipient == group:
                        raise GroupMutationDenied('no verified first Create for Group object')
                    continue
                parent_id: str | None = None
                if event.object.kind == 'comment':
                    root = session.get(CommunityThreadGroup, group_row.thread_group_id)
                    parent = (session.get(CommunityMessageGroup, group_row.parent_message_group_id)
                              if group_row.parent_message_group_id is not None else None)
                    if (root is None or root.community_actor_id != recipient or not root.ap_object_id
                            or (parent is not None and (parent.community_actor_id != recipient
                                or parent.thread_group_id != root.id))
                            or (group_row.parent_message_group_id is not None and parent is None)):
                        if recipient == group:
                            raise GroupMutationDenied('original reply ancestry mismatch')
                        continue
                    parent_id = parent.ap_object_id if parent is not None else root.ap_object_id
                    if event.object.post_ap_id is not None and event.object.post_ap_id != root.ap_object_id:
                        if recipient == group:
                            raise GroupMutationDenied('changed reply root')
                        continue
                if not _verified_inner(event, group_row, group_signed=signed_group,
                                       expected_parent=parent_id):
                    if recipient == group:
                        raise GroupMutationDenied('invalid inner actor, Note owner or parent')
                    continue
                verified_rows.append(group_row)
            if not verified_rows:
                raise GroupMutationDenied('no verified Group view admitted for this Activity')
            # The timestamp is part of the authenticated inner Update, not the
            # moment this bridge happened to receive a delayed Announce.
            source_time = (_authenticated_update_time(event.source_activity_json)
                           if event.event_type.endswith('.updated') else None)
            changed = 0
            for group_row in verified_rows:
                if group_row.view_state == 'deleted':
                    # A Group-specific tombstone is terminal even for later
                    # author-signed Updates or an out-of-order old Activity.
                    continue
                if source_time is not None and group_row.view_source_updated_at is not None:
                    # Compare only timestamps authenticated by the signed source.
                    # v2 snapshots written by CP48/49 used view_updated_at as
                    # the bridge's receive time, never trusted Note ordering.
                    prior_time = group_row.view_source_updated_at
                    if prior_time.tzinfo is None:
                        prior_time = prior_time.replace(tzinfo=timezone.utc)
                    if source_time <= prior_time.astimezone(timezone.utc):
                        # Retain the receipt for this authenticated wire ID, but
                        # do not let a delayed older Note restore stale text.
                        continue
                if event.event_type.endswith('.deleted'):
                    group_row.view_state = 'deleted'
                else:
                    prior = group_row.display_object_json
                    updated = dict(prior)
                    updated['body_markdown'] = event.object.body_markdown
                    raw_object = event.source_activity_json.get('object')
                    if (event.object.kind == 'post' and isinstance(raw_object, dict)
                            and isinstance(raw_object.get('name'), str)):
                        updated['title'] = event.object.title
                    group_row.display_object_json = updated
                group_row.view_revision += 1
                group_row.view_updated_at = datetime.now(timezone.utc)
                if source_time is not None:
                    # A timestampless intermediate Update advances the local
                    # revision but cannot destroy the last trusted source clock.
                    group_row.view_source_updated_at = source_time
                group_row.last_view_activity_id = event.source_activity_id or event.delivery_id
                changed += 1
            # A stale signed Update is a processed non-mutating event, not a
            # tombstone. Its terminal receipt still fences conflicting replays.
            if changed:
                detail = 'Group view committed'
            elif all(row.view_state == 'deleted' for row in verified_rows):
                detail = 'Group object already deleted'
            else:
                detail = 'Stale Group Update ignored'
            outcome = (InboundActivityOutcome.APPLIED.value if changed else
                       InboundActivityOutcome.IGNORED_ALREADY_APPLIED.value)
            revision = (verified_rows[0].view_revision if len(verified_rows) == 1 else None)
            receipt = ActivityPubEventReceipt(
                delivery_id=event.delivery_id, event_type=event.event_type,
                object_ap_id=event.object.ap_id, status='processed',
                outcome=outcome, detail=detail, mutation_fingerprint=fingerprint,
                group_view_revision=revision,
            )
            session.add(receipt)
            session.flush()
            return ProjectionReceipt('processed', outcome, detail, revision)
