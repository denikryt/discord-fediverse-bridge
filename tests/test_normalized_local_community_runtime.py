"""File-backed SQLite scenarios for Local Community DTO ingress before Gateway cutover.

These tests exercise the real runtime and persistence with only AP and Discord
network boundaries faked; each scenario checks canonical and physical effects.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.content_publish_service import ContentPublishService
from src.fedify_gateway_client import PublishContentResult
from src.local_communities.runtime import LocalCommunityRuntime
from src.models import LocalSubscriber
from src.local_communities.service import LocalCommunityService
from src.platform_types import (
    AuthorRef, ChannelRef, ContentDocument, CreatedMessage, CreatedThread,
    MessageCreated, MessageRef, MessageView, PlatformOperationError,
    ThreadCreated, ThreadRef,
)
from support.db import add_registered_user, build_database


class Gateway:
    """Fake ActivityPub HTTP only while recording the actual publish payloads."""

    def __init__(self):
        """Start with no federation requests."""
        self.requests = []

    async def publish_local_community_content(self, request):
        """Return distinct confirmed canonical identifiers for each publish."""
        self.requests.append(request)
        n = len(self.requests)
        return PublishContentResult(
            activity_id=f'https://bridge.example/actors/users/alice/activities/{n}',
            object_id=f'https://bridge.example/objects/{n}',
            community_actor_url=request.community_actor_url,
        )


class PhysicalTransport:
    """Fake physical Discord API receipts, not routing or SQLite repositories."""

    def __init__(self, *, failed=None):
        """Track create calls and allow one selected target to fail."""
        self.failed = failed
        self.posts = []
        self.replies = []

    async def create_forum_thread(self, forum, content, *, title, presentation, attempt):
        """Confirm distinct forum-thread and starter IDs for each target."""
        self.posts.append((forum, content, attempt))
        if forum.external_channel_id == self.failed:
            raise OSError('destination down')
        target = int(forum.external_channel_id)
        # Different canonical posts in the same destination are distinct
        # physical threads; first delivery keeps the readable stable IDs.
        generation = sum(p[0].external_channel_id == forum.external_channel_id
                         for p in self.posts) - 1
        thread = ThreadRef(forum, str(target + 1000 + generation * 10000))
        return CreatedThread(thread, MessageRef('discord-primary', thread.external_thread_id,
                                                str(target + 2000 + generation * 10000)), 'bot')

    async def send_message(self, destination, content, *, reply_to, presentation, attempt):
        """Confirm unique reply IDs and expose the per-recipient parent reference."""
        self.replies.append((destination, reply_to, content, attempt))
        if destination.channel.external_channel_id == self.failed:
            raise OSError('destination down')
        n = 7000 + len(self.replies)
        return CreatedMessage(MessageRef('discord-primary', destination.external_thread_id,
                                         str(n)), 'bot')


def world(tmp_path, *, fail=None):
    """Create one host, two active subscribers, registered author and real runtime."""
    db = build_database(tmp_path, 'local-normalized-runtime.sqlite3')
    add_registered_user(db)
    LocalCommunityService(
        database=db, base_url='https://bridge.example',
        keypair_generator=lambda: ('public', 'private'),
    ).create_local_community(
        discord_guild_id=10, discord_forum_channel_id=100, slug='hackers',
        name='Hackers', description='Community', created_by_discord_user_id='123',
    )
    community = db.local_communities.get_local_community_by_slug('hackers')
    subscribers = {}
    for forum in (200, 300):
        subscribers[forum] = db.local_subscribers.create_local_subscriber(
            local_community_id=community.id, discord_guild_id=10,
            discord_channel_id=forum, initiated_by_discord_user_id='123',
        )
    gateway = Gateway()
    policy = SimpleNamespace(
        is_discord_guild_allowed=lambda guild: guild == 10,
        federation_decision=lambda actor: SimpleNamespace(allowed=True),
        snapshot=lambda: SimpleNamespace(federation_decision=lambda actor: SimpleNamespace(allowed=True)),
    )
    publisher = ContentPublishService(
        database=db, fedify_gateway=gateway,
        bridge_prefix='[bridge]', bridge_policy_service=policy,
    )
    runtime = LocalCommunityRuntime(
        database=db, fedify_gateway=gateway, content_publish_service=publisher,
        bridge_prefix='[bridge]', bridge_policy_service=policy,
    )
    return db, community, subscribers, runtime, gateway, PhysicalTransport(failed=fail)


def starter(*, forum=100, guild=10, thread=1100, message=1101, author='123'):
    """Represent a verified forum thread with independent starter identity."""
    channel = ChannelRef('discord-primary', str(forum), 'forum', str(guild))
    opening = MessageRef('discord-primary', str(thread), str(message))
    event = ThreadCreated('discord-primary', ThreadRef(channel, str(thread)),
                          'Topic', starter=opening, external_guild_id=str(guild))
    view = MessageView(opening, AuthorRef('discord-primary', author, 'human', 'Alice'),
                       ContentDocument('Post body'))
    return event, view


def reply(*, forum=100, guild=10, thread=1100, message=1200, parent=1101):
    """Supply a reply from one known Discord physical surface."""
    channel = ChannelRef('discord-primary', str(forum), 'forum', str(guild))
    return MessageCreated(
        'discord-primary', MessageRef('discord-primary', str(thread), str(message)),
        AuthorRef('discord-primary', '123', 'human', 'Alice'), ContentDocument('Comment'),
        parent_channel=channel, thread_context=ThreadRef(channel, str(thread)),
        external_guild_id=str(guild),
        reply_to=MessageRef('discord-primary', str(thread), str(parent)) if parent else None,
    )


@pytest.mark.asyncio
async def test_host_starter_creates_one_ap_post_and_two_subscriber_surfaces(tmp_path):
    """Host starter is canonical once and each subscriber receives a separate copy."""
    db, community, _, runtime, gateway, port = world(tmp_path)
    try:
        event, opening = starter()
        first = await runtime.handle_normalized_thread_create(event, opening, adapter=port)
        second = await runtime.handle_normalized_thread_create(event, opening, adapter=port)
        assert first.status == 'published'
        assert second.reason == 'duplicate_thread'
        assert len(gateway.requests) == 1
        assert [x[0].external_channel_id for x in port.posts] == ['200', '300']
        physical = db.local_community_surfaces.get_local_community_thread_surface_by_discord_thread_id(1100)
        assert physical.role == 'host' and physical.discord_starter_message_id == 1101
        canonical = db.local_community_surfaces.get_local_community_thread_for_surface(physical.id)
        assert len(db.local_community_surfaces.list_local_community_thread_surfaces(canonical.id)) == 3
    finally:
        db.close()


@pytest.mark.asyncio
async def test_subscriber_starter_preserves_source_and_copies_to_host_and_peer(tmp_path):
    """Subscriber authors a canonical post, retaining subscriber source ownership."""
    db, community, subscribers, runtime, gateway, port = world(tmp_path)
    try:
        event, opening = starter(forum=200, thread=4400, message=4401)
        first = await runtime.handle_normalized_thread_create(event, opening, adapter=port)
        assert first.status == 'published'
        assert [x[0].external_channel_id for x in port.posts] == ['100', '300']
        source = db.local_community_surfaces.get_local_community_thread_surface_by_discord_thread_id(4400)
        assert source.role == 'local_subscriber' and source.local_subscriber_id == subscribers[200].id
        canonical = db.local_community_surfaces.get_local_community_thread_for_surface(source.id)
        assert canonical.origin_kind == 'discord_local_subscriber'
        again = await runtime.handle_normalized_thread_create(event, opening, adapter=port)
        assert again.reason == 'duplicate_thread' and len(port.posts) == 2
        assert len(gateway.requests) == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_host_reply_and_nested_reply_use_target_local_parents(tmp_path):
    """Root and nested comments retain canonical AP parents and surface-specific IDs."""
    db, _, _, runtime, gateway, port = world(tmp_path)
    try:
        event, opening = starter()
        await runtime.handle_normalized_thread_create(event, opening, adapter=port)
        first = await runtime.handle_normalized_message_create(reply(), adapter=port)
        assert first.status == 'published'
        assert [r[1].external_message_id for r in port.replies] == ['2200', '2300']
        nested = await runtime.handle_normalized_message_create(reply(message=1201, parent=1200), adapter=port)
        assert nested.status == 'published'
        assert [r[1].external_message_id for r in port.replies[2:]] == ['7001', '7002']
        assert gateway.requests[1].in_reply_to_object_id == 'https://bridge.example/objects/1'
        # Nested AP reply must point at the first canonical comment, not the root.
        assert gateway.requests[2].in_reply_to_object_id == 'https://bridge.example/objects/2'
        replay = await runtime.handle_normalized_message_create(reply(), adapter=port)
        assert replay.reason == 'duplicate_message' and len(gateway.requests) == 3
    finally:
        db.close()


@pytest.mark.asyncio
async def test_wrong_guild_and_foreign_connector_have_no_external_effects(tmp_path):
    """A matching numeric forum is not sufficient to authorize publication."""
    db, _, _, runtime, gateway, port = world(tmp_path)
    try:
        event, opening = starter(guild=11)
        rejected = await runtime.handle_normalized_thread_create(event, opening, adapter=port)
        assert rejected.status == 'ignored'
        forum = ChannelRef('fluxer-primary', '100', 'forum', '10')
        thread = ThreadRef(forum, '1100')
        msg = MessageRef('fluxer-primary', '1100', '1101')
        foreign = ThreadCreated('fluxer-primary', thread, 'Topic', starter=msg, external_guild_id='10')
        view = MessageView(msg, AuthorRef('fluxer-primary', '123', 'human', 'Alice'), ContentDocument('Post'))
        with pytest.raises(PlatformOperationError):
            await runtime.handle_normalized_thread_create(foreign, view, adapter=port)
        assert not gateway.requests and not port.posts
    finally:
        db.close()


@pytest.mark.asyncio
async def test_unregistered_author_and_revoked_source_cannot_publish(tmp_path):
    """Authorization denies an unregistered author and deactivated subscriber."""
    db, community, subscribers, runtime, gateway, port = world(tmp_path)
    try:
        event, opening = starter(author='999')
        result = await runtime.handle_normalized_thread_create(event, opening, adapter=port)
        assert result.reason == 'unregistered_user'
        with db.immediate_session() as session:
            session.get(LocalSubscriber, subscribers[200].id).status = 'removed'
        event, opening = starter(forum=200, thread=4400, message=4401)
        result = await runtime.handle_normalized_thread_create(event, opening, adapter=port)
        assert result.status == 'ignored'
        assert not gateway.requests and not port.posts
    finally:
        db.close()

@pytest.mark.asyncio
async def test_subscriber_mirror_gateway_replay_does_not_repair_using_untrusted_body(tmp_path):
    """A subscriber mirror cannot become a source merely because its role is subscriber."""
    db, _, _, runtime, gateway, port = world(tmp_path, fail='300')
    try:
        authored, opening = starter(forum=200, thread=4400, message=4401)
        await runtime.handle_normalized_thread_create(authored, opening, adapter=port)
        assert [x[0].external_channel_id for x in port.posts] == ['100', '300']
        # A mirror can receive a Gateway replay before a different subscriber
        # recovers, but it must not supply text to a new physical create.
        mirror_event, mirror_opening = starter(forum=100, thread=1100, message=2100)
        duplicate = await runtime.handle_normalized_thread_create(
            mirror_event, mirror_opening, adapter=port
        )
        assert duplicate.reason == 'duplicate_thread'
        assert len(port.posts) == 2 and len(gateway.requests) == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_subscriber_comment_preserves_source_parent_and_host_mirror(tmp_path):
    """Human comment from a subscriber carries source ownership into host copy."""
    db, _, _, runtime, gateway, port = world(tmp_path)
    try:
        event, opening = starter(forum=200, thread=4400, message=4401)
        await runtime.handle_normalized_thread_create(event, opening, adapter=port)
        result = await runtime.handle_normalized_message_create(
            reply(forum=200, thread=4400, message=4500, parent=4401), adapter=port
        )
        assert result.status == 'published'
        assert [x[0].channel.external_channel_id for x in port.replies] == ['100', '300']
        assert [x[1].external_message_id for x in port.replies] == ['2100', '2300']
        source = db.local_community_surfaces.get_local_community_message_surface_by_discord_message_id(4500)
        assert source.role == 'local_subscriber'
        canonical = db.local_community_surfaces.get_local_community_message_for_surface(source.id)
        assert canonical.parent_ap_object_id == 'https://bridge.example/objects/1'
        again = await runtime.handle_normalized_message_create(
            reply(forum=200, thread=4400, message=4500, parent=4401), adapter=port
        )
        assert again.reason == 'duplicate_message'
        assert len(port.replies) == 2 and len(gateway.requests) == 2
    finally:
        db.close()


@pytest.mark.asyncio
async def test_foreign_parent_mapping_is_rejected_before_federation(tmp_path):
    """Even a valid mapped Discord parent cannot be borrowed from another forum."""
    db, _, _, runtime, gateway, port = world(tmp_path)
    try:
        original, opening = starter()
        await runtime.handle_normalized_thread_create(original, opening, adapter=port)
        await runtime.handle_normalized_message_create(reply(), adapter=port)
        before = len(gateway.requests)
        with pytest.raises(PlatformOperationError):
            await runtime.handle_normalized_message_create(
                reply(message=1202, parent=7001), adapter=port
            )
        assert len(gateway.requests) == before
    finally:
        db.close()

@pytest.mark.asyncio
async def test_known_foreign_starter_cannot_be_used_as_root_reply_parent(tmp_path):
    """A starter from a different known thread is not an unknown flat reply."""
    db, _, _, runtime, gateway, port = world(tmp_path)
    try:
        event, opening = starter()
        await runtime.handle_normalized_thread_create(event, opening, adapter=port)
        other, other_opening = starter(forum=200, thread=4400, message=4401)
        await runtime.handle_normalized_thread_create(other, other_opening, adapter=port)
        before = len(gateway.requests)
        with pytest.raises(PlatformOperationError):
            await runtime.handle_normalized_message_create(
                reply(message=1250, parent=4401), adapter=port
            )
        assert len(gateway.requests) == before
    finally:
        db.close()
