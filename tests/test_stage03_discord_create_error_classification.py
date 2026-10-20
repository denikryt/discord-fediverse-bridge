"""Only proven Discord HTTP rejections may be retried with the same create key."""

from __future__ import annotations

import pytest

from src.platform_types import (
    ChannelRef, ContentDocument, CreateAttempt, DeliveryPresentation, MessageRef,
    PlatformOperationError,
)
from test_stage03_discord_adapter_transport import (
    FakeChannel, FakeForum, FakeMessage, adapter, fake_discord_sdk,
)


class Forbidden(Exception):
    """Represent a Discord SDK 403 rejection without a created object."""


class NotFound(Exception):
    """Represent a Discord SDK 404 rejection without a created object."""


class RateLimited(Exception):
    """Represent an observable HTTP 429 response that cannot have committed a Create."""

    status = 429


@pytest.mark.asyncio
@pytest.mark.parametrize(('operation', 'error_type', 'reason'), [
    ('forum_thread', Forbidden, 'forbidden'),
    ('reply_thread', NotFound, 'not_found'),
    ('message', RateLimited, 'rate_limited'),
])
async def test_definitive_discord_create_rejection_can_be_retried_after_fix(operation, error_type, reason):
    """A server-rejected POST keeps its classified reason and permits a later retry."""
    channel = FakeForum(300) if operation == 'forum_thread' else FakeChannel(100)
    transport = adapter(channel)
    posts = []
    if operation == 'forum_thread':
        destination = ChannelRef('discord-primary', '300', 'forum', '10')
        original = channel.create_thread
        async def physical(**kwargs):
            """Reject first API call; allow the same attempt after policy recovery."""
            posts.append('post')
            if len(posts) == 1:
                raise error_type('Discord refusal')
            return await original(**kwargs)
        channel.create_thread = physical
        async def call():
            """Submit a forum create through the full transport boundary."""
            return await transport.create_forum_thread(
                destination, ContentDocument('body'), title='post',
                presentation=DeliveryPresentation('bot'),
                attempt=CreateAttempt('retry-safe', 'discord-primary', operation, destination),
            )
    elif operation == 'reply_thread':
        root = FakeMessage(1200, channel)
        channel._messages[root.id] = root
        destination = MessageRef('discord-primary', '100', '1200')
        original = root.create_thread
        async def physical(**kwargs):
            """Reject first root create but accept the independently verified next call."""
            posts.append('post')
            if len(posts) == 1:
                raise error_type('Discord refusal')
            return await original(**kwargs)
        root.create_thread = physical
        async def call():
            """Create from exact text root rather than an inferred forum ID."""
            return await transport.create_reply_thread(
                destination, title='replies',
                attempt=CreateAttempt('retry-safe', 'discord-primary', operation, destination),
            )
    else:
        destination = ChannelRef('discord-primary', '100', 'text', '10')
        original = channel.send
        async def physical(**kwargs):
            """Reject first message send with an explicit status code."""
            posts.append('post')
            if len(posts) == 1:
                raise error_type('Discord refusal')
            return await original(**kwargs)
        channel.send = physical
        async def call():
            """Send one exact text-channel message with a retained CreateAttempt key."""
            return await transport.send_message(
                destination, ContentDocument('hello'), reply_to=None,
                presentation=DeliveryPresentation('bot'),
                attempt=CreateAttempt('retry-safe', 'discord-primary', operation, destination),
            )
    with pytest.raises(PlatformOperationError) as caught:
        await call()
    assert caught.value.reason == reason
    assert await call() is not None
    assert posts == ['post', 'post']


@pytest.mark.asyncio
async def test_ambiguous_discord_create_failure_still_never_posts_twice():
    """Dropped network response leaves the same key stuck unresolved."""
    channel = FakeChannel(100)
    transport = adapter(channel)
    posts = []
    destination = ChannelRef('discord-primary', '100', 'text', '10')
    async def physical(**kwargs):
        """Model a write that could have committed before a transport timeout."""
        posts.append('post')
        raise TimeoutError('connection dropped after write')
    channel.send = physical
    async def call():
        """Repeat the same physical request identity to test sticky uncertainty."""
        return await transport.send_message(
            destination, ContentDocument('hello'), reply_to=None,
            presentation=DeliveryPresentation('bot'),
            attempt=CreateAttempt('lost-response', 'discord-primary', 'message', destination),
        )
    for _ in range(2):
        with pytest.raises(PlatformOperationError) as caught:
            await call()
        assert caught.value.reason == 'unknown_delivery'
    assert posts == ['post']
