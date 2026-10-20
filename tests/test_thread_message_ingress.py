"""Thread MessageCreated admission tests at the Discord transport boundary."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.discord_adapter import DiscordAdapter
from src.platform_types import ChannelRef, ConnectorRef, MessageCreated, PlatformOperationError


class FakeThread:
    """Represent only the independently observed Discord Thread type."""


class CollectingSink:
    """Capture events after real adapter normalization but before domain effects."""

    def __init__(self) -> None:
        """Start with zero admitted events."""
        self.events: list[MessageCreated] = []

    async def handle_platform_event(self, event: MessageCreated) -> None:
        """Record the event without manufacturing a canonical publication."""
        self.events.append(event)


def physical_message(*, parent=45, thread=100, guild=9, parent_guild=9,
                     thread_guild=9, msg_guild=9, attachments=(),
                     kind='default') -> object:
    """Create physical Discord message evidence, not a fabricated DTO."""
    parent_channel = SimpleNamespace(id=parent, guild=SimpleNamespace(id=parent_guild))
    container = FakeThread()
    container.id = thread
    container.parent_id = parent
    container.parent = parent_channel
    container.guild = SimpleNamespace(id=thread_guild)
    return SimpleNamespace(
        id=301, guild=SimpleNamespace(id=msg_guild), channel=container,
        author=SimpleNamespace(id=11, bot=False, display_name='Bob'),
        webhook_id=None, type=kind, content='A reply',
        reference=SimpleNamespace(channel_id=thread, message_id=302),
        attachments=attachments, created_at=datetime(2026, 10, 19, tzinfo=timezone.utc),
    )


def adapter_with_sink() -> tuple[DiscordAdapter, CollectingSink]:
    """Substitute only the SDK type/physical channel assertion at the test edge."""
    adapter = DiscordAdapter(
        connector=ConnectorRef('discord-primary', 'discord'),
        bot=SimpleNamespace(user=SimpleNamespace(id=900)),
    )
    adapter._sdk = lambda: SimpleNamespace(Thread=FakeThread)
    adapter._validate_channel = lambda request, observed: ChannelRef(
        'discord-primary', str(observed.id), 'forum', str(observed.guild.id),
    )
    sink = CollectingSink()
    adapter._sink = sink
    return adapter, sink


@pytest.mark.asyncio
async def test_observed_forum_reply_retains_author_parent_and_reply_identity() -> None:
    """One physical forum reply yields one neutral event with exact ancestry."""
    adapter, sink = adapter_with_sink()
    await adapter.ingest_thread_message_created(physical_message())
    assert len(sink.events) == 1
    event = sink.events[0]
    assert event.message.external_container_id == '100'
    assert event.thread_context.external_thread_id == '100'
    assert event.parent_channel.external_channel_id == '45'
    assert event.parent_channel.kind == 'forum'
    assert event.reply_to.external_message_id == '302'
    assert event.author.external_actor_id == '11'
    assert event.created_at.year == 2026


@pytest.mark.asyncio
async def test_mismatched_parent_and_guild_are_rejected_before_sink() -> None:
    """An unverified physical parent or conflicting guild cannot reach core."""
    adapter, sink = adapter_with_sink()
    for kwargs in ({'parent_guild': 10}, {'thread_guild': 10}, {'msg_guild': 10}):
        with pytest.raises(PlatformOperationError, match='guild'):
            await adapter.ingest_thread_message_created(physical_message(**kwargs))
    msg = physical_message()
    msg.channel.parent_id = 46
    with pytest.raises(PlatformOperationError, match='parent'):
        await adapter.ingest_thread_message_created(msg)
    assert sink.events == []


@pytest.mark.asyncio
async def test_synthetic_starter_is_not_published_and_attachments_fail_closed() -> None:
    """Synthetic thread notifications never become comments and media is not dropped."""
    adapter, sink = adapter_with_sink()
    await adapter.ingest_thread_message_created(physical_message(kind='thread_starter_message'))
    with pytest.raises(PlatformOperationError, match='attachment'):
        await adapter.ingest_thread_message_created(physical_message(attachments=(object(),)))
    assert sink.events == []


@pytest.mark.asyncio
async def test_uncached_parent_is_resolved_by_exact_sdk_lookup_before_ingress() -> None:
    """A cold SDK cache may publish only after an exact verified parent lookup."""
    adapter, sink = adapter_with_sink()
    msg = physical_message()
    parent = msg.channel.parent
    msg.channel.parent = None
    looked_up = []

    async def fetch_channel(channel_id: int) -> object:
        """Return the physical parent from the simulated Discord API."""
        looked_up.append(channel_id)
        return parent

    adapter._bot.get_channel = lambda _id: None
    adapter._bot.fetch_channel = fetch_channel
    await adapter.ingest_thread_message_created(msg)
    assert looked_up == [45]
    assert len(sink.events) == 1
    assert sink.events[0].thread_context.channel.external_channel_id == '45'


@pytest.mark.asyncio
async def test_uncached_parent_fetch_failure_never_guesses_a_reply_context() -> None:
    """Unavailable parent metadata cannot silently become a text-channel root."""
    adapter, sink = adapter_with_sink()
    msg = physical_message()
    msg.channel.parent = None
    adapter._bot.get_channel = lambda _id: None

    async def fetch_channel(_channel_id: int) -> object:
        """Simulate Discord refusing the authoritative parent lookup."""
        raise RuntimeError('Discord unavailable')

    adapter._bot.fetch_channel = fetch_channel
    with pytest.raises(PlatformOperationError, match='unavailable'):
        await adapter.ingest_thread_message_created(msg)
    assert sink.events == []
