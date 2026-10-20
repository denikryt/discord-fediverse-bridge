"""Live BridgeBot Message Create admission distinguishes Personal Feed from community."""

from __future__ import annotations

from types import SimpleNamespace

import pytest


def _bot_world(monkeypatch, *, root=None, mapped=False, community=False):
    """Substitute only the Discord SDK type and observable adapter/DB edges."""
    discord = pytest.importorskip('discord')
    from src.discord_bot import BridgeBot

    class FakeThread:
        """Represent the physical on_message thread SDK shape."""

    monkeypatch.setattr(discord, 'Thread', FakeThread)
    thread = FakeThread()
    thread.id = 300
    thread.parent_id = 200
    msg = SimpleNamespace(
        id=401, channel=thread, guild=SimpleNamespace(id=1),
        author=SimpleNamespace(id=10, bot=False),
    )
    observed = []

    class FakeAdapter:
        """Observe exactly one route into physical thread-message normalization."""
        async def ingest_thread_message_created(self, _msg):
            """Record only a correctly selected thread message."""
            observed.append('typed')

    class FakeRouter:
        """Choose only current persisted bindings and forum subscriptions."""
        def is_personal_feed_thread(self, _id):
            """Resolve a previously adopted discussion thread."""
            return mapped

        def is_remote_subscription_forum(self, _id):
            """Represent a preexisting subscribed forum, when requested."""
            return community

        def is_local_community_forum(self, _id):
            """No local community is relevant to this case."""
            return False

        async def handle_message(self, *, message):
            """Observe whether the legacy community handler still receives messages."""
            observed.append('community')

    bot = SimpleNamespace(
        user=None, event_router=FakeRouter(),
        database=SimpleNamespace(
            activitypub_objects=SimpleNamespace(
                get_published_activity_object_by_discord_message_id=lambda _id: root,
            ),
            discord_fanout_groups=SimpleNamespace(
                get_thread_delivery_by_thread=lambda _id: None,
            ),
        ),
        _require_discord_adapter=lambda: FakeAdapter(),
    )
    return BridgeBot, bot, msg, observed


@pytest.mark.asyncio
async def test_reply_precedes_thread_create_but_published_root_enters_typed_ingress(monkeypatch):
    """A root-anchored reply is not dropped by the subscription gate."""
    root = SimpleNamespace(publication_context='personal_feed', discord_channel_id=200)
    bridge, bot, msg, observed = _bot_world(monkeypatch, root=root)
    await bridge._on_message_admitted(bot, msg)
    assert observed == ['typed']


@pytest.mark.asyncio
async def test_unmatched_text_thread_is_ignored_and_forum_keeps_legacy_route(monkeypatch):
    """A non-Personal Feed thread cannot publish via the new typed route."""
    root = SimpleNamespace(publication_context='personal_feed', discord_channel_id=201)
    bridge, bot, msg, observed = _bot_world(monkeypatch, root=root)
    await bridge._on_message_admitted(bot, msg)
    assert observed == []
    bridge, bot, msg, observed = _bot_world(monkeypatch, community=True)
    await bridge._on_message_admitted(bot, msg)
    assert observed == ['community']
