"""End-to-end Remote Community proof that typed receipts are visible to later comments."""

from __future__ import annotations

import sys
from types import ModuleType

# These scenarios fake only network edges; importing the legacy fanout does not
# require a live discord.py installation or SDK instance.
sys.modules.setdefault("discord", ModuleType("discord"))

from dataclasses import replace
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from src.fedify_gateway_client import RenderCommunityResult

from src.models import CommunityMessageGroupDelivery, CommunityThreadGroupDelivery, PlatformDeliveryIntent
from test_normalized_community_runtime import PhysicalTransport, reply, starter, world
from support.fake_platform_adapter import FakeDiscordPort


class VerifiedPhysicalTransport(PhysicalTransport, FakeDiscordPort):
    """Use complete adapter port contract with observable physical receipts."""



def fresh_world(tmp_path):
    """Use real domain handlers and SQLite, with only signed gateway HTTP faked."""
    db, runtime, gateway, adapter = world(tmp_path)

    actor = db.users.get_user_by_discord_user_id("123").actor_url

    async def render(request):
        """Return exactly the deterministic activity built from the verified source."""
        return RenderCommunityResult(
            activity_id=request.activity_id, object_id=request.object_id,
            community_actor_url=request.community_actor_url, published_at=request.published_at,
            activity_json={
                "id": request.activity_id, "type": "Create",
                "actor": actor,
                "object": {"id": request.object_id, "type": "Page" if request.kind == "post" else "Note",
                           "published": request.published_at, "attributedTo": actor,
                           "source": {"content": request.body_markdown}, "name": request.title,
                           "inReplyTo": request.in_reply_to_object_id},
                "to": [request.community_actor_url],
            },
        )
    gateway.render_community = render
    return db, runtime, gateway, VerifiedPhysicalTransport()


def verified_starter():
    """Preserve the actual source creation instant independently of receipt time."""
    event, view = starter()
    return event, replace(view, created_at=datetime(2026, 10, 24, tzinfo=timezone.utc))


def verified_reply(**kwargs):
    """A source message has a trustworthy creation time, not local wall time."""
    return replace(reply(**kwargs), created_at=datetime(2026, 10, 24, tzinfo=timezone.utc))


@pytest.mark.asyncio
async def test_typed_mirror_threads_are_used_by_source_comment_and_nested_reply(tmp_path):
    """Physical B/C receipts without Discord projections must accept later comments."""
    db, runtime, gateway, adapter = fresh_world(tmp_path)
    try:
        event, opening = verified_starter()
        assert (await runtime.handle_normalized_thread_create(event, opening, adapter=adapter)).status == 'published'
        group = db.discord_fanout_groups.get_thread_group_by_source_thread(200)
        with db.session() as session:
            rows = session.scalars(select(CommunityThreadGroupDelivery).where(
                CommunityThreadGroupDelivery.thread_group_id == group.id,
                CommunityThreadGroupDelivery.role == 'mirror',
            )).all()
            assert {row.external_thread_id for row in rows} == {'1101', '1102'}
            assert all(row.discord_thread_id is None and row.discord_channel_id is None for row in rows)
        # This runs the actual Remote Community publication and mirror fan-out,
        # not the coordinator directly: previously it silently sent zero replies.
        assert (await runtime.handle_normalized_message_create(verified_reply(), adapter=adapter)).status == 'published'
        with db.session() as session:
            targets = session.scalars(select(PlatformDeliveryIntent).where(
                PlatformDeliveryIntent.domain == 'remote', PlatformDeliveryIntent.object_kind == 'comment',
            )).all()
            assert len(targets) == 2
            assert all(row.state == 'delivered' for row in targets)
            copies = session.scalars(select(CommunityMessageGroupDelivery).where(
                CommunityMessageGroupDelivery.role == 'mirror',
            )).all()
            assert {row.external_thread_id for row in copies} == {'1101', '1102'}
        assert len(adapter.replies) == 2
        assert {dest.external_thread_id for dest, _parent, _attempt in adapter.replies} == {'1101', '1102'}
        assert {parent.external_message_id for _dest, parent, _attempt in adapter.replies} == {'2101', '2102'}
        # A human posting in B's typed mirror is also a valid physical source.
        assert (await runtime.handle_normalized_message_create(
            verified_reply(forum='101', thread='1101', message='700', parent='2101'), adapter=adapter,
        )).status == 'published'
    finally:
        db.close()


@pytest.mark.asyncio
async def test_typed_thread_lookup_rejects_foreign_forum_claim(tmp_path):
    """Matching thread ID in the wrong forum must not authorize source routing."""
    db, runtime, gateway, adapter = fresh_world(tmp_path)
    try:
        event, opening = verified_starter()
        await runtime.handle_normalized_thread_create(event, opening, adapter=adapter)
        # Correct ID, but submitted under forum 102 rather than its actual forum 101.
        denied = await runtime.handle_normalized_message_create(
            verified_reply(forum='102', thread='1101', message='900', parent='2101'), adapter=adapter,
        )
        assert denied.status != 'published'
        with db.session() as session:
            assert session.scalar(select(CommunityMessageGroupDelivery)) is None
        assert adapter.replies == []
    finally:
        db.close()

@pytest.mark.asyncio
async def test_typed_mirror_parent_missing_in_one_forum_stays_blocked(tmp_path):
    """A known nested parent lacking B's receipt must not become a flat reply."""
    from src.models import CommunityMessageGroup

    db, runtime, gateway, adapter = fresh_world(tmp_path)
    try:
        created, opening = verified_starter()
        await runtime.handle_normalized_thread_create(created, opening, adapter=adapter)
        await runtime.handle_normalized_message_create(verified_reply(), adapter=adapter)
        parent = db.discord_fanout_groups.get_message_group_by_source_message(400)
        with db.immediate_session() as session:
            copy = session.scalar(select(CommunityMessageGroupDelivery).where(
                CommunityMessageGroupDelivery.message_group_id == parent.id,
                CommunityMessageGroupDelivery.external_thread_id == '1101',
            ))
            assert copy is not None
            session.delete(copy)
        observed_before = len(adapter.replies)
        assert (await runtime.handle_normalized_message_create(
            verified_reply(message='401', parent='400'), adapter=adapter,
        )).status == 'published'
        child = db.discord_fanout_groups.get_message_group_by_source_message(401)
        with db.session() as session:
            intents = session.scalars(select(PlatformDeliveryIntent).where(
                PlatformDeliveryIntent.domain == 'remote',
                PlatformDeliveryIntent.object_kind == 'comment',
                PlatformDeliveryIntent.canonical_row_id == child.id,
            )).all()
            assert len(intents) == 2
            states = {str(i.channel_surface_id): i.state for i in intents}
            assert sorted(states.values()) == ['blocked', 'delivered']
            blocked = next(i for i in intents if i.state == 'blocked')
            assert blocked.blocked_parent_canonical_id == parent.id
            assert blocked.request_json is None
        # One verified parent in C yields one child, while B remains durable.
        assert len(adapter.replies) == observed_before + 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_inbound_remote_ap_comment_uses_typed_only_starter_threads(tmp_path):
    """An authenticated inbound comment must see both typed forum receipts."""
    from dataclasses import dataclass
    from types import ModuleType
    from unittest.mock import patch
    from src.activitypub_models import ActivityPubEvent

    db, runtime, gateway, adapter = fresh_world(tmp_path)
    try:
        source, opening = verified_starter()
        await runtime.handle_normalized_thread_create(source, opening, adapter=adapter)
        post = db.discord_fanout_groups.get_thread_group_by_source_thread(200)
        now = datetime(2026, 10, 24, tzinfo=timezone.utc).isoformat()
        event = ActivityPubEvent.model_validate({
            'event_type': 'comment.created',
            'delivery_id': 'https://remote.example/activities/comment-77',
            'occurred_at': now, 'community_actor_id': post.community_actor_id,
            'actor_id': 'https://remote.example/u/bob',
            'object': {
                'ap_id': 'https://remote.example/comment/77', 'kind': 'comment',
                'lemmy_id': 77, 'post_ap_id': post.ap_object_id, 'post_lemmy_id': 200,
                'parent_ap_id': post.ap_object_id, 'title': None,
                'body_markdown': 'inbound comment',
                'url': 'https://remote.example/comment/77',
                'published_at': now, 'author_name': 'Bob',
            },
        })
        sdk_boundary = ModuleType('src.activitypub_handlers')
        @dataclass
        class HandlerResult:
            """Preserve the public outcome contract without importing Discord SDK."""
            status: str
            outcome: object
            detail: str
        sdk_boundary.HandlerResult = HandlerResult
        with patch.dict('sys.modules', {'src.activitypub_handlers': sdk_boundary}):
            outcome = await runtime.handle_normalized_inbound_comment(event, adapter=adapter)
        assert outcome.status == 'processed'
        created = db.discord_fanout_groups.get_message_group_by_ap_object(event.object.ap_id)
        assert created is not None
        with db.session() as session:
            rows = session.scalars(select(PlatformDeliveryIntent).where(
                PlatformDeliveryIntent.domain == 'remote',
                PlatformDeliveryIntent.object_kind == 'comment',
                PlatformDeliveryIntent.canonical_row_id == created.id,
            )).all()
            assert len(rows) == 3  # source + B + C threads are accepted subscriptions
            assert all(row.state == 'delivered' for row in rows)
    finally:
        db.close()


@pytest.mark.asyncio
async def test_revoked_typed_mirror_is_historical_not_new_reply_target(tmp_path):
    """Revoking B after its Create receipt removes B from later fan-out only."""
    from src.community_sync.delivery_mapping import get_sibling_thread_deliveries

    db, runtime, gateway, adapter = fresh_world(tmp_path)
    try:
        source, opening = verified_starter()
        await runtime.handle_normalized_thread_create(source, opening, adapter=adapter)
        canonical = db.discord_fanout_groups.get_thread_group_by_source_thread(200)
        db.remote_subscriptions.update_subscription_follow_state(
            discord_channel_id=101, community_inbox_url=None,
            follow_activity_id=None, status='pending',
        )
        children = get_sibling_thread_deliveries(
            db, thread_group_id=canonical.id, source_thread=source.thread,
        )
        assert {row.external_thread_id for row in children} == {'1102'}
        assert (await runtime.handle_normalized_message_create(
            verified_reply(), adapter=adapter,
        )).status == 'published'
        assert [dest.external_thread_id for dest, _, _ in adapter.replies] == ['1102']
        with db.session() as session:
            # Original B receipt survives as historical evidence; no new B
            # comment intent is admitted after the Follow was revoked.
            historical = session.scalars(select(CommunityThreadGroupDelivery).where(
                CommunityThreadGroupDelivery.thread_group_id == canonical.id,
                CommunityThreadGroupDelivery.external_thread_id == '1101',
            )).all()
            assert len(historical) == 1
            comment_intents = session.scalars(select(PlatformDeliveryIntent).where(
                PlatformDeliveryIntent.domain == 'remote',
                PlatformDeliveryIntent.object_kind == 'comment',
            )).all()
            assert len(comment_intents) == 1
    finally:
        db.close()
