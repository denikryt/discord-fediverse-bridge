"""Gateway DTO -> existing mutation routing with no SDK-shaped domain inputs.

These scenarios exercise the mutation handler with a real identity contract and
stateful fakes for the repository and outer platform reader.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from src.discord_message_mutations import DiscordMessageMutationHandler
from src.platform_types import (
    AuthorRef, ConnectorRef, ContentDocument, MessageDeleted, MessageRef,
    MessageUpdated, MessageView, PlatformOperationError,
)


WHEN = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
MESSAGE = MessageRef('discord-primary', '100', '200')


class _MutationDatabase:
    """Track actual admission lookups and pending durable reconciliation work."""

    def __init__(self, role: str | None = 'source'):
        """Construct one canonical source, mirror, or unmapped message."""
        self.lookups: list[int] = []
        self.requests: list[dict] = []
        self.discord_fanout_groups = SimpleNamespace(get_message_delivery_by_message=self._delivery)
        self.message_mappings = SimpleNamespace(get_message_mapping_by_discord_message_id=lambda _id: None)
        self.content_mutations = SimpleNamespace(request_discord_reconciliation=lambda **kwargs: self.requests.append(kwargs))
        self.role = role

    def _delivery(self, message_id: int):
        """Return the stored physical delivery's role after recording its ID."""
        self.lookups.append(message_id)
        return SimpleNamespace(role=self.role) if self.role else None


class _Router:
    """Record the existing federation mutation entry points after admission."""

    def __init__(self):
        """Create observable edit/delete boundary tasks."""
        self.handle_message_edit = AsyncMock()
        self.handle_message_delete = AsyncMock()

    def is_local_community_message(self, message_id: int) -> bool:
        """Keep these scenarios in the remote-community source branch."""
        return False


class _Reader:
    """Resolve a neutral MessageView without exposing any Discord SDK object."""

    def __init__(self, *, view=None, failure=None):
        """Configure an authoritative physical read or an unavailable platform."""
        self.connector = ConnectorRef('discord-primary', 'discord')
        self.calls: list[MessageRef] = []
        self.view = view
        self.failure = failure

    async def fetch_message(self, message: MessageRef):
        """Record the exact connector-scoped message and return its state."""
        self.calls.append(message)
        if self.failure:
            raise self.failure
        return self.view


def _handler(db, router, *, wake=None):
    """Build the existing handler; the legacy SDK fetcher is never used here."""
    context = SimpleNamespace(user_actor_delivery_wake=SimpleNamespace(wake=wake or Mock()))
    return DiscordMessageMutationHandler(
        database=db, event_router=router,
        discord_fetcher=SimpleNamespace(),
        mutation_context_provider=lambda: context,
        clock=lambda: 100.0,
    )


@pytest.mark.asyncio
async def test_typed_source_edit_uses_verified_event_without_sdk_fetch():
    """Known content/timestamp reaches a single edit path with original author."""
    db, router = _MutationDatabase(), _Router()
    handler = _handler(db, router)
    reader = _Reader()
    event = MessageUpdated(
        'discord-primary', MESSAGE, ContentDocument('new body'), WHEN,
        external_guild_id='42', author=AuthorRef('discord-primary', '55', 'human', 'Alice'),
    )
    await handler.handle_platform_message_edit(event, reader)
    assert db.lookups == [200]
    assert reader.calls == []
    router.handle_message_edit.assert_awaited_once()
    kwargs = router.handle_message_edit.await_args.kwargs
    assert (kwargs['message_id'], kwargs['new_content'], kwargs['author_display_name']) == (200, 'new body', 'Alice')
    assert kwargs['edited_timestamp'] is WHEN and kwargs['guild_id'] == 42


@pytest.mark.asyncio
async def test_typed_wrong_connector_rejected_before_any_repository_lookup():
    """Fluxer-originated IDs cannot be interpreted as Discord database snowflakes."""
    db, router = _MutationDatabase(), _Router()
    handler = _handler(db, router)
    reader = _Reader()
    event = MessageUpdated('fluxer-main', MessageRef('fluxer-main', '100', '200'), ContentDocument('x'), WHEN)
    with pytest.raises(PlatformOperationError, match='connector'):
        await handler.handle_platform_message_edit(event, reader)
    assert db.lookups == [] and reader.calls == []
    router.handle_message_edit.assert_not_awaited()


@pytest.mark.asyncio
async def test_typed_mirror_edit_and_inbound_delete_do_not_propagate():
    """Only source edits and non-inbound deletes may mutate canonical AP state."""
    db, router = _MutationDatabase('mirror'), _Router()
    handler, reader = _handler(db, router), _Reader()
    await handler.handle_platform_message_edit(MessageUpdated('discord-primary', MESSAGE, ContentDocument('x'), WHEN), reader)
    router.handle_message_edit.assert_not_awaited()
    db.role = 'inbound'
    await handler.handle_platform_message_delete(MessageDeleted('discord-primary', MESSAGE), reader)
    router.handle_message_delete.assert_not_awaited()
    assert reader.calls == []


@pytest.mark.asyncio
async def test_typed_partial_edit_uses_authoritative_view_and_persists_unavailable_read():
    """Missing partial fields fetch the same ID; failed fetch parks reconciliation."""
    db, router = _MutationDatabase(), _Router()
    wake = Mock()
    handler = _handler(db, router, wake=wake)
    known = MessageView(MESSAGE, AuthorRef('discord-primary','55','human','Alice'), ContentDocument('fresh'), WHEN)
    event = MessageUpdated('discord-primary', MESSAGE, ContentDocument('stale'))
    reader = _Reader(view=known)
    await handler.handle_platform_message_edit(event, reader)
    assert reader.calls == [MESSAGE]
    assert router.handle_message_edit.await_args.kwargs['new_content'] == 'fresh'
    reader = _Reader(failure=PlatformOperationError('unavailable', 'timeout'))
    router.handle_message_edit.reset_mock()
    await handler.handle_platform_message_edit(event, reader)
    assert db.requests == [{'channel_id': 100, 'message_id': 200, 'error': 'timeout'}]
    assert wake.call_count == 1
    router.handle_message_edit.assert_not_awaited()


@pytest.mark.asyncio
async def test_typed_delete_is_id_only_and_bridge_echo_is_suppressed():
    """Cache-less deletion never fetches a message; echo state gates local deletes."""
    db, router = _MutationDatabase(), _Router()
    handler, reader = _handler(db, router), _Reader()
    handler.track_message_delete(200)
    event = MessageDeleted('discord-primary', MESSAGE, external_guild_id='42')
    await handler.handle_platform_message_delete(event, reader)
    assert db.lookups == [] and reader.calls == []
    router.handle_message_delete.assert_not_awaited()
    # Different ID with a canonical source role is genuinely admitted.
    event = MessageDeleted('discord-primary', MessageRef('discord-primary', '100', '201'))
    await handler.handle_platform_message_delete(event, reader)
    assert db.lookups == [201]
    assert router.handle_message_delete.await_args.kwargs['message_id'] == 201
