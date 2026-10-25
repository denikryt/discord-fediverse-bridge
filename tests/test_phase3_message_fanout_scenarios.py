"""Phase 3 reply fanout through the sole verified Community Create ingress.

Original source/mirror/duplicate/legacy-parent/failure scenarios remain distinct.
SQLite and confirmed typed receipts are authoritative; no raw SDK message Create
or fallback from a legacy PostLink may publish a second ActivityPub object.
"""
from __future__ import annotations

from unittest.mock import AsyncMock
import pytest
from sqlalchemy import select

from src.models import PlatformDeliveryIntent
from test_normalized_community_runtime import reply, starter, world


@pytest.mark.asyncio
async def test_phase3_single_subscription_message_published_no_mirror(tmp_path):
    """A verified reply with one forum has one physical source and zero mirrors."""
    db, runtime, gateway, transport = world(tmp_path, forum_ids=(100,))
    try:
        event, observed = starter()
        await runtime.handle_normalized_thread_create(event, observed, adapter=transport)
        created = await runtime.handle_normalized_message_create(reply(), adapter=transport)
        group = db.discord_fanout_groups.get_message_group_by_source_message(400)
        assert created.status == 'published' and group is not None
        copies = db.discord_fanout_groups.get_message_deliveries(group.id)
        assert len(copies) == 1
        assert (copies[0].role, copies[0].external_thread_id,
                copies[0].external_message_id) == ('source', '200', '400')
        assert transport.replies == [] and len(gateway.requests) == 2
    finally:
        db.close()


@pytest.mark.asyncio
async def test_phase3_two_subscriptions_message_mirrored_to_sibling_thread(tmp_path):
    """The sibling receives one reply in its own confirmed physical thread."""
    db, runtime, gateway, transport = world(tmp_path, forum_ids=(100, 101))
    try:
        event, observed = starter()
        await runtime.handle_normalized_thread_create(event, observed, adapter=transport)
        created = await runtime.handle_normalized_message_create(reply(), adapter=transport)
        group = db.discord_fanout_groups.get_message_group_by_source_message(400)
        copies = db.discord_fanout_groups.get_message_deliveries(group.id)
        assert created.status == 'published' and len(copies) == 2
        assert {r.role for r in copies} == {'source', 'mirror'}
        mirror = next(r for r in copies if r.role == 'mirror')
        assert (mirror.external_thread_id, mirror.external_message_id) == ('1101', '4101')
        assert len(transport.replies) == 1
        assert transport.replies[0][0].external_thread_id == '1101'
        assert len(gateway.requests) == 2
    finally:
        db.close()


@pytest.mark.asyncio
async def test_phase3_duplicate_message_is_ignored(tmp_path):
    """Replayed MESSAGE_CREATE preserves its original canonical ID and receipt."""
    db, runtime, gateway, transport = world(tmp_path, forum_ids=(100, 101))
    try:
        event, observed = starter()
        await runtime.handle_normalized_thread_create(event, observed, adapter=transport)
        message = reply()
        await runtime.handle_normalized_message_create(message, adapter=transport)
        replay = await runtime.handle_normalized_message_create(message, adapter=transport)
        group = db.discord_fanout_groups.get_message_group_by_source_message(400)
        assert replay.status == 'ignored' and replay.reason == 'duplicate_discord_message'
        assert len(db.discord_fanout_groups.get_message_deliveries(group.id)) == 2
        assert len(transport.replies) == 1 and len(gateway.requests) == 2
    finally:
        db.close()


@pytest.mark.asyncio
async def test_phase3_message_in_legacy_thread_is_ignored_without_thread_group(tmp_path):
    """A legacy PostLink alone does not establish the typed physical parent."""
    db, runtime, gateway, transport = world(tmp_path, forum_ids=(100,))
    try:
        db.legacy_lemmy_mappings.create_post_link(
            lemmy_post_id=-200,
            lemmy_post_ap_id='https://bridge.example/objects/post/legacy',
            discord_forum_channel_id=100, discord_forum_thread_id=200,
            discord_starter_message_id=300, direction='discord_to_activitypub',
        )
        ignored = await runtime.handle_normalized_message_create(reply(), adapter=transport)
        assert ignored.status == 'ignored' and ignored.reason == 'no_post_context'
        assert db.discord_fanout_groups.get_message_group_by_source_message(400) is None
        assert gateway.requests == [] and transport.replies == []
    finally:
        db.close()


@pytest.mark.asyncio
async def test_phase3_mirror_message_failure_does_not_block_source_publish(tmp_path):
    """An uncertain sibling HTTP reply leaves canonical Create intact and recoverable."""
    db, runtime, gateway, transport = world(tmp_path, forum_ids=(100, 101))
    try:
        event, observed = starter()
        await runtime.handle_normalized_thread_create(event, observed, adapter=transport)
        transport.send_message = AsyncMock(side_effect=OSError('lost HTTP response'))
        created = await runtime.handle_normalized_message_create(reply(), adapter=transport)
        group = db.discord_fanout_groups.get_message_group_by_source_message(400)
        assert created.status == 'published' and group is not None
        copies = db.discord_fanout_groups.get_message_deliveries(group.id)
        assert len(copies) == 1 and copies[0].role == 'source'
        assert transport.send_message.await_count == 1 and len(gateway.requests) == 2
        with db.session() as session:
            intents = session.scalars(select(PlatformDeliveryIntent).where(
                PlatformDeliveryIntent.domain == 'remote',
                PlatformDeliveryIntent.object_kind == 'comment',
                PlatformDeliveryIntent.canonical_row_id == group.id,
            )).all()
            assert len(intents) == 1 and intents[0].state == 'unresolved'
    finally:
        db.close()
