"""Verify physical parent preservation through typed Community Create ingress.

A known canonical nested parent has a different physical MessageRef in each
recipient thread. Unknown source references follow the documented flat fallback,
not a fabricated sibling parent or an SDK-specific numeric message lookup.
"""
from __future__ import annotations

import pytest

from test_normalized_community_runtime import reply, starter, world


async def _published_post(tmp_path):
    """Create a verified source starter and its independent sibling forum receipt."""
    db, runtime, gateway, adapter = world(tmp_path, forum_ids=(100, 101))
    event, observed = starter()
    assert (await runtime.handle_normalized_thread_create(event, observed, adapter=adapter)).status == 'published'
    return db, runtime, gateway, adapter


@pytest.mark.asyncio
async def test_phase4_root_message_mirrored_flat(tmp_path):
    """A source reply without a reference reaches the sibling with no reply-to."""
    db, runtime, gateway, adapter = await _published_post(tmp_path)
    try:
        result = await runtime.handle_normalized_message_create(
            reply(message='400', parent=None), adapter=adapter,
        )
        group = db.discord_fanout_groups.get_message_group_by_source_message(400)
        assert result.status == 'published' and group is not None
        assert group.parent_message_group_id is None
        assert len(adapter.replies) == 1 and adapter.replies[0][1] is None
    finally:
        db.close()


@pytest.mark.asyncio
async def test_phase4_reply_to_starter_references_mirror_starter(tmp_path):
    """A starter reply targets the confirmed starter of the corresponding sibling."""
    db, runtime, gateway, adapter = await _published_post(tmp_path)
    try:
        result = await runtime.handle_normalized_message_create(
            reply(message='400', parent='300'), adapter=adapter,
        )
        group = db.discord_fanout_groups.get_message_group_by_source_message(400)
        assert result.status == 'published' and group.parent_message_group_id is None
        assert len(adapter.replies) == 1
        destination, parent, _attempt = adapter.replies[0]
        assert destination.external_thread_id == '1101'
        assert (parent.external_container_id, parent.external_message_id) == ('1101', '2101')
    finally:
        db.close()


@pytest.mark.asyncio
async def test_phase4_reply_to_mirrored_message_references_mirror_delivery(tmp_path):
    """A nested reply points to its recipient's copy, never the source message."""
    db, runtime, gateway, adapter = await _published_post(tmp_path)
    try:
        await runtime.handle_normalized_message_create(reply(message='400', parent='300'), adapter=adapter)
        previous = db.discord_fanout_groups.get_message_group_by_source_message(400)
        created = await runtime.handle_normalized_message_create(
            reply(message='401', parent='400'), adapter=adapter,
        )
        group = db.discord_fanout_groups.get_message_group_by_source_message(401)
        assert created.status == 'published' and group.parent_message_group_id == previous.id
        assert len(adapter.replies) == 2
        destination, parent, _attempt = adapter.replies[1]
        assert (destination.external_thread_id, parent.external_container_id,
                parent.external_message_id) == ('1101', '1101', '4101')
    finally:
        db.close()


@pytest.mark.asyncio
async def test_phase4_reply_to_unknown_message_mirrored_flat(tmp_path):
    """Unknown source-side physical parents use the root fallback, not a fake ref."""
    db, runtime, gateway, adapter = await _published_post(tmp_path)
    try:
        result = await runtime.handle_normalized_message_create(
            reply(message='400', parent='999'), adapter=adapter,
        )
        group = db.discord_fanout_groups.get_message_group_by_source_message(400)
        assert result.status == 'published' and group.parent_message_group_id is None
        assert len(adapter.replies) == 1 and adapter.replies[0][1] is None
    finally:
        db.close()


@pytest.mark.asyncio
async def test_phase4_reply_db_consistency_mirror_delivery_recorded(tmp_path):
    """A second nested reply stores its exact thread-scoped physical copy once."""
    db, runtime, gateway, adapter = await _published_post(tmp_path)
    try:
        await runtime.handle_normalized_message_create(reply(message='400', parent='300'), adapter=adapter)
        previous = db.discord_fanout_groups.get_message_group_by_source_message(400)
        await runtime.handle_normalized_message_create(reply(message='401', parent='400'), adapter=adapter)
        group = db.discord_fanout_groups.get_message_group_by_source_message(401)
        assert group is not None and group.parent_message_group_id == previous.id
        copies = db.discord_fanout_groups.get_message_deliveries(group.id)
        mirror = [copy for copy in copies if copy.role == 'mirror']
        assert len(mirror) == 1
        assert (mirror[0].external_thread_id, mirror[0].external_message_id) == ('1101', '14101')
        assert len(adapter.replies) == 2
        assert adapter.replies[1][1].external_message_id == '4101'
        assert (await runtime.handle_normalized_message_create(
            reply(message='401', parent='400'), adapter=adapter,
        )).status == 'ignored'
        assert len(adapter.replies) == 2
    finally:
        db.close()
