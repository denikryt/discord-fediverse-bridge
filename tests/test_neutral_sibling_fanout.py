"""local subscriber synchronization neutral sibling fanout exercises real subscription SQLite routing."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.community_sync.discord_fanout import DiscordFanout
from src.platform_types import (
    AuthorRef, ChannelRef, ConnectorRef, ContentDocument, CreateAttempt,
    CreatedMessage, CreatedThread, DeliveryPresentation, MessageCreated, MessageRef,
    MessageView, PlatformOperationError, ThreadCreated, ThreadRef,
)
from support.db import COMMUNITY_ACTOR_URL, add_accepted_subscription, build_database


class PhysicalAdapter:
    """Observe exact neutral transport requests, including physical failures."""

    def __init__(self, *, fail_channel=None):
        """Construct one verified connector with optional one-target failure."""
        self.connector = ConnectorRef('discord-primary', 'discord')
        self.fail_channel = fail_channel
        self.threads = []
        self.messages = []

    async def create_forum_thread(self, forum, content, *, title, presentation, attempt):
        """Record requested destination and report distinct thread/starter IDs."""
        self.threads.append((forum, content, title, presentation, attempt))
        if forum.external_channel_id == self.fail_channel:
            raise OSError('one forum failed')
        n = int(forum.external_channel_id)
        return CreatedThread(ThreadRef(forum, str(n+1000)),
                             MessageRef('discord-primary', str(n+1000), str(n+2000)), 'bot')

    async def send_message(self, destination, content, *, reply_to, presentation, attempt):
        """Record a single message write and preserve its per-target reference."""
        self.messages.append((destination, content, reply_to, presentation, attempt))
        if destination.channel.external_channel_id == self.fail_channel:
            raise OSError('one thread failed')
        return CreatedMessage(MessageRef('discord-primary', destination.external_thread_id,
                                         str(int(destination.external_thread_id)+2000)), 'bot')


def world(tmp_path, *, active=(101, 102), wrong_community=()):
    """Create actual persisted subscription ownership for selected sibling forums."""
    db = build_database(tmp_path, 'neutral-sibling.sqlite3')
    for channel in active:
        add_accepted_subscription(db, channel_id=channel)
    for channel in wrong_community:
        add_accepted_subscription(db, channel_id=channel,
                                  community_actor_url='https://other.example/c/foreign')
    policy = SimpleNamespace(is_discord_guild_allowed=lambda guild_id: guild_id == 1)
    fanout = DiscordFanout(bot=SimpleNamespace(), mutation_tracker=SimpleNamespace(),
                           database=db, policy_service=policy)
    return db, fanout


def post_event():
    """Build a verified source forum starter with distinct starter and thread IDs."""
    channel = ChannelRef('discord-primary', '100', 'forum', '1')
    thread = ThreadRef(channel, '200')
    starter = MessageRef('discord-primary', '200', '301')
    return ThreadCreated('discord-primary', thread, 'Subject', starter=starter,
                         external_guild_id='1'), MessageView(
                             starter, AuthorRef('discord-primary', '123', 'human', 'Alice'),
                             ContentDocument('Body'))


def reply_event():
    """Build a source reply whose parent must be translated per destination."""
    forum = ChannelRef('discord-primary', '100', 'forum', '1')
    return MessageCreated('discord-primary', MessageRef('discord-primary', '200', '302'),
                          AuthorRef('discord-primary', '123', 'human', 'Alice'),
                          ContentDocument('Reply'), reply_to=MessageRef('discord-primary', '200', '301'),
                          parent_channel=forum, thread_context=ThreadRef(forum, '200'),
                          external_guild_id='1')


@pytest.mark.asyncio
async def test_verified_source_fans_out_to_each_accepted_sibling_with_true_receipts(tmp_path):
    """One source makes two independent mirror posts with true starter IDs."""
    db, fanout = world(tmp_path)
    try:
        adapter = PhysicalAdapter()
        created, starter = post_event()
        copies = await fanout.mirror_normalized_thread_to_siblings(
            adapter=adapter, source=created, starter=starter,
            community_actor_id=COMMUNITY_ACTOR_URL, sibling_channel_ids=[101,102])
        assert [(v.channel_id, v.thread_id, v.starter_message_id) for v in copies] == [
            (101,1101,2101),(102,1102,2102)]
        assert all(item[1].body == '`Alice`\n\nBody' for item in adapter.threads)
        assert all(item[4].operation == 'forum_thread' for item in adapter.threads)
        assert adapter.threads[0][4].key != adapter.threads[1][4].key
    finally:
        db.close()


@pytest.mark.asyncio
async def test_invalid_or_unsubscribed_targets_cannot_receive_mirror(tmp_path):
    """Foreign-community, missing, pending and duplicate targets cannot receive a POST."""
    db, fanout = world(tmp_path, active=(101,), wrong_community=(103,))
    try:
        adapter = PhysicalAdapter()
        created, starter = post_event()
        copies = await fanout.mirror_normalized_thread_to_siblings(
            adapter=adapter, source=created, starter=starter,
            community_actor_id=COMMUNITY_ACTOR_URL,
            sibling_channel_ids=[101,101,103,104])
        assert len(copies) == 1 and len(adapter.threads) == 1
        assert adapter.threads[0][0].external_channel_id == '101'
    finally:
        db.close()


@pytest.mark.asyncio
async def test_one_sibling_failure_does_not_block_other(tmp_path):
    """Failed physical create leaves healthy sibling receipt observable."""
    db, fanout = world(tmp_path)
    try:
        adapter = PhysicalAdapter(fail_channel='101')
        created, starter = post_event()
        copies = await fanout.mirror_normalized_thread_to_siblings(
            adapter=adapter, source=created, starter=starter,
            community_actor_id=COMMUNITY_ACTOR_URL, sibling_channel_ids=[101,102])
        assert [v.channel_id for v in copies] == [102]
        assert len(adapter.threads) == 2
    finally:
        db.close()


@pytest.mark.asyncio
async def test_reply_mirrors_preserve_destination_specific_reference(tmp_path):
    """Replies retain per-sibling parent IDs and deterministic physical attempts."""
    db, fanout = world(tmp_path)
    try:
        adapter = PhysicalAdapter()
        deliveries = [SimpleNamespace(discord_channel_id=101,discord_thread_id=1101),
                      SimpleNamespace(discord_channel_id=102,discord_thread_id=1102)]
        context = SimpleNamespace(get_reference_for_thread=lambda thread_id: {1101:2101,1102:2102}[thread_id])
        copies = await fanout.mirror_normalized_message_to_siblings(
            adapter=adapter, source=reply_event(),
            community_actor_id=COMMUNITY_ACTOR_URL,
            sibling_thread_deliveries=deliveries, reply_context=context)
        assert [(r.thread_id,r.parent_message_id) for r in copies] == [(1101,2101),(1102,2102)]
        assert [v[2].external_message_id for v in adapter.messages] == ['2101','2102']
        assert all(v[1].body == '`Alice`\n\nReply' for v in adapter.messages)
    finally:
        db.close()


@pytest.mark.asyncio
async def test_cross_connector_source_is_rejected_before_physical_write(tmp_path):
    """Connector mismatch cannot be smuggled through an integer Discord ID."""
    db, fanout = world(tmp_path)
    try:
        adapter = PhysicalAdapter()
        created, starter = post_event()
        foreign_forum = ChannelRef('fluxer-a', '100', 'forum', '1')
        foreign_thread = ThreadRef(foreign_forum, '200')
        foreign_starter = MessageRef('fluxer-a', '200', '301')
        foreign = ThreadCreated('fluxer-a', foreign_thread, 'Subject',
                                starter=foreign_starter, external_guild_id='1')
        starter = MessageView(foreign_starter,
                              AuthorRef('fluxer-a', '123', 'human', 'Alice'),
                              ContentDocument('Body'))
        with pytest.raises(PlatformOperationError):
            await fanout.mirror_normalized_thread_to_siblings(
                adapter=adapter, source=foreign, starter=starter,
                community_actor_id=COMMUNITY_ACTOR_URL, sibling_channel_ids=[101])
        assert adapter.threads == []
    finally:
        db.close()


@pytest.mark.asyncio
async def test_revoked_subscription_blocks_new_mirror_send(tmp_path):
    """An Undo(Follow)-like revoked subscription cannot authorize further writes."""
    db, fanout = world(tmp_path)
    try:
        db.remote_subscriptions.update_subscription_follow_state(
            discord_channel_id=101, community_inbox_url=None,
            follow_activity_id=None, status='pending')
        adapter = PhysicalAdapter()
        created, starter = post_event()
        copies = await fanout.mirror_normalized_thread_to_siblings(
            adapter=adapter, source=created, starter=starter,
            community_actor_id=COMMUNITY_ACTOR_URL, sibling_channel_ids=[101])
        assert copies == [] and adapter.threads == []
    finally:
        db.close()


@pytest.mark.asyncio
async def test_wrong_physical_receipt_is_not_persistable(tmp_path):
    """An unexpected physical parent/guild must not become a mirror mapping."""
    db, fanout = world(tmp_path, active=(101,))
    try:
        class WrongReceiptAdapter(PhysicalAdapter):
            """Return a mismatched physical forum parent after a POST."""

            async def create_forum_thread(self, forum, content, *, title, presentation, attempt):
                """Return an apparently valid thread ID under a foreign guild."""
                self.threads.append((forum, content, title, presentation, attempt))
                wrong = ChannelRef('discord-primary', forum.external_channel_id, 'forum', '9')
                return CreatedThread(ThreadRef(wrong, '1101'),
                                     MessageRef('discord-primary', '1101', '2101'), 'bot')

        adapter = WrongReceiptAdapter()
        created, starter = post_event()
        copies = await fanout.mirror_normalized_thread_to_siblings(
            adapter=adapter, source=created, starter=starter,
            community_actor_id=COMMUNITY_ACTOR_URL, sibling_channel_ids=[101])
        assert copies == [] and len(adapter.threads) == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_invalid_destination_reply_reference_does_not_send(tmp_path):
    """An unmappable zero-valued reply reference may not reach physical Discord I/O."""
    db, fanout = world(tmp_path, active=(101,))
    try:
        adapter = PhysicalAdapter()
        delivery = SimpleNamespace(discord_channel_id=101, discord_thread_id=1101)
        context = SimpleNamespace(get_reference_for_thread=lambda _: 0)
        assert await fanout.mirror_normalized_message_to_siblings(
            adapter=adapter, source=reply_event(),
            community_actor_id=COMMUNITY_ACTOR_URL,
            sibling_thread_deliveries=[delivery], reply_context=context) == []
        assert adapter.messages == []
    finally:
        db.close()
