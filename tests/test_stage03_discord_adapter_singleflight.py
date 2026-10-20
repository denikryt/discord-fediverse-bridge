"""DiscordAdapter CreateAttempt identity, concurrency, cancellation and capacity."""

from __future__ import annotations

import asyncio

import pytest

from src.platform_types import (
    ChannelRef, ConnectorRef, ContentDocument, CreateAttempt,
    CreatedMessage, DeliveryPresentation, MessageRef, PlatformOperationError,
)
from src.discord_adapter import DiscordAdapter


CHANNEL = ChannelRef('discord-primary', '100', 'text', '1')
ATTEMPT = CreateAttempt('send-1', 'discord-primary', 'message', CHANNEL)
DOCUMENT = ContentDocument('hello')
PRESENTATION = DeliveryPresentation('bot')
RECEIPT = CreatedMessage(MessageRef('discord-primary', '100', '501'), 'bot')


def _adapter(max_attempts=100_000):
    """Construct transport ownership without opening a network connection."""
    return DiscordAdapter(
        connector=ConnectorRef('discord-primary', 'discord'),
        bot=object(), max_create_attempts=max_attempts,
    )


@pytest.mark.asyncio
async def test_parallel_identical_attempts_share_one_physical_result():
    """Two concurrent callers for one copy must trigger just one physical POST."""
    adapter = _adapter()
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def send():
        """Represent one pending physical Discord message POST."""
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return RECEIPT

    async def call():
        """Submit identical immutable attempt signatures from different handlers."""
        return await adapter._begin_create_attempt(
            ATTEMPT, operation='message', destination=CHANNEL,
            content=DOCUMENT, presentation=PRESENTATION, title=None, reply_to=None,
            send=send,
        )

    first = asyncio.create_task(call())
    await entered.wait()
    second = asyncio.create_task(call())
    release.set()
    assert await asyncio.gather(first, second) == [RECEIPT, RECEIPT]
    assert calls == 1
    assert await call() == RECEIPT
    assert calls == 1


@pytest.mark.asyncio
async def test_changed_payload_same_key_rejected_while_owner_inflight():
    """A colliding CreateAttempt key cannot change content or destination metadata."""
    adapter = _adapter()
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def send():
        """Pause the first physical write before its verified receipt."""
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return RECEIPT

    first = asyncio.create_task(adapter._begin_create_attempt(
        ATTEMPT, operation='message', destination=CHANNEL, content=DOCUMENT,
        presentation=PRESENTATION, title=None, reply_to=None, send=send,
    ))
    await entered.wait()
    with pytest.raises(PlatformOperationError) as info:
        await adapter._begin_create_attempt(
            ATTEMPT, operation='message', destination=CHANNEL,
            content=ContentDocument('forged replacement'),
            presentation=PRESENTATION, title=None, reply_to=None, send=send,
        )
    assert info.value.reason == 'invalid_request'
    release.set()
    assert await first == RECEIPT
    assert calls == 1


@pytest.mark.asyncio
async def test_unknown_create_result_is_sticky_and_never_replayed():
    """An ambiguous HTTP result blocks future creates with that copy key."""
    adapter = _adapter()
    calls = 0

    async def uncertain():
        """Model Discord committing then dropping its HTTP receipt."""
        nonlocal calls
        calls += 1
        raise PlatformOperationError('unknown_delivery')

    async def send():
        """A replay that must never be invoked."""
        nonlocal calls
        calls += 1
        return RECEIPT

    with pytest.raises(PlatformOperationError) as info:
        await adapter._begin_create_attempt(
            ATTEMPT, operation='message', destination=CHANNEL,
            content=DOCUMENT, presentation=PRESENTATION, title=None,
            reply_to=None, send=uncertain,
        )
    assert info.value.reason == 'unknown_delivery'
    with pytest.raises(PlatformOperationError) as replay:
        await adapter._begin_create_attempt(
            ATTEMPT, operation='message', destination=CHANNEL,
            content=DOCUMENT, presentation=PRESENTATION, title=None,
            reply_to=None, send=send,
        )
    assert replay.value.reason == 'unknown_delivery'
    assert calls == 1
    assert await adapter.reconcile_create(ATTEMPT) is None


@pytest.mark.asyncio
async def test_capacity_refuses_new_keys_but_allows_existing_joins():
    """Fail closed at ledger capacity rather than evicting a verified receipt."""
    adapter = _adapter(max_attempts=1)

    async def send():
        """Return one verified immutable receipt."""
        return RECEIPT

    assert await adapter._begin_create_attempt(
        ATTEMPT, operation='message', destination=CHANNEL,
        content=DOCUMENT, presentation=PRESENTATION, title=None,
        reply_to=None, send=send,
    ) == RECEIPT
    second_attempt = CreateAttempt('send-2', 'discord-primary', 'message', CHANNEL)
    with pytest.raises(PlatformOperationError) as info:
        await adapter._begin_create_attempt(
            second_attempt, operation='message', destination=CHANNEL,
            content=DOCUMENT, presentation=PRESENTATION, title=None,
            reply_to=None, send=send,
        )
    assert info.value.reason == 'unavailable'
    assert await adapter._begin_create_attempt(
        ATTEMPT, operation='message', destination=CHANNEL,
        content=DOCUMENT, presentation=PRESENTATION, title=None,
        reply_to=None, send=send,
    ) == RECEIPT


@pytest.mark.asyncio
async def test_owner_cancellation_latches_unknown_and_releases_waiters():
    """Cancelling the owner does not strand a concurrent caller or retry POST."""
    adapter = _adapter()
    started = asyncio.Event()
    never = asyncio.Event()

    async def send():
        """Model an in-flight SDK send cancelled by shutdown."""
        started.set()
        await never.wait()
        return RECEIPT

    async def call():
        """Request the same physical copy on either coroutine."""
        return await adapter._begin_create_attempt(
            ATTEMPT, operation='message', destination=CHANNEL,
            content=DOCUMENT, presentation=PRESENTATION, title=None,
            reply_to=None, send=send,
        )

    owner = asyncio.create_task(call())
    await started.wait()
    follower = asyncio.create_task(call())
    await asyncio.sleep(0)
    owner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner
    with pytest.raises(PlatformOperationError) as info:
        await follower
    assert info.value.reason == 'unknown_delivery'
