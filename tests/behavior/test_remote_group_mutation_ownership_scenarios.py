"""Verified remote Group mutations must preserve immutable Create authorization.

Each scenario creates real canonical SQLite rows through the inbound runtime, then
submits an apparent remote edit/delete and observes the Discord and DB effects.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.activitypub_models import ActivityPubEvent
from src.community_sync.runtime import CommunityRuntime
from src.content_publish_service import ContentPublishService
from src.db import Database
from src.inbound_activity_outcomes import InboundActivityOutcome
from support.runtime import build_test_policy_service

GROUP = "https://remote.example/category/1"
OTHER_GROUP = "https://remote.example/category/2"
OWNER = "https://author.example/actors/alice"
FOREIGN = "https://author.example/actors/eve"
ROOT = "https://remote.example/post/42"
REPLY = "https://remote.example/post/43"


def _event(
    operation: str = "created", *, kind: str = "post", actor: str = OWNER,
    group: str = GROUP, source_actor: str | None = None,
    signed_direct: bool = True, attributed: str | None = None,
    parent: str | None = None,
) -> ActivityPubEvent:
    """Build the gateway's trusted normalized event and its typed wire evidence."""
    object_id = ROOT if kind == "post" else REPLY
    author = actor if attributed is None else attributed
    object_data = {
        "id": object_id, "type": "Note", "attributedTo": author,
        "audience": group, "inReplyTo": parent if kind == "comment" else None,
    }
    source = {"id": f"https://author.example/activities/{kind}/{operation}",
              "type": {"created": "Create", "updated": "Update", "deleted": "Delete"}[operation],
              "actor": source_actor if source_actor is not None else actor,
              "audience": group, "object": object_id if operation == "deleted" else object_data}
    return ActivityPubEvent.model_validate({
        "event_type": f"{kind}.{operation}", "delivery_id": source["id"],
        "occurred_at": datetime.now(timezone.utc), "community_actor_id": group,
        "actor_id": actor,
        "source_activity_json": source if operation != "created" else None,
        "source_announce_id": None if signed_direct else "https://remote.example/announces/1",
        "object": {
            "ap_id": object_id, "kind": kind, "lemmy_id": 0,
            "post_ap_id": ROOT if kind == "comment" else None,
            "post_lemmy_id": 0 if kind == "comment" else None,
            "parent_ap_id": parent if kind == "comment" else None,
            "title": "topic", "body_markdown": "updated content", "url": object_id,
            "author_name": "alice", "published_at": datetime.now(timezone.utc),
            "attributed_to_actor_id": author,
        },
    })


def _runtime(tmp_path: Path):
    """Persist one accepted Group and expose only fake Discord/network boundaries."""
    db = Database(f"sqlite:///{tmp_path / 'mutation.db'}")
    db.create_all()
    db.remote_subscriptions.create_subscription(
        discord_channel_id=100, lemmy_community_actor_id=GROUP,
        lemmy_community_name="topic", lemmy_community_id=None,
        community_inbox_url=f"{GROUP}/inbox", status="accepted",
    )
    forum = SimpleNamespace(id=100, create_thread=AsyncMock(return_value=(
        SimpleNamespace(id=200), SimpleNamespace(id=300))))
    thread = SimpleNamespace(id=200, send=AsyncMock(return_value=SimpleNamespace(id=400)))

    async def fetch_forum_channel(channel):
        return forum

    async def get_thread_by_id(thread_id):
        return thread

    async def ready():
        return None

    bot = SimpleNamespace(fetch_forum_channel=fetch_forum_channel,
                          get_thread_by_id=get_thread_by_id, wait_until_bridge_ready=ready)
    policy = build_test_policy_service(db)
    publish = ContentPublishService(database=db, fedify_gateway=AsyncMock(),
                                    bridge_prefix="[bridge]", bridge_policy_service=policy)
    service = CommunityRuntime(database=db, content_publish_service=publish, bot=bot)
    context = SimpleNamespace(bot=bot, database=db, bridge_policy_service=policy)
    return db, service, context


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["updated", "deleted"])
@pytest.mark.parametrize("kind", ["post", "comment"])
async def test_signed_original_owner_can_mutate_own_root_or_reply(tmp_path, monkeypatch, operation, kind):
    """Direct signed owner mutation affects only its canonical Discord surfaces."""
    db, service, context = _runtime(tmp_path)
    from src.community_sync import runtime as runtime_module
    effect = AsyncMock()
    monkeypatch.setattr(runtime_module, f"propagate_inbound_{kind}_{'update' if operation == 'updated' else 'delete'}", effect)
    try:
        assert (await service.handle_inbound_post(_event(), context)).status == "processed"
        if kind == "comment":
            assert (await service.handle_inbound_comment(_event(kind="comment", parent=ROOT), context)).status == "processed"
        result = await getattr(service, f"handle_inbound_{kind}_{'update' if operation == 'updated' else 'delete'}")(
            _event(operation, kind=kind, parent=ROOT if kind == "comment" else None), context)
        assert result.status == "processed"
        effect.assert_awaited_once()
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["foreign_owner", "other_group", "wrong_attribution", "claimed_actor_mismatch", "group_announce", "missing_evidence", "legacy_null_owner"])
async def test_unproven_mutation_never_reaches_discord(tmp_path, monkeypatch, case):
    """Forged provenance, wrong tenant and legacy unknown owners cannot mutate."""
    db, service, context = _runtime(tmp_path)
    from src.community_sync import runtime as runtime_module
    effect = AsyncMock()
    monkeypatch.setattr(runtime_module, "propagate_inbound_post_update", effect)
    try:
        await service.handle_inbound_post(_event(), context)
        if case == "legacy_null_owner":
            with db.session() as session:
                from src.models import CommunityThreadGroup
                row = session.query(CommunityThreadGroup).filter_by(ap_object_id=ROOT).one()
                row.source_owner_actor_id = None
        changes = {
            "foreign_owner": {"actor": FOREIGN},
            "other_group": {"group": OTHER_GROUP},
            "wrong_attribution": {"attributed": FOREIGN},
            "claimed_actor_mismatch": {"source_actor": FOREIGN},
            "group_announce": {"signed_direct": False},
        }.get(case, {})
        event = _event("updated", **changes)
        if case == "missing_evidence":
            event = event.model_copy(update={"source_activity_json": None})
        result = await service.handle_inbound_post_update(event, context)
        assert result.status == "skipped"
        effect.assert_not_awaited()
    finally:
        db.close()


@pytest.mark.asyncio
async def test_group_signed_tombstone_cannot_delete_other_person_reply(tmp_path, monkeypatch):
    """Group Announce(Delete(Tombstone)) cannot impersonate the initial Person."""
    db, service, context = _runtime(tmp_path)
    from src.community_sync import runtime as runtime_module
    effect = AsyncMock()
    monkeypatch.setattr(runtime_module, "propagate_inbound_comment_delete", effect)
    try:
        await service.handle_inbound_post(_event(), context)
        await service.handle_inbound_comment(_event(kind="comment", parent=ROOT), context)
        event = _event("deleted", kind="comment", actor=GROUP,
                       attributed=OWNER, signed_direct=False, parent=ROOT)
        result = await service.handle_inbound_comment_delete(event, context)
        assert result.status == "skipped"
        effect.assert_not_awaited()
    finally:
        db.close()


@pytest.mark.asyncio
async def test_delete_nodebb_post_url_reply_uses_canonical_comment_kind(tmp_path, monkeypatch):
    """A NodeBB /post/{pid} reply Delete must not edit the root starter."""
    db, service, context = _runtime(tmp_path)
    from src.community_sync import runtime as runtime_module
    reply_delete = AsyncMock()
    root_delete = AsyncMock()
    monkeypatch.setattr(runtime_module, "propagate_inbound_comment_delete", reply_delete)
    monkeypatch.setattr(runtime_module, "propagate_inbound_post_delete", root_delete)
    try:
        await service.handle_inbound_post(_event(), context)
        await service.handle_inbound_comment(_event(kind="comment", parent=ROOT), context)
        # Gateway cannot infer root/reply from /post/42 and /post/43 alone.
        ambiguous_wire = _event("deleted", kind="comment").model_copy(update={
            "event_type": "post.deleted",
            "object": _event("deleted", kind="comment").object.model_copy(update={
                "kind": "post", "post_ap_id": None,
            }),
        })
        result = await service.handle_inbound_post_delete(ambiguous_wire, context)
        assert result.outcome == InboundActivityOutcome.APPLIED
        root_delete.assert_not_awaited()
        reply_delete.assert_awaited_once()
    finally:
        db.close()


@pytest.mark.asyncio
async def test_delete_no_longer_authorized_after_last_group_unsubscribe(tmp_path, monkeypatch):
    """A formerly valid owner signature cannot mutate an unsubscribed Group."""
    db, service, context = _runtime(tmp_path)
    from src.community_sync import runtime as runtime_module
    effect = AsyncMock()
    monkeypatch.setattr(runtime_module, "propagate_inbound_post_delete", effect)
    try:
        await service.handle_inbound_post(_event(), context)
        with db.session() as session:
            from src.models import ChannelCommunitySubscription
            session.query(ChannelCommunitySubscription).delete()
        result = await service.handle_inbound_post_delete(_event("deleted"), context)
        assert result.outcome == InboundActivityOutcome.IGNORED_NO_SUBSCRIPTION
        effect.assert_not_awaited()
    finally:
        db.close()


@pytest.mark.asyncio
async def test_comment_edit_cannot_change_original_parent(tmp_path, monkeypatch):
    """A same-owner edit cannot move a reply to a different root or parent."""
    db, service, context = _runtime(tmp_path)
    from src.community_sync import runtime as runtime_module
    effect = AsyncMock()
    monkeypatch.setattr(runtime_module, "propagate_inbound_comment_update", effect)
    try:
        await service.handle_inbound_post(_event(), context)
        await service.handle_inbound_comment(_event(kind="comment", parent=ROOT), context)
        changed = _event("updated", kind="comment", parent="https://remote.example/post/999")
        result = await service.handle_inbound_comment_update(changed, context)
        assert result.outcome == InboundActivityOutcome.IGNORED_UNAUTHORIZED_MUTATION
        effect.assert_not_awaited()
    finally:
        db.close()


@pytest.mark.asyncio
async def test_group_with_duplicate_root_and_reply_id_is_ambiguous(tmp_path, monkeypatch):
    """A post/reply identity collision cannot accidentally choose a surface."""
    db, service, context = _runtime(tmp_path)
    from src.community_sync import runtime as runtime_module
    effect = AsyncMock()
    monkeypatch.setattr(runtime_module, "propagate_inbound_post_update", effect)
    try:
        await service.handle_inbound_post(_event(), context)
        root = db.discord_fanout_groups.get_thread_group_by_ap_object(ROOT, community_actor_id=GROUP)
        with db.session() as session:
            from src.models import CommunityMessageGroup
            session.add(CommunityMessageGroup(
                community_actor_id=GROUP, thread_group_id=root.id,
                ap_object_id=ROOT, source_owner_actor_id=OWNER,
            ))
        result = await service.handle_inbound_post_update(_event("updated"), context)
        assert result.outcome == InboundActivityOutcome.IGNORED_UNAUTHORIZED_MUTATION
        effect.assert_not_awaited()
    finally:
        db.close()


@pytest.mark.asyncio
async def test_missing_original_root_prevents_reply_mutation(tmp_path, monkeypatch):
    """An orphaned legacy reply never becomes a standalone mutable post."""
    db, service, context = _runtime(tmp_path)
    from src.community_sync import runtime as runtime_module
    effect = AsyncMock()
    monkeypatch.setattr(runtime_module, "propagate_inbound_comment_update", effect)
    try:
        await service.handle_inbound_post(_event(), context)
        await service.handle_inbound_comment(_event(kind="comment", parent=ROOT), context)
        with db.session() as session:
            from src.models import CommunityThreadGroup
            session.query(CommunityThreadGroup).filter_by(ap_object_id=ROOT).delete()
        result = await service.handle_inbound_comment_update(
            _event("updated", kind="comment", parent=ROOT), context)
        assert result.outcome == InboundActivityOutcome.IGNORED_UNAUTHORIZED_MUTATION
        effect.assert_not_awaited()
    finally:
        db.close()


@pytest.mark.asyncio
async def test_direct_signed_owner_delete_tombstone_vs_wrong_attribution(tmp_path, monkeypatch):
    """Only a signed owner Delete may tombstone the owner's original object."""
    db, service, context = _runtime(tmp_path)
    from src.community_sync import runtime as runtime_module
    effect = AsyncMock()
    monkeypatch.setattr(runtime_module, "propagate_inbound_post_delete", effect)
    try:
        await service.handle_inbound_post(_event(), context)
        source = _event("deleted")
        source_record = dict(source.source_activity_json)
        source_record["object"] = {"type": "Tombstone", "id": ROOT,
                                   "attributedTo": FOREIGN, "audience": GROUP}
        denied = await service.handle_inbound_post_delete(
            source.model_copy(update={"source_activity_json": source_record}), context)
        assert denied.outcome == InboundActivityOutcome.IGNORED_UNAUTHORIZED_MUTATION
        effect.assert_not_awaited()
        source_record["object"]["attributedTo"] = OWNER
        accepted = await service.handle_inbound_post_delete(
            source.model_copy(update={"source_activity_json": source_record}), context)
        assert accepted.outcome == InboundActivityOutcome.APPLIED
        effect.assert_awaited_once()
    finally:
        db.close()


@pytest.mark.asyncio
async def test_delete_kind_is_resolved_group_scoped_before_receipt(tmp_path):
    """A wrong URL hint becomes a scoped canonical event without changing wire ID."""
    from src.community_sync.edit_delete import canonicalize_remote_delete_kind
    from src.models import CommunityThreadGroup
    db, service, context = _runtime(tmp_path)
    try:
        await service.handle_inbound_post(_event(), context)
        await service.handle_inbound_comment(_event(kind="comment", parent=ROOT), context)
        # Different Group may have the same AP object ID as a root.
        with db.session() as session:
            session.add(CommunityThreadGroup(
                community_actor_id=OTHER_GROUP, ap_object_id=REPLY,
                source_owner_actor_id=FOREIGN,
            ))
        original = _event("deleted", kind="comment")
        false_hint = original.model_copy(update={
            "event_type": "post.deleted",
            "object": original.object.model_copy(update={"kind": "post"}),
        })
        canonical = canonicalize_remote_delete_kind(db, false_hint)
        assert canonical.event_type == "comment.deleted"
        assert canonical.object.kind == "comment"
        assert canonical.delivery_id == false_hint.delivery_id
        assert canonical.source_activity_json == false_hint.source_activity_json
        unrelated = false_hint.model_copy(update={"community_actor_id": OTHER_GROUP})
        assert canonicalize_remote_delete_kind(db, unrelated).event_type == "post.deleted"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_signed_owner_cannot_redirect_revision_to_other_group(tmp_path, monkeypatch):
    """Actor ownership does not permit silently changing the Group audience."""
    from src.community_sync import runtime as runtime_module
    db, service, context = _runtime(tmp_path)
    effect = AsyncMock()
    monkeypatch.setattr(runtime_module, "propagate_inbound_post_update", effect)
    try:
        await service.handle_inbound_post(_event(), context)
        direct = _event("updated")
        record = dict(direct.source_activity_json)
        record["object"] = dict(record["object"], audience=OTHER_GROUP)
        result = await service.handle_inbound_post_update(
            direct.model_copy(update={"source_activity_json": record}), context)
        assert result.outcome == InboundActivityOutcome.IGNORED_UNAUTHORIZED_MUTATION
        effect.assert_not_awaited()
    finally:
        db.close()
