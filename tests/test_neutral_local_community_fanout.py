"""Real-SQLite scenarios for adapter-owned Local Community physical copies."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.local_communities.discord_fanout import LocalCommunityDiscordFanout
from src.local_communities.service import LocalCommunityService
from src.platform_types import (
    ChannelRef, ContentDocument, CreatedMessage, CreatedThread, MessageRef,
    ThreadRef,
)
from support.db import build_database
from support.fake_platform_adapter import FakeDiscordPort


class PhysicalPort(FakeDiscordPort):
    """Fake only the external network; let the real repositories own receipts."""

    def __init__(self, *, fail_forum=None, corrupt_forum=None):
        self.fail_forum = fail_forum
        self.corrupt_forum = corrupt_forum
        self.threads = []
        self.messages = []

    async def create_forum_thread(self, forum, content, *, title, presentation, attempt):
        """Return a distinct real-looking thread/starter pair for each forum."""
        self.threads.append((forum, content, attempt))
        if forum.external_channel_id == self.fail_forum:
            raise OSError('discord unavailable')
        forum_id = int(forum.external_channel_id)
        owner = forum if forum.external_channel_id != self.corrupt_forum else ChannelRef(
            'discord-primary', '999', 'forum', '10'
        )
        thread_id = forum_id + (5000 if forum_id == 100 else 1000)
        thread = ThreadRef(owner, str(thread_id))
        return CreatedThread(thread, MessageRef('discord-primary', str(thread_id), str(thread_id + 1000)), 'bot')

    async def send_message(self, destination, content, *, reply_to, presentation, attempt):
        """Record each physical parent ref and return its immediate-container receipt."""
        self.messages.append((destination, reply_to, content, attempt))
        if destination.channel.external_channel_id == self.fail_forum:
            raise OSError('discord unavailable')
        mid = int(destination.external_thread_id) + 4000 + 100 * (len(self.messages) // 3)
        if destination.channel.external_channel_id == self.corrupt_forum:
            return CreatedMessage(MessageRef('discord-primary', '99999', str(mid)), 'bot')
        return CreatedMessage(MessageRef('discord-primary', destination.external_thread_id, str(mid)), 'bot')


def world(tmp_path):
    """Create a host, two active subscribers and source post with SQLite surfaces."""
    db = build_database(tmp_path, 'typed-local-fanout.sqlite3')
    LocalCommunityService(
        database=db, base_url='https://bridge.example',
        keypair_generator=lambda: ('public', 'private'),
    ).create_local_community(
        discord_guild_id=10, discord_forum_channel_id=100, slug='hackers', name='Hackers',
        description='Community', created_by_discord_user_id='123',
    )
    community = db.local_communities.get_local_community_by_slug('hackers')
    subscribers = {}
    for forum in (200, 300):
        subscribers[forum] = db.local_subscribers.create_local_subscriber(
            local_community_id=community.id, discord_guild_id=10,
            discord_channel_id=forum, initiated_by_discord_user_id='123',
        )
    post = db.local_community_content.create_local_community_thread(
        local_community_id=community.id, discord_thread_id=1100,
        discord_starter_message_id=1101, ap_activity_id='https://bridge.example/create/1',
        ap_object_id='https://bridge.example/post/1', direction='discord_to_ap',
        origin_kind='discord_local',
    )
    policy = SimpleNamespace(is_discord_guild_allowed=lambda guild: guild == 10)
    fanout = LocalCommunityDiscordFanout(database=db, bot=None, policy_service=policy)
    return db, community, subscribers, post, fanout


@pytest.mark.asyncio
async def test_host_post_mirrors_to_two_subscribers_once(tmp_path):
    """A host post creates two independent physical surfaces and replays no POST."""
    db, community, subscribers, post, fanout = world(tmp_path)
    port = PhysicalPort()
    try:
        result = await fanout.fanout_normalized_thread(
            adapter=port, local_community=community, thread_row=post, title='Topic',
            content='hello', author_display_name='Alice', source_forum_channel_id=100,
            include_host=False,
        )
        assert (result.attempted, result.delivered) == (2, 2)
        assert [f.external_channel_id for f, _, _ in port.threads] == ['200', '300']
        assert port.threads[0][1] == ContentDocument('`Alice`\n\nhello')
        for forum in (200, 300):
            physical = db.local_community_surfaces.get_local_community_thread_surface(
                local_community_thread_id=post.id, discord_forum_channel_id=forum,
            )
            assert (int(physical.external_thread_id), int(physical.external_starter_message_id)) == (
                forum + 1000, forum + 2000
            )
            assert physical.local_subscriber_id == subscribers[forum].id
        again = await fanout.fanout_normalized_thread(
            adapter=port, local_community=community, thread_row=post, title='Topic',
            content='hello', author_display_name='Alice', source_forum_channel_id=100,
            include_host=False,
        )
        assert again.skipped_existing == 2
        assert len(port.threads) == 2
    finally:
        db.close()


@pytest.mark.asyncio
async def test_failure_or_mismatched_receipt_cannot_create_surface(tmp_path):
    """A bad target receipt is not persisted and cannot block a healthy sibling."""
    db, community, _, post, fanout = world(tmp_path)
    port = PhysicalPort(corrupt_forum='200')
    try:
        result = await fanout.fanout_normalized_thread(
            adapter=port, local_community=community, thread_row=post, title='Topic',
            content='body', author_display_name='Alice', source_forum_channel_id=100,
            include_host=False,
        )
        assert (result.failed, result.delivered) == (1, 1)
        assert db.local_community_surfaces.get_local_community_thread_surface(
            local_community_thread_id=post.id, discord_forum_channel_id=200,
        ) is None
        assert db.local_community_surfaces.get_local_community_thread_surface(
            local_community_thread_id=post.id, discord_forum_channel_id=300,
        ) is not None
    finally:
        db.close()


@pytest.mark.asyncio
async def test_nested_reply_uses_per_recipient_parent_and_skips_missing_parent(tmp_path):
    """A reply cannot be flattened to a starter when its nested parent is missing."""
    db, community, _, post, fanout = world(tmp_path)
    port = PhysicalPort()
    try:
        await fanout.fanout_normalized_thread(
            adapter=port, local_community=community, thread_row=post, title='Topic',
            content='body', author_display_name='Alice', source_forum_channel_id=100,
            include_host=False,
        )
        parent = db.local_community_content.create_local_community_message(
            local_community_thread_id=post.id, discord_message_id=1200,
            ap_activity_id='https://bridge.example/create/2',
            ap_object_id='https://bridge.example/comment/2',
            parent_ap_object_id=post.ap_object_id, parent_discord_message_id=1101,
            direction='discord_to_ap',
        )
        left = db.local_community_surfaces.get_local_community_thread_surface(
            local_community_thread_id=post.id, discord_forum_channel_id=200,
        )
        db.local_community_surfaces.create_local_community_message_surface(
            local_community_message_id=parent.id, local_community_thread_surface_id=left.id,
            discord_forum_channel_id=200, discord_message_id=8200,
            parent_discord_message_id=left.discord_starter_message_id,
            role='local_subscriber', local_subscriber_id=left.local_subscriber_id,
        )
        reply = db.local_community_content.create_local_community_message(
            local_community_thread_id=post.id, discord_message_id=1201,
            ap_activity_id='https://bridge.example/create/3',
            ap_object_id='https://bridge.example/comment/3',
            parent_ap_object_id=parent.ap_object_id, parent_discord_message_id=1200,
            direction='discord_to_ap',
        )
        result = await fanout.fanout_normalized_message(
            adapter=port, local_community=community, thread_row=post, message_row=reply,
            content='nested', author_display_name='Alice', source_forum_channel_id=100,
            include_host=False,
        )
        assert (result.delivered, result.skipped_missing_parent_surface) == (1, 1)
        assert [(target.channel.external_channel_id, parent.external_message_id) for target, parent, _, _ in port.messages] == [('200', '8200')]
        surface = db.local_community_surfaces.get_local_community_message_surface(
            local_community_message_id=reply.id, local_community_thread_surface_id=left.id,
        )
        assert surface.external_parent_message_id == '8200'
        again = await fanout.fanout_normalized_message(
            adapter=port, local_community=community, thread_row=post, message_row=reply,
            content='nested', author_display_name='Alice', source_forum_channel_id=100,
            include_host=False,
        )
        assert (again.skipped_existing, again.skipped_missing_parent_surface) == (1, 1)
        assert len(port.messages) == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_inactive_subscriber_is_not_selected(tmp_path):
    """An inactive forum must never be admitted as an outbound target."""
    db, community, subscribers, post, fanout = world(tmp_path)
    try:
        with db.session() as session:
            item = session.get(type(subscribers[200]), subscribers[200].id)
            item.status = 'removed'
        port = PhysicalPort()
        result = await fanout.fanout_normalized_thread(
            adapter=port, local_community=community, thread_row=post, title='Topic',
            content='body', author_display_name='Alice', source_forum_channel_id=100,
            include_host=False,
        )
        assert result.delivered == 1
        assert [f.external_channel_id for f, _, _ in port.threads] == ['300']
    finally:
        db.close()


@pytest.mark.asyncio
async def test_local_subscriber_source_mirrors_into_host_without_rewriting_source(tmp_path):
    """An authored subscriber forum post has the host as a distinct destination."""
    db, community, subscribers, _old_post, fanout = world(tmp_path)
    try:
        post = db.local_community_content.create_local_community_thread_canonical(
            local_community_id=community.id,
            ap_activity_id='https://bridge.example/create/new',
            ap_object_id='https://bridge.example/post/new',
            direction='discord_to_ap', origin_kind='discord_local_subscriber',
        )
        db.local_community_surfaces.create_local_community_thread_surface(
            local_community_thread_id=post.id,
            discord_forum_channel_id=200, discord_thread_id=1400,
            discord_starter_message_id=1401, role='local_subscriber',
            local_subscriber_id=subscribers[200].id,
        )
        port = PhysicalPort()
        result = await fanout.fanout_normalized_thread(
            adapter=port, local_community=community, thread_row=post,
            title='Subscriber Topic', content='body', author_display_name='Bob',
            source_forum_channel_id=200, include_host=True,
        )
        assert result.delivered == 2
        assert sorted(f.external_channel_id for f, _, _ in port.threads) == ['100', '300']
        host = db.local_community_surfaces.get_local_community_thread_surface(
            local_community_thread_id=post.id, discord_forum_channel_id=100,
        )
        assert host.role == 'host' and host.local_subscriber_id is None
        assert host.external_thread_id != host.external_starter_message_id
        source = db.local_community_surfaces.get_local_community_thread_surface(
            local_community_thread_id=post.id, discord_forum_channel_id=200,
        )
        assert (source.discord_thread_id, source.discord_starter_message_id) == (1400, 1401)
    finally:
        db.close()


@pytest.mark.asyncio
async def test_wrong_canonical_parent_rejected_before_physical_post(tmp_path):
    """An unrelated canonical message may not be sent into this community post."""
    db, community, _, post, fanout = world(tmp_path)
    port = PhysicalPort()
    try:
        second = db.local_community_content.create_local_community_thread_canonical(
            local_community_id=community.id,
            ap_activity_id='https://bridge.example/create/other',
            ap_object_id='https://bridge.example/post/other',
            direction='discord_to_ap', origin_kind='discord_local',
        )
        foreign_message = db.local_community_content.create_local_community_message_canonical(
            local_community_thread_id=second.id,
            ap_activity_id='https://bridge.example/create/comment/other',
            ap_object_id='https://bridge.example/comment/other',
            parent_ap_object_id=second.ap_object_id, direction='discord_to_ap',
        )
        from src.platform_types import PlatformOperationError
        with pytest.raises(PlatformOperationError, match='canonical local reply ownership'):
            await fanout.fanout_normalized_message(
                adapter=port, local_community=community, thread_row=post,
                message_row=foreign_message, content='attacker', author_display_name='Bob',
                source_forum_channel_id=100, include_host=False,
            )
        assert not port.messages
    finally:
        db.close()


@pytest.mark.asyncio
async def test_wrong_message_receipt_cannot_be_persisted(tmp_path):
    """A forged container receipt never creates a sibling message surface."""
    db, community, _, post, fanout = world(tmp_path)
    port = PhysicalPort(corrupt_forum='200')
    try:
        await fanout.fanout_normalized_thread(
            adapter=port, local_community=community, thread_row=post, title='Topic',
            content='body', author_display_name='Bob', source_forum_channel_id=100,
            include_host=False,
        )
        reply = db.local_community_content.create_local_community_message(
            local_community_thread_id=post.id, discord_message_id=1300,
            ap_activity_id='https://bridge.example/create/comment/7',
            ap_object_id='https://bridge.example/comment/7',
            parent_ap_object_id=post.ap_object_id,
            parent_discord_message_id=1101, direction='discord_to_ap',
        )
        result = await fanout.fanout_normalized_message(
            adapter=port, local_community=community, thread_row=post,
            message_row=reply, content='hello', author_display_name='Bob',
            source_forum_channel_id=100, include_host=False,
        )
        # Bad thread creation prevented the left message target entirely.
        assert (result.skipped_missing_thread_surface, result.delivered) == (1, 1)
        assert len(port.messages) == 1
        assert port.messages[0][0].channel.external_channel_id == '300'
    finally:
        db.close()


@pytest.mark.asyncio
async def test_network_failure_isolated_per_local_subscriber(tmp_path):
    """Unavailable subscriber does not stop delivery into an independent forum."""
    db, community, _, post, fanout = world(tmp_path)
    try:
        port = PhysicalPort(fail_forum='200')
        summary = await fanout.fanout_normalized_thread(
            adapter=port, local_community=community, thread_row=post,
            title='Topic', content='body', author_display_name='Bob',
            source_forum_channel_id=100, include_host=False,
        )
        assert (summary.failed, summary.delivered) == (1, 1)
        assert db.local_community_surfaces.get_local_community_thread_surface(
            local_community_thread_id=post.id, discord_forum_channel_id=200,
        ) is None
    finally:
        db.close()


@pytest.mark.asyncio
async def test_same_forum_wrong_guild_receipt_is_rejected(tmp_path):
    """Reference equality must not hide contradictory confirmed guild metadata."""
    db, community, _, post, fanout = world(tmp_path)

    class WrongGuild(PhysicalPort):
        """Produce a receipt for the same forum ID but a conflicting guild."""

        async def create_forum_thread(self, forum, content, *, title, presentation, attempt):
            """Return a forged physical parent with otherwise valid IDs."""
            self.threads.append((forum, content, attempt))
            observed = ChannelRef('discord-primary', forum.external_channel_id, 'forum', '999')
            tid = str(int(forum.external_channel_id) + 1000)
            return CreatedThread(
                ThreadRef(observed, tid),
                MessageRef('discord-primary', tid, str(int(tid) + 1000)), 'bot',
            )

    try:
        port = WrongGuild()
        summary = await fanout.fanout_normalized_thread(
            adapter=port, local_community=community, thread_row=post,
            title='Topic', content='body', author_display_name='Bob',
            source_forum_channel_id=100, include_host=False,
        )
        assert (summary.failed, summary.delivered) == (2, 0)
        for forum in (200, 300):
            assert db.local_community_surfaces.get_local_community_thread_surface(
                local_community_thread_id=post.id, discord_forum_channel_id=forum,
            ) is None
    finally:
        db.close()

@pytest.mark.asyncio
async def test_sibling_thread_mode_receipt_requires_requested_bot_mode(tmp_path):
    """A fabricated webhook result cannot claim the bot-created subscriber thread."""
    db, community, subscribers, post, fanout = world(tmp_path)

    class MismatchedMode(PhysicalPort):
        """Simulate a transport returning a different actual presentation mode."""

        async def create_forum_thread(self, forum, content, *, title, presentation, attempt):
            """Corrupt only the first subscriber's receipt after a physical send."""
            receipt = await super().create_forum_thread(
                forum, content, title=title, presentation=presentation, attempt=attempt,
            )
            if forum.external_channel_id == '200':
                return CreatedThread(receipt.thread, receipt.starter, 'webhook')
            return receipt

    try:
        port = MismatchedMode()
        result = await fanout.fanout_normalized_thread(
            adapter=port, local_community=community, thread_row=post,
            title='Topic', content='Body', author_display_name='Alice',
            source_forum_channel_id=100, include_host=False,
        )
        assert (result.attempted, result.delivered, result.failed) == (2, 1, 1)
        assert db.local_community_surfaces.get_local_community_thread_surface(
            local_community_thread_id=post.id, discord_forum_channel_id=200,
        ) is None
        valid = db.local_community_surfaces.get_local_community_thread_surface(
            local_community_thread_id=post.id, discord_forum_channel_id=300,
        )
        assert valid is not None and valid.external_starter_message_id == '2300'
    finally:
        db.close()


@pytest.mark.asyncio
async def test_sibling_comment_mode_receipt_requires_requested_bot_mode(tmp_path):
    """A mismatched reply mode cannot claim a copy while another target succeeds."""
    db, community, _, post, fanout = world(tmp_path)

    class MismatchedMode(PhysicalPort):
        """Return an incorrectly declared delivery mode for one sent comment."""

        async def send_message(self, destination, content, *, reply_to, presentation, attempt):
            """Keep physical container IDs genuine so mode is the sole failure."""
            receipt = await super().send_message(
                destination, content, reply_to=reply_to,
                presentation=presentation, attempt=attempt,
            )
            if destination.channel.external_channel_id == '200':
                return CreatedMessage(receipt.message, 'webhook')
            return receipt

    try:
        await fanout.fanout_normalized_thread(
            adapter=PhysicalPort(), local_community=community, thread_row=post,
            title='Topic', content='Body', author_display_name='Alice',
            source_forum_channel_id=100, include_host=False,
        )
        comment = db.local_community_content.create_local_community_message(
            local_community_thread_id=post.id, discord_message_id=1200,
            ap_activity_id='https://bridge.example/create/comment/44',
            ap_object_id='https://bridge.example/comment/44',
            parent_ap_object_id=post.ap_object_id, parent_discord_message_id=1101,
            direction='discord_to_ap',
        )
        port = MismatchedMode()
        result = await fanout.fanout_normalized_message(
            adapter=port, local_community=community, thread_row=post,
            message_row=comment, content='reply', author_display_name='Alice',
            source_forum_channel_id=100, include_host=False,
        )
        assert (result.attempted, result.delivered, result.failed) == (2, 1, 1)
        target200 = db.local_community_surfaces.get_local_community_thread_surface(
            local_community_thread_id=post.id, discord_forum_channel_id=200,
        )
        target300 = db.local_community_surfaces.get_local_community_thread_surface(
            local_community_thread_id=post.id, discord_forum_channel_id=300,
        )
        assert db.local_community_surfaces.get_local_community_message_surface(
            local_community_message_id=comment.id,
            local_community_thread_surface_id=target200.id,
        ) is None
        valid = db.local_community_surfaces.get_local_community_message_surface(
            local_community_message_id=comment.id,
            local_community_thread_surface_id=target300.id,
        )
        assert valid is not None
        assert valid.external_parent_message_id == target300.external_starter_message_id
    finally:
        db.close()
