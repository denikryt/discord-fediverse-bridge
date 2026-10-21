"""Real SQLite sibling reply mapping through normalized source messages."""

from __future__ import annotations

from dataclasses import replace

import pytest

from src.community_sync.reply_mapping import resolve_normalized_reply_context
from src.platform_types import (
    AuthorRef, ChannelRef, ContentDocument, MessageCreated, MessageRef,
    PlatformOperationError, ThreadRef,
)
from support.db import (
    add_thread_delivery, build_database, create_source_thread_group,
)


def event(*, reference=None, thread_id='200', connector='discord-primary'):
    """Describe a physical source reply with an optional referenced message."""
    forum = ChannelRef(connector, '100', 'forum', '1')
    thread = ThreadRef(forum, thread_id)
    return MessageCreated(
        connector, MessageRef(connector, thread_id, '600'),
        AuthorRef(connector, '123', 'human', 'Alice'), ContentDocument('Nested'),
        reply_to=(MessageRef(connector, thread_id, str(reference)) if reference is not None else None),
        parent_channel=forum, thread_context=thread, external_guild_id='1',
    )


def world(tmp_path):
    """Save two physical copies and one canonical comment across them."""
    db = build_database(tmp_path, 'normalized-reply.sqlite3')
    group = create_source_thread_group(db, thread_id=200, starter_message_id=300)
    add_thread_delivery(db, thread_group_id=group.id, channel_id=101,
                        thread_id=201, starter_message_id=301, role='mirror')
    group_deliveries = db.discord_fanout_groups.get_thread_deliveries(group.id)
    mirror = [d for d in group_deliveries if d.discord_thread_id == 201]
    parent = db.discord_fanout_groups.create_message_group(
        community_actor_id=group.community_actor_id, thread_group_id=group.id,
        source_channel_id=100, source_thread_id=200, source_message_id=500,
        ap_object_id='https://bridge.example/objects/parent')
    db.discord_fanout_groups.add_message_delivery(
        message_group_id=parent.id, discord_channel_id=100,
        discord_thread_id=200, discord_message_id=500, role='source')
    db.discord_fanout_groups.add_message_delivery(
        message_group_id=parent.id, discord_channel_id=101,
        discord_thread_id=201, discord_message_id=501, role='mirror')
    return db, group, mirror, parent


@pytest.mark.asyncio
async def test_typed_reply_to_existing_message_uses_sibling_copy(tmp_path):
    """Parent group FK and target message reference come from durable mappings."""
    db, group, mirror, parent = world(tmp_path)
    try:
        result = resolve_normalized_reply_context(db, event(reference=500), group, mirror)
        assert result.parent_message_group_id == parent.id
        assert result.get_reference_for_thread(201) == 501
    finally:
        db.close()


def test_typed_reply_to_starter_uses_each_destination_starter(tmp_path):
    """Reply to the post references sibling starter, not its thread ID."""
    db, group, mirror, _ = world(tmp_path)
    try:
        result = resolve_normalized_reply_context(db, event(reference=300), group, mirror)
        assert result.parent_message_group_id is None
        assert result.get_reference_for_thread(201) == 301
    finally:
        db.close()


def test_unknown_or_absent_typed_parent_falls_back_to_flat_send(tmp_path):
    """Unknown parent cannot be invented or silently mapped to unrelated AP objects."""
    db, group, mirror, _ = world(tmp_path)
    try:
        # 301 is another physical thread's starter; it cannot be the source's root.
        for source in (event(reference=999), event(reference=301), event(reference=501), event()):
            result = resolve_normalized_reply_context(db, source, group, mirror)
            assert result.parent_message_group_id is None
            assert result.get_reference_for_thread(201) is None
    finally:
        db.close()


def test_foreign_canonical_group_never_binds_typed_parent(tmp_path):
    """A copied message from another group cannot become this post's AP parent."""
    db, group, mirror, _ = world(tmp_path)
    try:
        foreign = create_source_thread_group(db, channel_id=103, thread_id=203,
                                             starter_message_id=303,
                                             ap_object_id='https://remote.example/post/other',
                                             ap_activity_id='https://remote.example/activity/other')
        foreign_parent = db.discord_fanout_groups.create_message_group(
            community_actor_id=foreign.community_actor_id, thread_group_id=foreign.id,
            source_channel_id=103, source_thread_id=203, source_message_id=700,
            ap_object_id='https://bridge.example/objects/foreign')
        db.discord_fanout_groups.add_message_delivery(
            message_group_id=foreign_parent.id, discord_channel_id=103,
            discord_thread_id=203, discord_message_id=700, role='source')
        result = resolve_normalized_reply_context(db, event(reference=700), group, mirror)
        assert result.parent_message_group_id is None
        assert result.get_reference_for_thread(201) is None
    finally:
        db.close()


@pytest.mark.parametrize('bad', ['other_container', 'other_connector', 'other_thread'])
def test_typed_reply_identity_mismatch_fails_before_mapping(tmp_path, bad):
    """Forged immediate container/connector and unrelated source thread fail closed."""
    db, group, mirror, _ = world(tmp_path)
    try:
        e = event(reference=500)
        if bad == 'other_container':
            e = replace(e, reply_to=MessageRef('discord-primary', '202', '500'))
        elif bad == 'other_connector':
            e = event(reference=500, connector='fluxer-a')
        else:
            e = event(reference=500, thread_id='202')
        with pytest.raises(PlatformOperationError):
            resolve_normalized_reply_context(db, e, group, mirror)
    finally:
        db.close()
