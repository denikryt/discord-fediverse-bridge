"""A Gateway observation -> one neutral ingress -> owned domain mutation effect.

This suite checks the production-path dispatcher without a live Discord socket;
only the physical SDK and existing mutation service are faked at their edges.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.discord_gateway_events import DiscordGatewayNormalizer
from src.platform_types import (
    ChannelRef, ConnectorRef, ContentDocument, MessageCreated, MessageDeleted,
    MessageRef, MessageUpdated, PlatformOperationError, ThreadDeleted, ThreadRef,
    ThreadUpdated,
)
from src.platform_event_ingress import PlatformEventIngress


class MutationEffects:
    """Observe a canonical mutation once instead of counting SDK callbacks."""

    def __init__(self):
        """Collect persisted-effect boundary requests and any duplicate attempts."""
        self.edits = []
        self.deletes = []

    async def handle_platform_message_edit(self, event, reader):
        """Record a confirmed edit in the existing single effect path."""
        self.edits.append((event, reader))

    async def handle_platform_message_delete(self, event, reader):
        """Record a confirmed delete in the existing single effect path."""
        self.deletes.append((event, reader))


@pytest.fixture
def harness():
    """Attach a trusted connector adapter and independent domain effects."""
    effects = MutationEffects()
    adapter = SimpleNamespace(connector=ConnectorRef('discord-primary', 'discord'))
    return PlatformEventIngress(adapter=adapter, mutations=effects), effects, adapter


@pytest.mark.asyncio
async def test_raw_gateway_edit_goes_through_single_typed_mutation_path(harness):
    """An observed edit produces one canonical operation with source timestamp."""
    ingress, effects, adapter = harness
    event = DiscordGatewayNormalizer('discord-primary', owned_bot_id=None).raw_message_updated(
        SimpleNamespace(channel_id=20, message_id=30, guild_id=10, data={
            'content': 'updated', 'edited_timestamp': '2026-10-19T12:00:00+00:00'
        })
    )
    await ingress.handle_platform_event(event)
    assert len(effects.edits) == 1 and effects.deletes == []
    assert effects.edits[0][1] is adapter
    assert effects.edits[0][0].content.body == 'updated'
    assert effects.edits[0][0].edited_at == datetime(2026, 10, 19, 12, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_raw_gateway_delete_uses_no_sdk_lookup(harness):
    """An uncached raw deletion produces one canonical deletion without fetch."""
    ingress, effects, adapter = harness
    event = DiscordGatewayNormalizer('discord-primary', owned_bot_id=None).raw_message_deleted(
        SimpleNamespace(channel_id=20, message_id=30, guild_id=10)
    )
    await ingress.handle_platform_event(event)
    assert effects.edits == [] and effects.deletes == [(event, adapter)]


@pytest.mark.asyncio
async def test_foreign_connector_rejected_before_any_mutation_effect(harness):
    """A colliding numeric Fluxer ID never reaches Discord legacy persistence."""
    ingress, effects, _ = harness
    foreign = MessageDeleted('fluxer-main', MessageRef('fluxer-main', '20', '30'))
    with pytest.raises(PlatformOperationError, match='connector'):
        await ingress.handle_platform_event(foreign)
    assert effects.edits == [] and effects.deletes == []


@pytest.mark.asyncio
async def test_raw_thread_mutations_do_not_invent_ap_update_or_delete(harness, caplog):
    caplog.set_level("INFO", logger="src.platform_event_ingress")
    """Both uncached thread notices remain typed but have no AP mutation owner."""
    ingress, effects, _ = harness
    normalizer = DiscordGatewayNormalizer('discord-primary', owned_bot_id=None)
    await ingress.handle_platform_event(normalizer.raw_thread_updated(
        SimpleNamespace(id=44, parent_id=20, guild_id=10, data={'name': 'renamed'})
    ))
    await ingress.handle_platform_event(normalizer.raw_thread_deleted(
        SimpleNamespace(id=44, parent_id=20, guild_id=10)
    ))
    assert effects.edits == [] and effects.deletes == []
    assert 'no canonical mutation owner' in caplog.text


@pytest.mark.asyncio
async def test_create_not_accidentally_dropped_while_routing_is_not_bound(harness):
    """Unwired Create must fail loudly rather than silently discard a valid post."""
    ingress, effects, _ = harness
    from src.platform_types import AuthorRef
    event = MessageCreated(
        'discord-primary', MessageRef('discord-primary', '20', '30'),
        AuthorRef('discord-primary', '41', 'human', 'Alice'), ContentDocument('hello'),
        parent_channel=ChannelRef('discord-primary', '20', 'text', '10'),
    )
    with pytest.raises(RuntimeError, match='create ingress'):
        await ingress.handle_platform_event(event)
    assert effects.edits == [] and effects.deletes == []

@pytest.mark.asyncio
async def test_gateway_adapter_normalizes_once_and_routes_without_raw_handler():
    """An admitted SDK edit takes one neutral path, even without a cache hit."""
    from src.discord_adapter import DiscordAdapter
    client = SimpleNamespace(user=SimpleNamespace(id=99))
    adapter = DiscordAdapter(
        connector=ConnectorRef('discord-primary', 'discord'), bot=client,
    )
    effects = MutationEffects()
    sink = PlatformEventIngress(adapter=adapter, mutations=effects)
    adapter.bind_ingress_sink(sink)
    raw = SimpleNamespace(channel_id=40, message_id=80, guild_id=10, data={
        'content': 'changed', 'edited_timestamp': '2026-10-19T12:00:00Z',
        'author': {'id': '99', 'bot': True, 'username': 'bridge'},
    })
    assert adapter.enter_ingress()
    try:
        await adapter.ingest_raw_message_edit(raw)
        await adapter.ingest_raw_message_delete(
            SimpleNamespace(channel_id=40, message_id=80, guild_id=10)
        )
    finally:
        adapter.leave_ingress()
    assert len(effects.edits) == len(effects.deletes) == 1
    assert effects.edits[0][0].author.is_bridge_owned
    assert effects.deletes[0][0].message.external_container_id == '40'
    adapter.close_ingress_admission()
    await adapter.drain_ingress()
    assert not adapter.enter_ingress()


@pytest.mark.asyncio
async def test_gateway_admission_and_binding_fail_closed():
    """Unbound ingress or double binding cannot silently fall back to raw SDK."""
    from src.discord_adapter import DiscordAdapter
    adapter = DiscordAdapter(
        connector=ConnectorRef('discord-primary', 'discord'), bot=SimpleNamespace(user=None),
    )
    raw = SimpleNamespace(channel_id=40, message_id=80, guild_id=10)
    with pytest.raises(RuntimeError, match='not bound'):
        await adapter.ingest_raw_message_delete(raw)
    sink = PlatformEventIngress(adapter=adapter, mutations=MutationEffects())
    adapter.bind_ingress_sink(sink)
    with pytest.raises(RuntimeError, match='already bound'):
        adapter.bind_ingress_sink(sink)
    # After the SDK admission gate closes, no new event task is registered.
    adapter.close_ingress_admission()
    assert not adapter.enter_ingress()
