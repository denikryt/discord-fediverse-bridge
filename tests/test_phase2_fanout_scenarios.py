"""Phase 2 fanout contract exercised through verified normalized runtime ingress.

Each scenario retains its original source/mirror/duplicate/failure assertion,
but checks typed SQLite receipts instead of obsolete Discord-only projections.
"""
from __future__ import annotations

import pytest

from src.models import PlatformDeliveryIntent
from sqlalchemy import select
from test_normalized_community_runtime import world, starter


@pytest.mark.asyncio
async def test_phase2_single_subscription_no_mirror_created(tmp_path):
    """One accepted source forum publishes once with no physical mirror POST."""
    db, runtime, gateway, adapter = world(tmp_path, forum_ids=(100,))
    try:
        event, view = starter()
        result = await runtime.handle_normalized_thread_create(event, view, adapter=adapter)
        group = db.discord_fanout_groups.get_thread_group_by_source_thread(200)
        assert result.status == 'published' and group is not None
        deliveries = db.discord_fanout_groups.get_thread_deliveries(group.id)
        assert len(deliveries) == 1
        source = deliveries[0]
        assert (source.role, source.external_thread_id, source.external_starter_message_id) == ('source', '200', '300')
        assert adapter.posts == []
        assert len(gateway.requests) == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_phase2_two_subscriptions_mirror_thread_created_in_sibling(tmp_path):
    """The sibling receives one confirmed thread with a distinct starter receipt."""
    db, runtime, gateway, adapter = world(tmp_path, forum_ids=(100,101))
    try:
        event, view = starter()
        result = await runtime.handle_normalized_thread_create(event, view, adapter=adapter)
        group = db.discord_fanout_groups.get_thread_group_by_source_thread(200)
        assert result.status == 'published' and group is not None
        deliveries = db.discord_fanout_groups.get_thread_deliveries(group.id)
        assert {row.role for row in deliveries} == {'source', 'mirror'}
        mirror = next(row for row in deliveries if row.role == 'mirror')
        assert (mirror.external_thread_id, mirror.external_starter_message_id) == ('1101','2101')
        assert mirror.discord_thread_id is None  # New typed receipt is authoritative.
        assert [forum.external_channel_id for forum, _body, _attempt in adapter.posts] == ['101']
        assert len(gateway.requests) == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_phase2_duplicate_thread_create_is_ignored(tmp_path):
    """Replaying a verified THREAD_CREATE never makes a second AP object or POST."""
    db, runtime, gateway, adapter = world(tmp_path, forum_ids=(100,101))
    try:
        event, view = starter()
        assert (await runtime.handle_normalized_thread_create(event, view, adapter=adapter)).status == 'published'
        again = await runtime.handle_normalized_thread_create(event, view, adapter=adapter)
        group = db.discord_fanout_groups.get_thread_group_by_source_thread(200)
        assert again.status == 'ignored' and again.reason == 'duplicate_discord_thread'
        assert len(db.discord_fanout_groups.get_thread_deliveries(group.id)) == 2
        assert len(gateway.requests) == 1 and len(adapter.posts) == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_phase2_mirror_failure_does_not_block_source_publish(tmp_path):
    """A failing mirror keeps the canonical source and a durable uncertain intent."""
    db, runtime, gateway, adapter = world(tmp_path, forum_ids=(100,101), fail_forum='101')
    try:
        event, view = starter()
        result = await runtime.handle_normalized_thread_create(event, view, adapter=adapter)
        group = db.discord_fanout_groups.get_thread_group_by_source_thread(200)
        assert result.status == 'published' and group is not None
        deliveries = db.discord_fanout_groups.get_thread_deliveries(group.id)
        assert len(deliveries) == 1 and deliveries[0].role == 'source'
        assert len(adapter.posts) == 1 and len(gateway.requests) == 1
        with db.session() as session:
            intents = session.scalars(select(PlatformDeliveryIntent).where(
                PlatformDeliveryIntent.domain=='remote', PlatformDeliveryIntent.object_kind=='post',
            )).all()
            assert len(intents) == 1 and intents[0].state == 'unresolved'
    finally:
        db.close()
