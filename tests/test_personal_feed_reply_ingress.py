"""Personal Feed reply Create through neutral ingress into the real SQLite publisher."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.db import Database
from src.discord_event_router import DiscordEventRouter
from src.personal_feed_conversations import PersonalFeedConversationRuntime
from src.personal_feeds import PersonalFeedService
from src.personal_feed_publisher import PersonalFeedPublisher
from src.platform_event_ingress import PlatformEventIngress
from src.platform_types import (
    AuthorRef, ChannelRef, ConnectorRef, ContentDocument, MessageCreated,
    MessageRef, ThreadRef, PlatformOperationError,
)
from src.models import User


class FakeGateway:
    """Render only at the remote ActivityPub boundary and record AP requests."""

    def __init__(self) -> None:
        """Start with no external effects."""
        self.requests = []

    async def render_personal_feed(self, request):
        """Return the gateway's published object while retaining request details."""
        from src.fedify_gateway_client import RenderPersonalFeedResult
        self.requests.append(request)
        return RenderPersonalFeedResult(request.activity_id, request.object_id,
                                        request.published_at, {'id': request.activity_id})


class Effects:
    """Reject a Create that is accidentally sent to mutation routes."""

    async def handle_platform_message_edit(self, event, adapter):
        """No edits are expected."""
        raise AssertionError('typed reply went to edit')

    async def handle_platform_message_delete(self, event, adapter):
        """No deletes are expected."""
        raise AssertionError('typed reply went to delete')


def _world(tmp_path):
    """Persist an owner, a published root and its real personal-thread mapping."""
    db = Database(f"sqlite:///{tmp_path / 'replies.sqlite3'}")
    db.migrate()
    actor='https://bridge.example/actors/users/alice'
    with db.session() as session:
        session.add(User(discord_user_id='10', activitypub_username='alice', actor_url=actor,
                         inbox_url=f'{actor}/inbox', outbox_url=f'{actor}/outbox',
                         followers_url=f'{actor}/followers', public_key_pem='public',
                         private_key_pem='private'))
    service = PersonalFeedService(db)
    service.bind_or_move('10', 1, 200)
    gateway = FakeGateway()
    permitted = SimpleNamespace(is_discord_guild_allowed=lambda value: value == 1)
    ban_service = SimpleNamespace(check_global_discord_user=lambda _: SimpleNamespace(banned=False))
    publisher = PersonalFeedPublisher(database=db, personal_feed_service=service,
                                     fedify_gateway=gateway, user_ban_service=ban_service,
                                     bridge_policy_service=permitted,
                                     fedify_origin='https://bridge.example')
    root = MessageCreated('discord-primary', MessageRef('discord-primary', '200', '300'),
                          AuthorRef('discord-primary', '10', 'human', 'Alice'),
                          ContentDocument('root'), parent_channel=ChannelRef('discord-primary', '200', 'text', '1'),
                          external_guild_id='1', created_at=datetime(2026,10,19,tzinfo=timezone.utc))
    runtime = PersonalFeedConversationRuntime(database=db, personal_feed_service=service,
                                              fedify_gateway=gateway, user_ban_service=ban_service,
                                              bridge_policy_service=permitted,
                                              fedify_origin='https://bridge.example')
    router = DiscordEventRouter(database=db, community_runtime=SimpleNamespace(),
                                local_community_runtime=SimpleNamespace(),
                                bridge_policy_service=permitted,
                                personal_feed_service=service, personal_feed_publisher=publisher,
                                personal_feed_conversation_runtime=runtime)
    ingress = PlatformEventIngress(adapter=SimpleNamespace(connector=ConnectorRef('discord-primary', 'discord')),
                                   mutations=Effects(), creates=router)
    return db, root, publisher, runtime, ingress, gateway


def _reply(*, connector='discord-primary', author='10', guild='1', parent='200', thread='300', msg='401', reference=None, content='one reply'):
    """Produce a verified neutral thread reply with stable Discord identity."""
    channel=ChannelRef(connector, parent, 'text', guild)
    return MessageCreated(connector, MessageRef(connector, thread, msg),
                          AuthorRef(connector, author, 'human', 'Alice'),
                          ContentDocument(content), parent_channel=channel,
                          thread_context=ThreadRef(channel, thread),
                          reply_to=(MessageRef(connector, thread, reference)
                                    if reference is not None else None),
                          external_guild_id=guild,
                          created_at=datetime(2026,10,19,10,20,tzinfo=timezone.utc))


@pytest.mark.asyncio
async def test_verified_personal_reply_publishes_once_and_persists_root_relation(tmp_path):
    """A registered author's duplicate Gateway reply produces one durable AP comment."""
    db, root, publisher, runtime, ingress, gateway = _world(tmp_path)
    try:
        published = await publisher.publish_normalized_root(event=root,
                        binding=PersonalFeedService(db).resolve_root_binding(1, 200))
        db.personal_feed_conversations.adopt_thread(root_object_id=published.object_id,
                       channel_id=200, root_message_id=300, thread_id=300)
        await ingress.handle_platform_event(_reply())
        await ingress.handle_platform_event(_reply())
        assert len(gateway.requests) == 2  # one root, one comment
        assert gateway.requests[-1].kind == 'comment'
        assert gateway.requests[-1].conversation_root_object_id == published.object_id
        mapped = db.message_mappings.get_message_mapping_by_discord_message_id(401)
        assert mapped is not None and mapped.publication_context == 'personal_feed'
    finally:
        db.close()


@pytest.mark.asyncio
async def test_replies_from_wrong_parent_or_unregistered_author_do_not_publish(tmp_path):
    """The current root binding and registered author are checked before AP effects."""
    db, root, publisher, runtime, ingress, gateway = _world(tmp_path)
    try:
        published = await publisher.publish_normalized_root(event=root,
                        binding=PersonalFeedService(db).resolve_root_binding(1, 200))
        db.personal_feed_conversations.adopt_thread(root_object_id=published.object_id,
                       channel_id=200, root_message_id=300, thread_id=300)
        await ingress.handle_platform_event(_reply(parent='201', msg='402'))
        await ingress.handle_platform_event(_reply(author='77', msg='403'))
        assert len(gateway.requests) == 1
        assert db.message_mappings.get_message_mapping_by_discord_message_id(402) is None
        assert db.message_mappings.get_message_mapping_by_discord_message_id(403) is None
    finally:
        db.close()


@pytest.mark.asyncio
async def test_foreign_connector_and_wrong_thread_are_not_accepted(tmp_path):
    """A forged connector or an unbound thread cannot create a canonical reply."""
    db, root, publisher, runtime, ingress, gateway = _world(tmp_path)
    try:
        with pytest.raises(PlatformOperationError, match='connector'):
            event=_reply()
            await ingress.handle_platform_event(_reply(connector='fluxer-example'))
        await ingress.handle_platform_event(_reply(thread='999'))
        assert gateway.requests == []
    finally:
        db.close()


@pytest.mark.asyncio
async def test_out_of_order_message_adopts_published_root_before_comment(tmp_path):
    """MESSAGE_CREATE before THREAD_CREATE still gets one canonical reply."""
    db, root, publisher, runtime, ingress, gateway = _world(tmp_path)
    try:
        published = await publisher.publish_normalized_root(
            event=root, binding=PersonalFeedService(db).resolve_root_binding(1, 200),
        )
        # The discussion thread has not yet fired its independent event.
        await ingress.handle_platform_event(_reply(msg='404'))
        assert db.personal_feed_conversations.get_root_for_thread(300).object_id == published.object_id
        assert db.message_mappings.get_message_mapping_by_discord_message_id(404) is not None
        assert len(gateway.requests) == 2
    finally:
        db.close()


@pytest.mark.asyncio
async def test_nested_comment_uses_saved_parent_mapping(tmp_path):
    """A Discord reply reference to an already published comment preserves nesting."""
    db, root, publisher, runtime, ingress, gateway = _world(tmp_path)
    try:
        published = await publisher.publish_normalized_root(
            event=root, binding=PersonalFeedService(db).resolve_root_binding(1, 200),
        )
        await ingress.handle_platform_event(_reply(msg='401'))
        first = db.message_mappings.get_message_mapping_by_discord_message_id(401)
        await ingress.handle_platform_event(_reply(msg='402', reference='401'))
        assert gateway.requests[-1].in_reply_to_object_id == first.object_id
        assert gateway.requests[-1].conversation_root_object_id == published.object_id
        assert len(gateway.requests) == 3
    finally:
        db.close()


@pytest.mark.asyncio
async def test_registration_notice_failure_never_creates_ap_comment(tmp_path):
    """An unregistered source is rejected even when its physical notice fails."""
    db, root, publisher, runtime, ingress, gateway = _world(tmp_path)
    try:
        await publisher.publish_normalized_root(
            event=root, binding=PersonalFeedService(db).resolve_root_binding(1, 200),
        )
        class NoticeEdge:
            """Inspect one outer Discord operation and emulate a lost HTTP reply."""
            def __init__(self):
                """Collect attempted replies to the unregistered author."""
                self.calls = []
            async def send_message(self, destination, content, *, reply_to, presentation, attempt):
                """Fail the outbound physical message after collecting typed identity."""
                self.calls.append((destination, content, reply_to, attempt))
                raise RuntimeError('Discord API unavailable')
        edge = NoticeEdge()
        ingress.creates.bind_reply_notice_adapter(edge)
        await ingress.handle_platform_event(_reply(author='77', msg='403'))
        assert len(edge.calls) == 1
        destination, content, reply_to, attempt = edge.calls[0]
        assert reply_to.external_message_id == '403'
        assert destination.external_thread_id == '300'
        assert attempt.key.endswith(':300:403')
        assert len(gateway.requests) == 1
        assert db.message_mappings.get_message_mapping_by_discord_message_id(403) is None
    finally:
        db.close()


@pytest.mark.asyncio
async def test_author_and_root_policy_fail_closed_without_side_effects(tmp_path):
    """A bot, a banned guild, or an unsupported attachment cannot be a reply."""
    db, root, publisher, runtime, ingress, gateway = _world(tmp_path)
    try:
        await publisher.publish_normalized_root(
            event=root, binding=PersonalFeedService(db).resolve_root_binding(1, 200),
        )
        from dataclasses import replace
        bot = replace(_reply(msg='450'), author=AuthorRef('discord-primary', '11', 'bot', 'bot'))
        await ingress.handle_platform_event(bot)
        await ingress.handle_platform_event(_reply(guild='2', msg='451'))
        with pytest.raises(PlatformOperationError, match='attachment'):
            from src.platform_types import AttachmentRef
            await ingress.handle_platform_event(replace(
                _reply(msg='452'), content=ContentDocument(
                    'media', attachments=(AttachmentRef('https://cdn.example/media', 'photo.png'),),
                ),
            ))
        assert len(gateway.requests) == 1
        assert all(db.message_mappings.get_message_mapping_by_discord_message_id(x) is None
                   for x in (450, 451, 452))
    finally:
        db.close()


@pytest.mark.asyncio
async def test_physical_thread_message_traverses_adapter_and_real_router(tmp_path):
    """One observed thread reply passes adapter, ingress, router and real SQLite."""
    from src.discord_adapter import DiscordAdapter
    from src.platform_types import ChannelRef

    db, root, publisher, runtime, ingress, gateway = _world(tmp_path)
    try:
        await publisher.publish_normalized_root(
            event=root, binding=PersonalFeedService(db).resolve_root_binding(1, 200),
        )
        class FakeThread:
            """Stand in only for the SDK's physical Discord Thread type."""
        guild = SimpleNamespace(id=1)
        parent = SimpleNamespace(id=200, guild=guild)
        thread = FakeThread()
        thread.id = 300
        thread.parent_id = 200
        thread.parent = parent
        thread.guild = guild
        msg = SimpleNamespace(
            id=500, guild=guild, channel=thread,
            author=SimpleNamespace(id=10, bot=False, display_name='Alice'),
            webhook_id=None, content='reply via adapter', reference=None,
            created_at=datetime(2026,10,19,10,20,tzinfo=timezone.utc),
            attachments=(), type='default',
        )
        adapter = DiscordAdapter(connector=ConnectorRef('discord-primary', 'discord'),
                                 bot=SimpleNamespace(user=SimpleNamespace(id=900)))
        adapter._sdk = lambda: SimpleNamespace(Thread=FakeThread)
        adapter._validate_channel = lambda channel, observed: ChannelRef(
            'discord-primary', '200', 'text', '1',
        )
        ingress.adapter = adapter
        adapter.bind_ingress_sink(ingress)
        await adapter.ingest_thread_message_created(msg)
        await adapter.ingest_thread_message_created(msg)
        assert len(gateway.requests) == 2
        assert db.message_mappings.get_message_mapping_by_discord_message_id(500) is not None
    finally:
        db.close()


@pytest.mark.asyncio
async def test_reply_reference_to_another_personal_root_does_not_cross_conversations(tmp_path):
    """A forged reply-to ID belonging to another root cannot change AP parent."""
    db, root, publisher, runtime, ingress, gateway = _world(tmp_path)
    try:
        first = await publisher.publish_normalized_root(
            event=root, binding=PersonalFeedService(db).resolve_root_binding(1, 200),
        )
        from dataclasses import replace
        other = replace(root, message=MessageRef('discord-primary', '200', '310'))
        second = await publisher.publish_normalized_root(
            event=other, binding=PersonalFeedService(db).resolve_root_binding(1, 200),
        )
        db.personal_feed_conversations.adopt_thread(
            root_object_id=first.object_id, channel_id=200, root_message_id=300, thread_id=300,
        )
        await ingress.handle_platform_event(_reply(msg='410', reference='310'))
        assert gateway.requests[-1].in_reply_to_object_id == first.object_id
        assert gateway.requests[-1].in_reply_to_object_id != second.object_id
    finally:
        db.close()


@pytest.mark.asyncio
async def test_opaque_non_discord_thread_cannot_reach_legacy_sqlite_projection(tmp_path):
    """Connector-only DTO validation never turns arbitrary external IDs into Discord integers."""
    db, root, publisher, runtime, ingress, gateway = _world(tmp_path)
    try:
        from dataclasses import replace
        original = _reply()
        thread = ThreadRef(original.parent_channel, 'opaque-thread')
        foreign_id = replace(original, thread_context=thread,
                             message=MessageRef('discord-primary', 'opaque-thread', '401'))
        with pytest.raises(PlatformOperationError, match='snowflake'):
            await ingress.handle_platform_event(foreign_id)
        assert db.message_mappings.get_message_mapping_by_discord_message_id(401) is None
        assert gateway.requests == []
    finally:
        db.close()
