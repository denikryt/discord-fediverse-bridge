"""Stateful SDK-boundary scenarios for Stage 03 physical DiscordAdapter operations."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from src.discord_adapter import DiscordAdapter
from src.platform_types import (
    AttachmentRef, ChannelRef, ConnectorRef, ContentDocument, CreateAttempt,
    CreatedThread, DeliveryPresentation, MessageRef, PlatformOperationError, ThreadRef,
)


class FakeChannel:
    """Represent a guild text channel with authoritative ACL and messages."""

    def __init__(self, channel_id=100, *, guild_id=10, is_news=False):
        """Construct observable physical guild, permissions and message state."""
        self.id = channel_id
        self.guild = SimpleNamespace(id=guild_id, get_member=lambda member_id: object())
        self.name = 'source'
        self._is_news = is_news
        self._messages = {}
        self.sends = []
        self.can_send = True

    def is_news(self):
        """Report Discord announcement classification for routing."""
        return self._is_news

    def permissions_for(self, member):
        """Report explicit bot permissions for the actual destination."""
        return SimpleNamespace(send_messages=self.can_send,
                               send_messages_in_threads=self.can_send,
                               create_public_threads=self.can_send,
                               manage_messages=self.can_send)

    async def fetch_message(self, message_id):
        """Resolve only actual messages in the requested immediate container."""
        try:
            return self._messages[message_id]
        except KeyError:
            raise NotFound()

    async def send(self, **kwargs):
        """Record one physical POST and return a message in this container."""
        self.sends.append(kwargs)
        message = FakeMessage(500 + len(self.sends), self)
        self._messages[message.id] = message
        return message


class FakeForum(FakeChannel):
    """Represent Discord forum creation with separate starter and thread IDs."""

    def __init__(self, channel_id=300, *, guild_id=10):
        """Prepare a physical forum with no required tags."""
        super().__init__(channel_id, guild_id=guild_id)
        self.flags = SimpleNamespace(require_tag=False)

    async def create_thread(self, **kwargs):
        """Return physically distinct thread and starter IDs."""
        self.sends.append(kwargs)
        thread = FakeThread(600 + len(self.sends), parent_id=self.id, guild_id=self.guild.id)
        message = FakeMessage(700 + len(self.sends), thread)
        return SimpleNamespace(thread=thread, message=message)


class FakeThread(FakeChannel):
    """A real thread whose parent must be independently checked."""

    def __init__(self, thread_id=800, *, parent_id=100, guild_id=10):
        """Construct thread metadata separate from its parent channel."""
        super().__init__(thread_id, guild_id=guild_id)
        self.parent_id = parent_id
        self.starter_message = None


class FakeMessage:
    """A known original Discord message with actual thread/edit/delete effects."""

    def __init__(self, message_id, channel):
        """Represent the real physical message and its immediate container."""
        self.id = message_id
        self.channel = channel
        self.author = SimpleNamespace(id=101, bot=False, display_name='author')
        self.content = 'before'
        self.edited_at = None
        self.edits = []
        self.deleted = False

    async def create_thread(self, **kwargs):
        """Simulate the SDK's verified root-anchored message thread."""
        self.edits.append(kwargs)
        return FakeThread(self.id, parent_id=self.channel.id, guild_id=self.channel.guild.id)

    async def edit(self, **kwargs):
        """Record exact message edit without mutating thread state."""
        self.edits.append(kwargs)

    async def delete(self):
        """Mark this exact physical message as removed."""
        self.deleted = True


class NotFound(Exception):
    """Simulated SDK not-found error."""


class FakeBot:
    """Stateful physical message store with a verified bridge bot identity."""

    def __init__(self, channels):
        """Bind concrete channels by Discord snowflake, not by community ID."""
        self.channels = {c.id: c for c in channels}
        self.user = SimpleNamespace(id=999, display_name='bridge', bot=True)

    def get_channel(self, channel_id):
        """Return cached physical channel only when present."""
        return self.channels.get(channel_id)

    async def fetch_channel(self, channel_id):
        """Fail on stale/missing channel rather than assuming existence."""
        if channel_id not in self.channels:
            raise NotFound()
        return self.channels[channel_id]


@pytest.fixture(autouse=True)
def fake_discord_sdk(monkeypatch):
    """Replace only the external Discord SDK, not adapter or repositories."""
    class AllowedMentions:
        """Track explicit mention suppression on each physical send."""

        @staticmethod
        def none():
            """Return a distinguishable suppressed-mentions policy."""
            return 'no-mentions'

    class MessageReference:
        """Expose constructed exact-channel reply parent without an SDK install."""

        def __init__(self, **kwargs):
            """Preserve reference parameters for assertions."""
            self.kwargs = kwargs

    monkeypatch.setitem(sys.modules, 'discord', SimpleNamespace(
        ForumChannel=FakeForum, TextChannel=FakeChannel, Thread=FakeThread,
        AllowedMentions=AllowedMentions, MessageReference=MessageReference,
    ))


def adapter(*channels):
    """Create a borrowed transport adapter backed by stateful physical channels."""
    return DiscordAdapter(connector=ConnectorRef('discord-primary', 'discord'),
                          bot=FakeBot(channels))


@pytest.mark.asyncio
async def test_forum_creation_uses_distinct_starter_id_and_mentions_none():
    """ActivityPub inbound post creates only one forum copy with true starter ID."""
    forum = FakeForum()
    transport = adapter(forum)
    target = ChannelRef('discord-primary', '300', 'forum', '10')
    attempt = CreateAttempt('post-1', 'discord-primary', 'forum_thread', target)
    result = await transport.create_forum_thread(
        target, ContentDocument('hi @everyone'), title='news',
        presentation=DeliveryPresentation('bot'), attempt=attempt,
    )
    assert isinstance(result, CreatedThread)
    assert result.thread.external_thread_id == '601'
    assert result.starter.external_message_id == '701'
    assert len(forum.sends) == 1
    assert forum.sends[0]['allowed_mentions'] == 'no-mentions'
    assert await transport.reconcile_create(attempt) == result
    assert await transport.create_forum_thread(
        target, ContentDocument('hi @everyone'), title='news',
        presentation=DeliveryPresentation('bot'), attempt=attempt,
    ) == result
    assert len(forum.sends) == 1


@pytest.mark.asyncio
async def test_reply_thread_uses_real_root_and_checked_parent():
    """One reply-thread creation must verify both root and parent channel."""
    root_channel = FakeChannel(is_news=True)
    root = FakeMessage(1234, root_channel)
    root_channel._messages[root.id] = root
    transport = adapter(root_channel)
    message = MessageRef('discord-primary', '100', '1234')
    attempt = CreateAttempt('reply-1', 'discord-primary', 'reply_thread', message)
    result = await transport.create_reply_thread(message, title='@p reply', attempt=attempt)
    assert result.external_thread_id == '1234'
    assert result.channel.kind == 'announcement'
    assert root.edits[0]['name'] == '@\u200bp reply'


@pytest.mark.asyncio
async def test_wrong_parent_denies_send_before_post():
    """A cross-community stale thread cannot receive a message."""
    parent = FakeForum(300)
    wrong_thread = FakeThread(444, parent_id=999)
    transport = adapter(parent, wrong_thread)
    destination = ThreadRef(ChannelRef('discord-primary', '300', 'forum', '10'), '444')
    attempt = CreateAttempt('comment-1', 'discord-primary', 'message', destination)
    with pytest.raises(PlatformOperationError) as info:
        await transport.send_message(destination, ContentDocument('hi'), reply_to=None,
                                     presentation=DeliveryPresentation('bot'), attempt=attempt)
    assert info.value.reason == 'invalid_request'
    assert wrong_thread.sends == []


@pytest.mark.asyncio
async def test_send_rejects_cross_container_reply_before_post():
    """A forged reply parent cannot be silently flattened into a normal message."""
    channel = FakeChannel()
    transport = adapter(channel)
    target = ChannelRef('discord-primary', '100', 'text', '10')
    attempt = CreateAttempt('comment-2', 'discord-primary', 'message', target)
    with pytest.raises(PlatformOperationError) as info:
        await transport.send_message(target, ContentDocument('hi'),
                                     reply_to=MessageRef('discord-primary', '200', '900'),
                                     presentation=DeliveryPresentation('bot'), attempt=attempt)
    assert info.value.reason == 'invalid_request'
    assert not channel.sends


@pytest.mark.asyncio
async def test_send_preflight_refuses_webhooks_and_attachments():
    """Unsupported presentation/content never creates a best-effort bot copy."""
    channel = FakeChannel()
    transport = adapter(channel)
    target = ChannelRef('discord-primary', '100', 'text', '10')
    attempt = CreateAttempt('comment-3', 'discord-primary', 'message', target)
    with pytest.raises(PlatformOperationError) as info:
        await transport.send_message(target, ContentDocument('hi'), reply_to=None,
                                     presentation=DeliveryPresentation('webhook'), attempt=attempt)
    assert info.value.reason == 'unsupported'
    with pytest.raises(PlatformOperationError) as info:
        await transport.send_message(target, ContentDocument('hi', attachments=(
            AttachmentRef('https://example.test/file.png', 'file.png'),)), reply_to=None,
            presentation=DeliveryPresentation('bot'), attempt=attempt)
    assert info.value.reason == 'unsupported'
    assert not channel.sends


@pytest.mark.asyncio
async def test_send_permission_denied_prevents_physical_create():
    """A missing bot permission fails closed before the Discord POST."""
    channel = FakeChannel()
    channel.can_send = False
    transport = adapter(channel)
    target = ChannelRef('discord-primary', '100', 'text', '10')
    attempt = CreateAttempt('comment-4', 'discord-primary', 'message', target)
    with pytest.raises(PlatformOperationError) as info:
        await transport.send_message(target, ContentDocument('hi'), reply_to=None,
                                     presentation=DeliveryPresentation('bot'), attempt=attempt)
    assert info.value.reason == 'forbidden'
    assert not channel.sends


@pytest.mark.asyncio
async def test_send_and_mutations_touch_only_immediate_message():
    """New comment, edit and delete mutate the physical copy, not its container."""
    channel = FakeChannel()
    transport = adapter(channel)
    target = ChannelRef('discord-primary', '100', 'text', '10')
    attempt = CreateAttempt('comment-5', 'discord-primary', 'message', target)
    receipt = await transport.send_message(target, ContentDocument('hello'), reply_to=None,
                                           presentation=DeliveryPresentation('bot'), attempt=attempt)
    assert len(channel.sends) == 1
    assert channel.sends[0]['allowed_mentions'] == 'no-mentions'
    physical = channel._messages[int(receipt.message.external_message_id)]
    await transport.edit_message(receipt.message, ContentDocument('updated'))
    await transport.delete_message(receipt.message)
    assert physical.edits[0]['content'] == 'updated'
    assert physical.deleted
