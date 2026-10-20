"""Live BridgeBot forum Create callbacks use only normalized DiscordAdapter ingress."""
from __future__ import annotations

import sys
import asyncio
from types import ModuleType, SimpleNamespace

import pytest


@pytest.fixture
def bridge(monkeypatch):
    """Import only the SDK-owned shell; do not fake router or database logic."""
    command = ModuleType('src.command_tree')
    command.BridgeCommandTree = lambda *args: None
    monkeypatch.setitem(sys.modules, 'src.command_tree', command)
    from src.discord_bot import BridgeBot
    return BridgeBot


def setup_world(*, forum=True, allow=True):
    """Supply one subscribed forum and observable single transport route."""
    seen = []

    class FakeAdapter:
        """Observe a single normalized submission, without performing HTTP."""
        async def ingest_thread_created(self, thread):
            """Capture one forum thread event, not an SDK publish call."""
            seen.append(('thread', thread.id))
        async def ingest_thread_message_created(self, message):
            """Capture one message event without direct domain publication."""
            seen.append(('message', message.id))

    class FakeRouter:
        """Restrict admission to accepted forums and exclude personal roots."""
        def is_remote_subscription_forum(self, channel_id):
            """A forum source exists only when requested."""
            return forum and allow and channel_id == 300
        def is_local_community_forum(self, channel_id):
            """No local-source claim in this remote-community scenario."""
            return False
        def is_personal_feed_thread(self, thread_id):
            """Never treat a subscribed forum as Personal Feed."""
            return False
        async def handle_thread_create(self, **kwargs):
            """Legacy direct SDK dispatch must be unreachable."""
            raise AssertionError('legacy thread publication called')
        async def handle_message(self, **kwargs):
            """Legacy direct SDK dispatch must be unreachable."""
            raise AssertionError('legacy message publication called')

    fake_bot = SimpleNamespace(
        _thread_locks={},
        user=SimpleNamespace(id=999), event_router=FakeRouter(),
        discord_adapter=FakeAdapter(),
        _require_discord_adapter=lambda: FakeAdapter(),
        database=SimpleNamespace(
            activitypub_objects=SimpleNamespace(
                get_published_activity_object_by_discord_message_id=lambda _id: None,
            ),
            discord_fanout_groups=SimpleNamespace(
                get_thread_delivery_by_thread=lambda _id: None,
            ),
        ),
    )
    return fake_bot, seen


@pytest.mark.asyncio
async def test_thread_callback_routes_once_without_legacy_fetch_or_publish(bridge):
    """Accepted forum Thread Create reaches adapter and never runs SDK publisher."""
    bot, seen = setup_world()
    thread = SimpleNamespace(id=600, parent_id=300, owner_id=101)
    await bridge._on_thread_create_admitted(bot, thread)
    assert seen == [('thread', 600)]


@pytest.mark.asyncio
async def test_forum_message_callback_routes_once_without_legacy_publish(bridge, monkeypatch):
    """Accepted forum reply reaches same normalized transport as Personal Feed reply."""
    import discord
    class Thread(discord.Thread):
        """Model an observed thread rather than a text-root channel."""
    monkeypatch.setattr(discord, 'Thread', Thread)
    bot, seen = setup_world()
    channel = Thread()
    channel.id, channel.parent_id = 600, 300
    msg = SimpleNamespace(id=700, channel=channel, author=SimpleNamespace(id=123, bot=False),
                          guild=SimpleNamespace(id=1))
    await bridge._on_message_admitted(bot, msg)
    assert seen == [('message', 700)]


@pytest.mark.asyncio
async def test_unsubscribed_forum_never_enters_adapter(bridge):
    """An unrelated physical forum cannot trigger canonical publication."""
    bot, seen = setup_world(allow=False)
    await bridge._on_thread_create_admitted(bot, SimpleNamespace(id=600, parent_id=300, owner_id=101))
    assert seen == []


@pytest.mark.asyncio
async def test_concurrent_thread_callbacks_are_serialized_until_canonical_receipt(bridge):
    """Two Gateway notices cannot both pass the publish/SQLite race boundary."""
    bot, observed = setup_world()
    started = asyncio.Event()
    release = asyncio.Event()

    class SlowAdapter:
        """Pause the first publication before its canonical receipt commits."""
        async def ingest_thread_created(self, thread):
            """Expose whether another callback runs while the first is active."""
            observed.append(('begin', thread.id))
            started.set()
            await release.wait()
            observed.append(('end', thread.id))

    bot._require_discord_adapter = lambda: SlowAdapter()
    same = SimpleNamespace(id=600, parent_id=300, owner_id=101)
    first = asyncio.create_task(bridge._on_thread_create_admitted(bot, same))
    await started.wait()
    second = asyncio.create_task(bridge._on_thread_create_admitted(bot, same))
    await asyncio.sleep(0)
    assert observed == [('begin', 600)]
    release.set()
    await asyncio.gather(first, second)
    assert observed == [('begin', 600), ('end', 600), ('begin', 600), ('end', 600)]
