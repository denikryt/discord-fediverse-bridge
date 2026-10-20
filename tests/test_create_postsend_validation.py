"""A Discord Create receipt is evidence of a possible physical write, even when malformed."""
from __future__ import annotations

import pytest

from src.discord_adapter import DiscordAdapter
from src.platform_types import (
    ChannelRef, ConnectorRef, ContentDocument, CreateAttempt, CreatedMessage,
    DeliveryPresentation, MessageRef, PlatformOperationError,
)


@pytest.mark.asyncio
async def test_postsend_receipt_validation_error_remembers_unknown_without_reposting():
    """A wrong returned physical destination must not reopen the create key."""
    target = ChannelRef('discord-primary', '100', 'text', '10')
    transport = DiscordAdapter(connector=ConnectorRef('discord-primary', 'discord'), bot=object())
    attempt = CreateAttempt('uncertain-receipt', 'discord-primary', 'message', target)
    sends = []

    async def physical():
        """Simulate a completed HTTP POST with an untrustworthy physical receipt."""
        sends.append('posted')
        return CreatedMessage(MessageRef('discord-primary', '999', '800'), 'bot')

    async def submit():
        """Attempt the same request identity using the actual coordinator."""
        return await transport._begin_create_attempt(
            attempt, operation='message', destination=target,
            content=ContentDocument('test'), presentation=DeliveryPresentation('bot'),
            title=None, reply_to=None, send=physical,
        )

    for _ in range(3):
        with pytest.raises(PlatformOperationError) as raised:
            await submit()
        assert raised.value.reason == 'unknown_delivery'
    assert sends == ['posted']
    assert await transport.reconcile_create(attempt) is None


@pytest.mark.asyncio
async def test_valid_postsend_receipt_reused_without_second_physical_write():
    """An exact typed success is a reusable confirmed receipt, not an ambiguous write."""
    target = ChannelRef('discord-primary', '100', 'text', '10')
    transport = DiscordAdapter(connector=ConnectorRef('discord-primary', 'discord'), bot=object())
    attempt = CreateAttempt('confirmed-receipt', 'discord-primary', 'message', target)
    sends = []

    async def physical():
        """Return an exact Discord receipt from a single physical message create."""
        sends.append('posted')
        return CreatedMessage(MessageRef('discord-primary', '100', '800'), 'bot')

    async def submit():
        """Use the same immutable request for both calls."""
        return await transport._begin_create_attempt(
            attempt, operation='message', destination=target,
            content=ContentDocument('test'), presentation=DeliveryPresentation('bot'),
            title=None, reply_to=None, send=physical,
        )

    first = await submit()
    assert await submit() == first
    assert await transport.reconcile_create(attempt) == first
    assert sends == ['posted']
