"""Production-bound typed Gateway events exercise canonical SQLite runtimes.

Only Discord HTTP and ActivityPub HTTP are simulated. The ingress, router,
subscription checks, mapping repositories and publication handlers are real.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.discord_event_router import DiscordEventRouter
from src.platform_event_ingress import PlatformEventIngress
from src.platform_types import (
    ChannelRef, ConnectorRef, MessageView, PlatformOperationError, ThreadCreated,
)
from test_stage03_normalized_community_runtime import world as remote_world, starter as remote_starter, reply as remote_reply
from test_stage03_normalized_local_community_runtime import world as local_world, starter as local_starter, reply as local_reply


class NoMutations:
    """Exclude unrelated physical edit/delete events from these Create scenarios."""

    async def handle_platform_message_edit(self, event, adapter):
        """A Create must never be routed as edit."""
        raise AssertionError('unexpected edit')

    async def handle_platform_message_delete(self, event, adapter):
        """A Create must never be routed as delete."""
        raise AssertionError('unexpected delete')


def make_ingress(db, runtime, adapter, *, guild):
    """Use the production router to route into a real canonical community runtime."""
    adapter.connector = ConnectorRef('discord-primary', 'discord')
    policy = SimpleNamespace(is_discord_guild_allowed=lambda key: int(key) == guild)
    router = DiscordEventRouter(
        database=db, community_runtime=runtime if guild == 1 else SimpleNamespace(),
        local_community_runtime=runtime if guild == 10 else SimpleNamespace(),
        bridge_policy_service=policy,
    )
    router.bind_reply_notice_adapter(adapter)
    return PlatformEventIngress(adapter=adapter, mutations=NoMutations(), creates=router)


@pytest.mark.asyncio
async def test_remote_forum_starter_and_reply_publish_once_over_production_ingress(tmp_path):
    """A Discord forum starter then reply creates one canonical post/comment with mirrors."""
    db, runtime, fedify, port = remote_world(tmp_path)
    try:
        event, view = remote_starter()

        async def fetch_message(identity):
            """Provide the independently observed starter, not a guessed thread ID."""
            assert identity == event.starter
            return view

        port.fetch_message = fetch_message
        ingress = make_ingress(db, runtime, port, guild=1)
        await ingress.handle_platform_event(event)
        await ingress.handle_platform_event(event)
        assert len(fedify.requests) == 1
        assert len(port.posts) == 2
        await ingress.handle_platform_event(remote_reply())
        await ingress.handle_platform_event(remote_reply())
        assert len(fedify.requests) == 2
        assert len(port.replies) == 2
    finally:
        db.close()


@pytest.mark.asyncio
async def test_local_host_and_subscriber_route_to_local_runtime(tmp_path):
    """An existing host/source forum goes to Local Community, never Remote Community."""
    db, community, subscribers, runtime, fedify, port = local_world(tmp_path)
    try:
        event, view = local_starter()

        async def fetch_message(identity):
            """Return the exact starter body from the physical boundary."""
            assert identity == event.starter
            return view

        port.fetch_message = fetch_message
        ingress = make_ingress(db, runtime, port, guild=10)
        await ingress.handle_platform_event(event)
        await ingress.handle_platform_event(event)
        await ingress.handle_platform_event(local_reply())
        assert len(fedify.requests) == 2
        assert len(port.posts) == 2
        assert len(port.replies) == 2
        assert db.local_community_surfaces.get_local_community_thread_surface_by_discord_thread_id(1100)
    finally:
        db.close()


@pytest.mark.asyncio
async def test_missing_or_wrong_starter_never_publishes(tmp_path):
    """No starter evidence and mismatched physical receipt produce zero AP side effects."""
    db, runtime, fedify, port = remote_world(tmp_path)
    try:
        event, view = remote_starter()

        async def wrong_view(identity):
            """A remote API receipt must identify the requested physical message."""
            from dataclasses import replace
            from src.platform_types import MessageRef
            return replace(view, message=MessageRef('discord-primary', '200', '999'))

        port.fetch_message = wrong_view
        ingress = make_ingress(db, runtime, port, guild=1)
        with pytest.raises(PlatformOperationError):
            await ingress.handle_platform_event(event)
        assert len(fedify.requests) == len(port.posts) == 0
        from dataclasses import replace
        await ingress.handle_platform_event(replace(event, starter=None))
        assert len(fedify.requests) == 0
    finally:
        db.close()


@pytest.mark.asyncio
async def test_wrong_forum_or_connector_never_emits_ap(tmp_path):
    """A foreign connector and an unsubscribed forum cannot become a canonical author."""
    db, runtime, fedify, port = remote_world(tmp_path)
    try:
        event, view = remote_starter(forum_id='999')
        async def fetched(identity):
            """No read should authorize an unrelated forum."""
            return view
        port.fetch_message = fetched
        ingress = make_ingress(db, runtime, port, guild=1)
        await ingress.handle_platform_event(event)
        assert len(fedify.requests) == 0
        from src.platform_types import ThreadRef, MessageRef
        foreign = ThreadCreated(
            'fluxer-primary', ThreadRef(ChannelRef('fluxer-primary', '999', 'forum', '1'), '200'),
            'Subject', starter=MessageRef('fluxer-primary', '200', '300'),
            external_guild_id='1',
        )
        with pytest.raises(PlatformOperationError):
            await ingress.handle_platform_event(foreign)
    finally:
        db.close()
