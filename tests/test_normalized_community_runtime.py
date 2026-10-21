"""Scenario coverage for canonical Community runtime with neutral Discord inputs.

Only the physical platform transport and federation HTTP boundary are faked;
subscription ownership, thread/message groups and delivery rows use SQLite.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.community_sync.discord_fanout import DiscordFanout
from src.community_sync.runtime import CommunityRuntime
from src.content_publish_service import ContentPublishService
from src.fedify_gateway_client import PublishContentResult
from src.platform_types import (
    AuthorRef, ChannelRef, ConnectorRef, ContentDocument, CreatedMessage,
    CreatedThread, MessageCreated, MessageRef, MessageView, ThreadCreated, ThreadRef,
    PlatformOperationError,
)
from support.db import COMMUNITY_ACTOR_URL, add_accepted_subscription, add_registered_user, build_database


class RecordingGateway:
    """Emulate confirmed AP publishes and expose the exact parent chosen by runtime."""

    def __init__(self):
        self.requests = []

    async def publish_content(self, request):
        """Return deterministic receipts while observing actual publish requests."""
        self.requests.append(request)
        n = len(self.requests)
        return PublishContentResult(
            activity_id=f'https://bridge.example/actors/users/alice/activities/{n}',
            object_id=f'https://bridge.example/objects/{n}',
            community_actor_url=COMMUNITY_ACTOR_URL,
        )


class PhysicalTransport:
    """Record forum/reply writes with distinct confirmed thread and starter IDs."""

    def __init__(self, *, fail_forum=None):
        self.connector = ConnectorRef('discord-primary', 'discord')
        self.fail_forum = fail_forum
        self.posts = []
        self.replies = []

    async def create_forum_thread(self, forum, content, *, title, presentation, attempt):
        """Create a distinct physical copy or fail only the nominated target."""
        self.posts.append((forum, content, attempt))
        if forum.external_channel_id == self.fail_forum:
            raise OSError('destination unavailable')
        n = int(forum.external_channel_id)
        return CreatedThread(ThreadRef(forum, str(n + 1000)),
                             MessageRef('discord-primary', str(n + 1000), str(n + 2000)), 'bot')

    async def send_message(self, destination, content, *, reply_to, presentation, attempt):
        """Confirm the requested sibling reference and actual destination ID."""
        self.replies.append((destination, reply_to, attempt))
        n = int(destination.external_thread_id)
        # Every new source message gets its own physical receipt; different
        # recipients of that source share a logical generation, not an ID.
        generation = (len(self.replies) - 1) // 2
        return CreatedMessage(MessageRef('discord-primary', str(n),
                                         str(n + 3000 + generation * 10000)), 'bot')


def world(tmp_path, *, fail_forum=None):
    """Configure authorized Discord forums and real persisted canonical mappings."""
    db = build_database(tmp_path, 'canonical-neutral.sqlite3')
    add_registered_user(db)
    for channel_id in (100, 101, 102):
        add_accepted_subscription(db, channel_id=channel_id)
    gateway = RecordingGateway()
    policy = SimpleNamespace(
        federation_decision=lambda actor: SimpleNamespace(allowed=True),
        is_discord_guild_allowed=lambda guild: guild == 1,
    )
    content = ContentPublishService(database=db, fedify_gateway=gateway,
                                    bridge_prefix='[bridge]', bridge_policy_service=policy)
    fanout = DiscordFanout(bot=SimpleNamespace(), mutation_tracker=SimpleNamespace(),
                           database=db, policy_service=policy)
    runtime = CommunityRuntime(database=db, content_publish_service=content, discord_fanout=fanout)
    adapter = PhysicalTransport(fail_forum=fail_forum)
    return db, runtime, gateway, adapter


def starter(*, forum_id='100', guild='1'):
    """Supply an independently verified forum thread and its real starter message."""
    forum = ChannelRef('discord-primary', forum_id, 'forum', guild)
    thread = ThreadRef(forum, '200')
    opening = MessageRef('discord-primary', '200', '300')
    event = ThreadCreated('discord-primary', thread, 'Subject', starter=opening,
                          external_guild_id=guild)
    observation = MessageView(opening, AuthorRef('discord-primary', '123', 'human', 'Alice'),
                              ContentDocument('Post body'))
    return event, observation


def reply(*, forum='100', thread='200', message='400', parent='300'):
    """Create one reply in the current physical forum with explicit parent ID."""
    channel = ChannelRef('discord-primary', forum, 'forum', '1')
    return MessageCreated('discord-primary', MessageRef('discord-primary', thread, message),
                          AuthorRef('discord-primary', '123', 'human', 'Alice'),
                          ContentDocument('Reply body'),
                          parent_channel=channel, thread_context=ThreadRef(channel, thread),
                          external_guild_id='1',
                          reply_to=MessageRef('discord-primary', thread, parent) if parent else None)


@pytest.mark.asyncio
async def test_normalized_starter_creates_one_source_and_independent_siblings(tmp_path):
    """One verified post publishes once, persists exact starter and sibling receipts."""
    db, runtime, gateway, adapter = world(tmp_path)
    try:
        event, opening = starter()
        first = await runtime.handle_normalized_thread_create(event, opening, adapter=adapter)
        second = await runtime.handle_normalized_thread_create(event, opening, adapter=adapter)
        assert first.status == 'published'
        assert second.reason == 'duplicate_discord_thread'
        assert len(gateway.requests) == 1 and len(adapter.posts) == 2
        group = db.discord_fanout_groups.get_thread_group_by_source_thread(200)
        deliveries = db.discord_fanout_groups.get_thread_deliveries(group.id)
        assert sorted((d.discord_channel_id, d.discord_thread_id,
                       d.discord_starter_message_id, d.role) for d in deliveries) == [
                           (100, 200, 300, 'source'), (101, 1101, 2101, 'mirror'),
                           (102, 1102, 2102, 'mirror')]
    finally:
        db.close()


@pytest.mark.asyncio
async def test_revoked_subscription_prevents_canonical_post_and_mirror(tmp_path):
    """Denied source subscription creates no AP activity, SQLite group, or POST."""
    db, runtime, gateway, adapter = world(tmp_path)
    try:
        event, opening = starter(forum_id='999')
        result = await runtime.handle_normalized_thread_create(event, opening, adapter=adapter)
        assert result.status == 'ignored'
        assert len(gateway.requests) == len(adapter.posts) == 0
        assert db.discord_fanout_groups.get_thread_group_by_source_thread(200) is None
    finally:
        db.close()


@pytest.mark.asyncio
async def test_normalized_reply_maps_starter_and_nested_parent_per_sibling(tmp_path):
    """Reply chaining persists SQLite group FK and sibling-specific reply references."""
    db, runtime, gateway, adapter = world(tmp_path)
    try:
        event, opening = starter()
        await runtime.handle_normalized_thread_create(event, opening, adapter=adapter)
        first = await runtime.handle_normalized_message_create(reply(), adapter=adapter)
        assert first.status == 'published'
        group = db.discord_fanout_groups.get_message_group_by_source_message(400)
        assert group.parent_message_group_id is None
        assert [r[1].external_message_id for r in adapter.replies] == ['2101', '2102']
        assert len(gateway.requests) == 2
        assert gateway.requests[1].in_reply_to_object_id == 'https://bridge.example/objects/1'
        # A nested local reply resolves the newly-persisted canonical parent.
        second = await runtime.handle_normalized_message_create(
            reply(message='401', parent='400'), adapter=adapter)
        assert second.status == 'published'
        nested = db.discord_fanout_groups.get_message_group_by_source_message(401)
        assert nested.parent_message_group_id == group.id
        assert [r[1].external_message_id for r in adapter.replies[2:]] == ['4101', '4102']
        assert gateway.requests[2].in_reply_to_object_id == 'https://bridge.example/objects/2'
    finally:
        db.close()


@pytest.mark.asyncio
async def test_duplicate_and_foreign_parent_reply_have_no_untrusted_side_effects(tmp_path):
    """Deduplication survives gateway replay and foreign physical references fail closed."""
    db, runtime, gateway, adapter = world(tmp_path)
    try:
        event, opening = starter()
        await runtime.handle_normalized_thread_create(event, opening, adapter=adapter)
        good = reply()
        await runtime.handle_normalized_message_create(good, adapter=adapter)
        dup = await runtime.handle_normalized_message_create(good, adapter=adapter)
        assert dup.reason == 'duplicate_discord_message'
        foreign = reply(message='402', parent='300')
        # The source thread is absent from a different forum even with the same IDs.
        from dataclasses import replace
        foreign = replace(foreign, parent_channel=ChannelRef('discord-primary', '102', 'forum', '1'),
                          thread_context=ThreadRef(ChannelRef('discord-primary', '102', 'forum', '1'), '200'))
        with pytest.raises(PlatformOperationError):
            await runtime.handle_normalized_message_create(foreign, adapter=adapter)
        assert len(gateway.requests) == 2 and len(adapter.replies) == 2
    finally:
        db.close()


@pytest.mark.asyncio
async def test_failed_sibling_does_not_block_healthy_target(tmp_path):
    """A physical failure in one mirror does not discard a published source."""
    db, runtime, gateway, adapter = world(tmp_path, fail_forum='101')
    try:
        event, opening = starter()
        result = await runtime.handle_normalized_thread_create(event, opening, adapter=adapter)
        assert result.status == 'published' and len(gateway.requests) == 1
        group = db.discord_fanout_groups.get_thread_group_by_source_thread(200)
        delivered = db.discord_fanout_groups.get_thread_deliveries(group.id)
        assert sorted(d.discord_channel_id for d in delivered) == [100, 102]
    finally:
        db.close()

@pytest.mark.asyncio
async def test_human_reply_originating_in_mirror_is_a_new_canonical_comment(tmp_path):
    """A human in a mirror forum creates one AP comment and copies to other forums."""
    db, runtime, gateway, adapter = world(tmp_path)
    try:
        event, opening = starter()
        await runtime.handle_normalized_thread_create(event, opening, adapter=adapter)
        mirror_reply = reply(forum='101', thread='1101', message='700', parent='2101')
        created = await runtime.handle_normalized_message_create(mirror_reply, adapter=adapter)
        assert created.status == 'published'
        assert len(gateway.requests) == 2
        group = db.discord_fanout_groups.get_message_group_by_source_message(700)
        deliveries = db.discord_fanout_groups.get_message_deliveries(group.id)
        assert sorted((d.discord_channel_id, d.discord_thread_id)
                      for d in deliveries) == [(100, 200), (101, 1101), (102, 1102)]
        assert [r[1].external_message_id for r in adapter.replies] == ['300', '2102']
        # The previously-created bot mirror message must not be republished.
        mirrored = reply(forum='100', thread='200', message='3200', parent='300')
        replay = await runtime.handle_normalized_message_create(mirrored, adapter=adapter)
        assert replay.reason == 'duplicate_discord_message'
        assert len(gateway.requests) == 2
    finally:
        db.close()


@pytest.mark.asyncio
async def test_guild_mismatch_fails_before_federation_or_mirroring(tmp_path):
    """Numeric channel matches cannot override the persisted subscription guild."""
    db, runtime, gateway, adapter = world(tmp_path)
    try:
        event, opening = starter(guild='2')
        result = await runtime.handle_normalized_thread_create(event, opening, adapter=adapter)
        assert result.status == 'ignored'
        assert not gateway.requests and not adapter.posts
    finally:
        db.close()


@pytest.mark.asyncio
async def test_unregistered_source_author_cannot_publish_or_create_group(tmp_path):
    """Community author admission runs before AP/SQLite source ownership effects."""
    db, runtime, gateway, adapter = world(tmp_path)
    try:
        event, opening = starter()
        invalid = MessageView(opening.message,
                              AuthorRef('discord-primary', '999', 'human', 'Unknown'),
                              opening.content)
        result = await runtime.handle_normalized_thread_create(event, invalid, adapter=adapter)
        assert result.status == 'ignored' and result.reason == 'unregistered_user'
        assert len(gateway.requests) == len(adapter.posts) == 0
        assert db.discord_fanout_groups.get_thread_group_by_any_thread(200) is None
    finally:
        db.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('reported_mode,expected_channels', [
    ('bot', [100, 101, 102]),
    ('webhook', [100, 102]),
])
async def test_remote_community_thread_receipt_mode_controls_durable_mirror(tmp_path, reported_mode, expected_channels):
    """Only a true bot Create receipt may become a Remote Community mirror copy."""
    db, runtime, gateway, adapter = world(tmp_path)
    original_create = adapter.create_forum_thread

    async def returned_thread(forum, content, *, title, presentation, attempt):
        """Simulate an incorrect physical mode while leaving the forum IDs genuine."""
        receipt = await original_create(
            forum, content, title=title, presentation=presentation, attempt=attempt,
        )
        if forum.external_channel_id == '101':
            return CreatedThread(receipt.thread, receipt.starter, reported_mode)
        return receipt

    adapter.create_forum_thread = returned_thread
    try:
        event, opening = starter()
        result = await runtime.handle_normalized_thread_create(event, opening, adapter=adapter)
        assert result.status == 'published'
        assert len(gateway.requests) == 1 and len(adapter.posts) == 2
        group = db.discord_fanout_groups.get_thread_group_by_source_thread(200)
        deliveries = db.discord_fanout_groups.get_thread_deliveries(group.id)
        assert sorted(row.discord_channel_id for row in deliveries) == expected_channels
        assert next(row.discord_starter_message_id for row in deliveries
                    if row.discord_channel_id == 102) == 2102
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('reported_mode,expected_channels', [
    ('bot', [100, 101, 102]),
    ('webhook', [100, 102]),
])
async def test_remote_community_reply_receipt_mode_controls_durable_mirror(tmp_path, reported_mode, expected_channels):
    """A mirror reply retains its verified parent but never adopts a wrong-mode receipt."""
    db, runtime, gateway, adapter = world(tmp_path)
    try:
        event, opening = starter()
        await runtime.handle_normalized_thread_create(event, opening, adapter=adapter)
        original_send = adapter.send_message

        async def returned_reply(destination, content, *, reply_to, presentation, attempt):
            """Misreport one sibling reply mode without changing physical IDs."""
            receipt = await original_send(
                destination, content, reply_to=reply_to,
                presentation=presentation, attempt=attempt,
            )
            if destination.channel.external_channel_id == '101':
                return CreatedMessage(receipt.message, reported_mode)
            return receipt

        adapter.send_message = returned_reply
        result = await runtime.handle_normalized_message_create(reply(), adapter=adapter)
        assert result.status == 'published'
        assert len(gateway.requests) == 2 and len(adapter.replies) == 2
        group = db.discord_fanout_groups.get_message_group_by_source_message(400)
        deliveries = db.discord_fanout_groups.get_message_deliveries(group.id)
        assert sorted(row.discord_channel_id for row in deliveries) == expected_channels
        assert next(row.discord_message_id for row in deliveries
                    if row.discord_channel_id == 102) == 4102
        assert adapter.replies[1][1].external_message_id == '2102'
    finally:
        db.close()
