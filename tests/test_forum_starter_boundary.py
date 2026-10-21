"""Gateway Thread Create fetches verified starters without inventing physical IDs."""
from __future__ import annotations

from types import SimpleNamespace
import pytest

from src.discord_adapter import DiscordAdapter
from src.platform_types import ConnectorRef, PlatformOperationError, ThreadCreated
from test_discord_adapter_transport import (
    FakeForum, FakeThread, FakeMessage, FakeBot, NotFound,
)


class Sink:
    """Record one typed event leaving the SDK transport boundary."""
    def __init__(self):
        """Start with no domain events."""
        self.events = []
    async def handle_platform_event(self, event):
        """Capture only actual verified forum observations."""
        self.events.append(event)


def world(monkeypatch, *, cached=True, missing=False, foreign=False):
    """Model one physical forum/thread with starter ID distinct from its thread."""
    guild = SimpleNamespace(id=10)
    forum = FakeForum(300, guild_id=10)
    thread = FakeThread(600, parent_id=300, guild_id=10)
    thread.parent = forum
    thread.name = 'Topic'
    thread.owner_id = 101
    opener = FakeMessage(700, thread)
    opener.content = 'Original post'
    if foreign:
        opener.channel = FakeThread(999, parent_id=300, guild_id=10)
    thread.starter_message = opener if cached else None
    if not cached and not missing:
        # Real Discord often uses thread.id as starter ID; don't infer
        # this when the actual starter has a distinct ID and isn't cached.
        thread._messages[thread.id] = FakeMessage(thread.id, thread)
    sdk = SimpleNamespace(Thread=FakeThread, ForumChannel=FakeForum,
                          TextChannel=type('FakeText', (), {}))
    transport = DiscordAdapter(connector=ConnectorRef('discord-primary', 'discord'),
                               bot=FakeBot([forum, thread]))
    monkeypatch.setattr(transport, '_sdk', lambda: sdk)
    sink = Sink()
    transport.bind_ingress_sink(sink)
    return transport, thread, opener, sink


@pytest.mark.asyncio
async def test_cached_distinct_forum_starter_id_is_preserved(monkeypatch):
    """Known starter 700 is published as 700 even when thread ID is 600."""
    port, thread, opener, sink = world(monkeypatch)
    await port.ingest_thread_created(thread)
    assert len(sink.events) == 1
    event = sink.events[0]
    assert isinstance(event, ThreadCreated)
    assert event.starter.external_message_id == '700'
    assert event.thread.external_thread_id == '600'


@pytest.mark.asyncio
async def test_no_cache_no_exact_starter_no_domain_event(monkeypatch):
    """A missing exact starter never falls back to an arbitrary history message."""
    port, thread, _, sink = world(monkeypatch, cached=False, missing=True)
    async def forbidden_history(**kwargs):
        """Fallback to unrelated messages would forge original authorship."""
        raise AssertionError('history must not be queried')
    thread.history = forbidden_history
    await port.ingest_thread_created(thread)
    assert sink.events == []


@pytest.mark.asyncio
async def test_wrong_starter_container_is_rejected(monkeypatch):
    """A starter from another thread must never cross the normalized boundary."""
    port, thread, _, sink = world(monkeypatch, foreign=True)
    with pytest.raises(PlatformOperationError) as exc:
        await port.ingest_thread_created(thread)
    assert exc.value.reason == 'invalid_request'
    assert sink.events == []


@pytest.mark.asyncio
async def test_bot_authored_starter_is_not_canonical(monkeypatch):
    """Echo from the bridge's own bot must not become a new canonical post."""
    port, thread, opener, sink = world(monkeypatch)
    opener.author.bot = True
    await port.ingest_thread_created(thread)
    assert sink.events == []


@pytest.mark.asyncio
async def test_uncached_parent_is_fetched_and_checked_before_starter(monkeypatch):
    """A cold parent cache can recover only from the actual fetched forum."""
    port, thread, _, sink = world(monkeypatch)
    thread.parent = None
    await port.ingest_thread_created(thread)
    assert len(sink.events) == 1
    assert sink.events[0].thread.channel.external_channel_id == '300'


@pytest.mark.asyncio
async def test_missing_parent_lookup_rejects_forum_event(monkeypatch):
    """An unresolved parent is not a license to publish to a guessed forum."""
    port, thread, _, sink = world(monkeypatch)
    thread.parent = None
    port._bot.channels.pop(300)
    with pytest.raises(PlatformOperationError):
        await port.ingest_thread_created(thread)
    assert sink.events == []
