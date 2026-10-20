"""Discord root Gateway actions -> neutral Personal Feed publication and SQL identity."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.db import Database
from src.models import User
from src.personal_feeds import PersonalFeedService
from src.personal_feed_publisher import PersonalFeedPublisher
from src.platform_event_ingress import PlatformEventIngress
from src.platform_types import (
    AuthorRef, ChannelRef, ConnectorRef, ContentDocument, MessageCreated,
    MessageRef, ThreadCreated, ThreadRef, PlatformOperationError,
)


CREATED = datetime(2026, 10, 19, 10, 20, 30, tzinfo=timezone.utc)


class Renderer:
    """Render a deterministic ActivityPub payload at the only external HTTP edge."""

    def __init__(self):
        """Record physical requests to detect redundant publications."""
        self.requests = []

    async def render_personal_feed(self, request):
        """Return only the identity expected by the durable publisher."""
        from src.fedify_gateway_client import RenderPersonalFeedResult
        self.requests.append(request)
        return RenderPersonalFeedResult(request.activity_id, request.object_id,
                                        request.published_at, {'id': request.activity_id,
                                        'object': {'id': request.object_id}})


class RootRoute:
    """Connect neutral creates to the existing binding/publisher service."""

    def __init__(self, publisher, service):
        """Keep the durable Personal Feed binding as the only routing truth."""
        self.publisher = publisher
        self.service = service
        self.received = []

    async def handle_platform_message_create(self, event):
        """Reject unrelated creates and invoke the neutral publisher directly."""
        self.received.append(event)
        if event.thread_context is not None or event.parent_channel.kind not in ('text', 'announcement'):
            return
        binding = self.service.resolve_root_binding(int(event.external_guild_id),
                                                    int(event.message.external_container_id))
        if binding is not None:
            return await self.publisher.publish_normalized_root(event=event, binding=binding)


class MutationEffects:
    """Fail loudly if a Create is confused with an edit or delete."""

    async def handle_platform_message_edit(self, event, adapter):
        """No edit is permitted in this scenario."""
        raise AssertionError('Create incorrectly routed as edit')

    async def handle_platform_message_delete(self, event, adapter):
        """No delete is permitted in this scenario."""
        raise AssertionError('Create incorrectly routed as delete')


def _event(*, author='10', connector='discord-primary', kind='text', timestamp=CREATED,
           guild='100', thread=False):
    """Construct the exact DTO that the adapter will emit for a source root."""
    channel = ChannelRef(connector, '200', kind, guild)
    return MessageCreated(
        connector, MessageRef(connector, '200', '300'),
        AuthorRef(connector, author, 'human' if author else 'unknown', 'Owner'),
        ContentDocument('root content'), parent_channel=channel,
        external_guild_id=guild, created_at=timestamp,
    )


def _world(tmp_path):
    """Build real SQLite identity/bindings and a deterministic fake gateway."""
    db = Database(f"sqlite:///{tmp_path / 'feed.sqlite3'}")
    db.migrate()
    with db.session() as session:
        session.add(User(discord_user_id='10', activitypub_username='alice',
                         actor_url='https://bridge.example/actors/users/alice',
                         inbox_url='https://bridge.example/actors/users/alice/inbox',
                         outbox_url='https://bridge.example/actors/users/alice/outbox',
                         followers_url='https://bridge.example/actors/users/alice/followers',
                         public_key_pem='public', private_key_pem='private'))
    service = PersonalFeedService(db)
    binding = service.bind_or_move('10', 100, 200).binding
    render = Renderer()
    publisher = PersonalFeedPublisher(
        database=db, personal_feed_service=service, fedify_gateway=render,
        user_ban_service=SimpleNamespace(check_global_discord_user=lambda _id: SimpleNamespace(banned=False)),
        bridge_policy_service=SimpleNamespace(is_discord_guild_allowed=lambda _id: True),
        fedify_origin='https://bridge.example',
    )
    route = RootRoute(publisher, service)
    ingress = PlatformEventIngress(
        adapter=SimpleNamespace(connector=ConnectorRef('discord-primary', 'discord')),
        mutations=MutationEffects(), creates=route,
    )
    return db, service, route, ingress, render


@pytest.mark.asyncio
async def test_gateway_owner_root_publishes_once_and_preserves_original_time(tmp_path):
    """Repeated owner Create produces one persisted canonical note and one render."""
    db, service, route, ingress, renderer = _world(tmp_path)
    try:
        await ingress.handle_platform_event(_event())
        await ingress.handle_platform_event(_event())
        assert len(renderer.requests) == 1
        assert renderer.requests[0].published_at == '2026-10-19T10:20:30.000Z'
        mapping = db.message_mappings.get_message_mapping_by_discord_message_id(300)
        assert mapping is not None and mapping.publication_context == 'personal_feed'
        assert len(route.received) == 2
    finally:
        db.close()


@pytest.mark.asyncio
async def test_bad_connector_and_missing_author_have_no_root_effects(tmp_path):
    """Another connector or unknown author cannot produce a Discord User Actor note."""
    db, _, route, ingress, renderer = _world(tmp_path)
    try:
        with pytest.raises(PlatformOperationError, match='connector'):
            await ingress.handle_platform_event(_event(connector='fluxer-a'))
        await ingress.handle_platform_event(_event(author=None))
        assert route.received == [] or all(e.author.external_actor_id is None for e in route.received)
        assert renderer.requests == []
        assert db.message_mappings.get_message_mapping_by_discord_message_id(300) is None
    finally:
        db.close()


@pytest.mark.asyncio
async def test_changed_binding_and_forum_root_do_not_publish(tmp_path):
    """Moved subscription or forum container cannot be treated as current text root."""
    db, service, route, ingress, renderer = _world(tmp_path)
    try:
        service.bind_or_move('10', 100, 201)
        await ingress.handle_platform_event(_event())
        with pytest.raises(RuntimeError, match='community create ingress'):
            # The real router must fail loudly until forum Create is migrated.
            from src.discord_event_router import DiscordEventRouter
            real_router = DiscordEventRouter(
                database=db, community_runtime=SimpleNamespace(),
                local_community_runtime=SimpleNamespace(),
                bridge_policy_service=SimpleNamespace(is_discord_guild_allowed=lambda _id: True),
                personal_feed_service=service, personal_feed_publisher=route.publisher,
            )
            ingress.creates = real_router
            await ingress.handle_platform_event(_event(kind='forum'))
        assert renderer.requests == []
        assert db.message_mappings.get_message_mapping_by_discord_message_id(300) is None
    finally:
        db.close()


@pytest.mark.asyncio
async def test_missing_source_time_is_not_replaced_by_received_time(tmp_path):
    """Missing original Discord timestamp fails before Fedify or DB side effects."""
    db, _, _, ingress, renderer = _world(tmp_path)
    try:
        await ingress.handle_platform_event(_event(timestamp=None))
        assert renderer.requests == []
        assert db.message_mappings.get_message_mapping_by_discord_message_id(300) is None
    finally:
        db.close()

@pytest.mark.asyncio
async def test_sdk_gateway_message_reaches_real_router_and_sql_once(tmp_path):
    """A physical text-channel user post enters adapter -> real router -> SQLite."""
    from src.discord_adapter import DiscordAdapter
    from src.discord_event_router import DiscordEventRouter

    db, service, fixture_route, _, renderer = _world(tmp_path)
    try:
        router = DiscordEventRouter(
            database=db, community_runtime=SimpleNamespace(),
            local_community_runtime=SimpleNamespace(),
            bridge_policy_service=SimpleNamespace(is_discord_guild_allowed=lambda _id: True),
            personal_feed_service=service,
            personal_feed_publisher=fixture_route.publisher,
        )
        connector = ConnectorRef('discord-primary', 'discord')
        adapter = DiscordAdapter(connector=connector, bot=SimpleNamespace(user=SimpleNamespace(id=900)))
        adapter.bind_ingress_sink(PlatformEventIngress(
            adapter=adapter, mutations=MutationEffects(), creates=router,
        ))
        # At the SDK boundary the channel is already independently verified.
        # This fake replaces only the physical discord.py type identity check.
        adapter._validate_channel = lambda requested, observed: ChannelRef(
            connector.key, '200', 'text', '100',
        )
        guild = SimpleNamespace(id=100)
        channel = SimpleNamespace(id=200, guild=guild, parent=None, parent_id=None)
        message = SimpleNamespace(
            id=300, guild=guild, channel=channel,
            author=SimpleNamespace(id=10, bot=False, display_name='Alice'),
            webhook_id=None, type='default', reference=None,
            content='root content', created_at=CREATED, attachments=(),
        )
        await adapter.ingest_message_created(message)
        await adapter.ingest_message_created(message)
        assert len(renderer.requests) == 1
        assert db.message_mappings.get_message_mapping_by_discord_message_id(300) is not None
        # Absent parent evidence is a hard rejection, not an arbitrary root.
        channel.parent_id = 123
        with pytest.raises(ValueError, match='parent'):
            await adapter.ingest_message_created(message)
        # An unsupported attachment must not be silently omitted from AP.
        message.attachments = (SimpleNamespace(url='https://cdn.example/image.png'),)
        with pytest.raises(PlatformOperationError, match='attachment'):
            await adapter.ingest_message_created(message)
        assert len(renderer.requests) == 1
    finally:
        db.close()

@pytest.mark.asyncio
async def test_manual_reply_thread_adopts_only_exact_published_personal_root(tmp_path):
    """User starts a thread from a published root -> typed event adopts only that root."""
    from src.personal_feed_conversations import PersonalFeedConversationRuntime
    from src.discord_event_router import DiscordEventRouter
    from src.discord_gateway_events import DiscordGatewayNormalizer

    db, service, fixture_route, ingress, _ = _world(tmp_path)
    try:
        await ingress.handle_platform_event(_event())
        runtime = PersonalFeedConversationRuntime(
            database=db, personal_feed_service=service,
            fedify_gateway=SimpleNamespace(), user_ban_service=SimpleNamespace(),
            bridge_policy_service=SimpleNamespace(), fedify_origin='https://bridge.example',
        )
        router = DiscordEventRouter(
            database=db, community_runtime=SimpleNamespace(),
            local_community_runtime=SimpleNamespace(),
            bridge_policy_service=SimpleNamespace(is_discord_guild_allowed=lambda _id: True),
            personal_feed_service=service, personal_feed_publisher=fixture_route.publisher,
            personal_feed_conversation_runtime=runtime,
        )
        ingress.creates = router
        normalizer = DiscordGatewayNormalizer('discord-primary', owned_bot_id=None)
        guild = SimpleNamespace(id=100)
        parent = SimpleNamespace(id=200, guild=guild)
        thread = SimpleNamespace(id=300, parent_id=200, parent=parent, guild=guild,
                                 name='Discussion', owner_id=10, starter_message=None)
        event = normalizer.thread_created(thread, parent_kind='text')
        await ingress.handle_platform_event(event)
        await ingress.handle_platform_event(event)
        assert db.personal_feed_conversations.get_thread_by_thread_id(300) is not None
        # Same thread ID under another parent cannot rewrite the binding.
        wrong = ThreadCreated('discord-primary', ThreadRef(ChannelRef('discord-primary', '201', 'text', '100'), '300'), 'Other parent', external_guild_id='100')
        await ingress.handle_platform_event(wrong)
        assert db.personal_feed_conversations.get_thread_by_thread_id(300).root_discord_channel_id == 200
    finally:
        db.close()


def test_text_root_thread_normalization_rejects_unverified_or_nonroot_identity():
    """Only a verified root-anchored thread may be treated as a Personal Feed thread."""
    from src.discord_gateway_events import DiscordGatewayNormalizer
    normalizer = DiscordGatewayNormalizer('discord-primary', owned_bot_id=None)
    parent = SimpleNamespace(id=200, guild=SimpleNamespace(id=100))
    raw = SimpleNamespace(id=300, parent_id=200, parent=parent, guild=parent.guild,
                          name='Discussion', owner_id=10, starter_message=None)
    assert normalizer.thread_created(raw, parent_kind='text') is not None
    bad = SimpleNamespace(**{**raw.__dict__, 'parent': None})
    assert normalizer.thread_created(bad, parent_kind='text') is None

@pytest.mark.asyncio
async def test_adapter_physical_manual_thread_adopts_with_verified_parent(tmp_path):
    """Gateway thread with exact root/parent is mapped without fetching a starter."""
    from src.discord_adapter import DiscordAdapter
    from src.discord_event_router import DiscordEventRouter
    from src.personal_feed_conversations import PersonalFeedConversationRuntime

    db, service, fixture_route, ingress, _ = _world(tmp_path)
    try:
        await ingress.handle_platform_event(_event())
        runtime = PersonalFeedConversationRuntime(
            database=db, personal_feed_service=service,
            fedify_gateway=SimpleNamespace(), user_ban_service=SimpleNamespace(),
            bridge_policy_service=SimpleNamespace(), fedify_origin='https://bridge.example',
        )
        router = DiscordEventRouter(
            database=db, community_runtime=SimpleNamespace(),
            local_community_runtime=SimpleNamespace(),
            bridge_policy_service=SimpleNamespace(is_discord_guild_allowed=lambda _id: True),
            personal_feed_service=service, personal_feed_conversation_runtime=runtime,
            personal_feed_publisher=fixture_route.publisher,
        )
        adapter = DiscordAdapter(
            connector=ConnectorRef('discord-primary', 'discord'),
            bot=SimpleNamespace(user=SimpleNamespace(id=900)),
        )
        adapter.bind_ingress_sink(PlatformEventIngress(
            adapter=adapter, mutations=MutationEffects(), creates=router,
        ))
        class PhysicalThread:
            """SDK boundary fake with explicitly observed thread metadata."""
        adapter._sdk = lambda: SimpleNamespace(Thread=PhysicalThread)
        adapter._validate_channel = lambda expected, parent: ChannelRef(
            'discord-primary', str(parent.id), 'announcement', '100',
        )
        guild = SimpleNamespace(id=100)
        parent = SimpleNamespace(id=200, guild=guild)
        physical = PhysicalThread()
        physical.id = 300
        physical.parent_id = 200
        physical.parent = parent
        physical.guild = guild
        physical.name = 'Discussion'
        physical.owner_id = 10
        physical.starter_message = None
        await adapter.ingest_thread_created(physical)
        assert db.personal_feed_conversations.get_thread_by_thread_id(300) is not None
        physical.parent = SimpleNamespace(id=201, guild=guild)
        with pytest.raises(PlatformOperationError, match='parent'):
            await adapter.ingest_thread_created(physical)
    finally:
        db.close()
