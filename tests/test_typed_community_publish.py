"""Verified forum starter publication using neutral platform observations and SQLite."""

from __future__ import annotations

from support.community_events import configure_renderer, OBSERVED_AT
from support.db import add_accepted_subscription
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.content_publish_service import ContentPublishService
from src.db import Database
from src.fedify_gateway_client import PublishContentResult
from src.platform_types import (AuthorRef, ChannelRef, ContentDocument, MessageRef,
                                MessageView, MessageCreated, ThreadCreated, ThreadRef,
                                PlatformOperationError)

COMMUNITY = 'https://remote.example/c/hackers'
ACTOR = 'https://bridge.example/actors/users/alice'


def world(tmp_path):
    """Persist one registered user and isolate the outbound AP boundary."""
    database = Database(f"sqlite:///{tmp_path / 'forum.sqlite3'}")
    database.migrate()
    database.users.create_user(discord_user_id='123', activitypub_username='alice',
                               actor_url=ACTOR, inbox_url=f'{ACTOR}/inbox',
                               outbox_url=f'{ACTOR}/outbox', followers_url=f'{ACTOR}/followers',
                               public_key_pem='public', private_key_pem='private')
    gateway = SimpleNamespace(publish_content=AsyncMock(return_value=PublishContentResult(
        activity_id=f'{ACTOR}/activities/create/posts/1', object_id=f'{ACTOR}/objects/posts/1',
        community_actor_url=COMMUNITY)))
    add_accepted_subscription(database, community_actor_url=COMMUNITY)
    configure_renderer(gateway, database)
    service = ContentPublishService(database=database, fedify_gateway=gateway,
                                    bridge_prefix='[bridge]', bridge_policy_service=SimpleNamespace())
    return database, gateway, service


def payload(*, connector='discord-primary', author_id='123', kind='human',
            starter_id='301', content='body'):
    """Describe a verified forum starter with thread and message IDs differing."""
    forum = ChannelRef(connector, '100', 'forum', '1')
    thread = ThreadRef(forum, '200')
    starter = MessageRef(connector, '200', starter_id)
    event = ThreadCreated(connector, thread, 'Topic', starter=starter, external_guild_id='1')
    snapshot = MessageView(starter, AuthorRef(connector, author_id, kind, 'Alice'),
                           ContentDocument(content), created_at=OBSERVED_AT)
    return event, snapshot


@pytest.mark.asyncio
async def test_verified_starter_publishes_original_two_ids_and_mapping(tmp_path):
    """The real publisher persists starter ID 301 rather than thread ID 200."""
    db, gateway, service = world(tmp_path)
    try:
        event, starter = payload()
        result = await service.publish_normalized_post_to_community(
            event=event, starter=starter, community_actor_url=COMMUNITY,
            publish_call=gateway.publish_content)
        assert result.status == 'published'
        mapping = db.message_mappings.get_by_source_ref(MessageRef('discord-primary', '200', str(301)))
        assert mapping is not None and mapping.discord_channel_id == 100
        assert db.message_mappings.get_by_source_ref(MessageRef('discord-primary', '200', str(200))) is None
        request = gateway.render_community.await_args.args[0]
        assert request.body_markdown.endswith('body')
        assert request.title == 'Topic'
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['bot', 'other_starter', 'unknown_content', 'wrong_kind', 'other_connector'])
async def test_invalid_starter_fails_before_gateway_and_storage(tmp_path, change):
    """Unverified physical starter metadata never enters an AP publish call."""
    db, gateway, service = world(tmp_path)
    try:
        event, starter = payload(kind='bot' if change == 'bot' else 'human',
                                 connector='fluxer-a' if change == 'other_connector' else 'discord-primary')
        if change == 'other_starter':
            starter = MessageView(MessageRef('discord-primary', '200', '302'), starter.author, starter.content)
        if change == 'unknown_content':
            starter = MessageView(starter.message, starter.author, None)
        if change == 'wrong_kind':
            forum = ChannelRef('discord-primary', '100', 'text', '1')
            event = ThreadCreated('discord-primary', ThreadRef(forum, '200'), 'Topic',
                                  starter=event.starter, external_guild_id='1')
        with pytest.raises(PlatformOperationError):
            await service.publish_normalized_post_to_community(
                event=event, starter=starter, community_actor_url=COMMUNITY,
                publish_call=gateway.publish_content)
        gateway.publish_content.assert_not_awaited()
        assert db.message_mappings.get_by_source_ref(MessageRef('discord-primary', '200', str(301))) is None
    finally:
        db.close()


@pytest.mark.asyncio
async def test_unregistered_starter_sends_only_one_adapter_notice(tmp_path):
    """A human with no registration receives a neutral transport notice, not AP Create."""
    db, gateway, service = world(tmp_path)
    try:
        event, starter = payload(author_id='999')
        transport = SimpleNamespace(send_message=AsyncMock())
        first = await service.publish_normalized_post_to_community(
            event=event, starter=starter, community_actor_url=COMMUNITY,
            publish_call=gateway.publish_content, notice_adapter=transport)
        assert (first.status, first.reason) == ('ignored', 'unregistered_user')
        args, kwargs = transport.send_message.await_args
        assert args[0] == event.thread
        assert kwargs['reply_to'] == starter.message
        assert kwargs['attempt'].key.endswith(':200:301')
        assert kwargs['attempt'].destination == event.thread
        gateway.publish_content.assert_not_awaited()
        assert db.message_mappings.get_by_source_ref(MessageRef('discord-primary', '200', str(301))) is None
    finally:
        db.close()


@pytest.mark.asyncio
async def test_failed_registration_notice_never_authorizes_federation(tmp_path):
    """A failed Discord notice has no effect on the unregistered user's refusal."""
    db, gateway, service = world(tmp_path)
    try:
        event, starter = payload(author_id='999')
        transport = SimpleNamespace(send_message=AsyncMock(side_effect=RuntimeError('offline')))
        result = await service.publish_normalized_post_to_community(
            event=event, starter=starter, community_actor_url=COMMUNITY,
            publish_call=gateway.publish_content, notice_adapter=transport)
        assert result.reason == 'unregistered_user'
        gateway.publish_content.assert_not_awaited()
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('ban_error', [False, True])
async def test_ban_or_unknown_ban_state_fails_closed_before_ap(tmp_path, ban_error):
    """Banned authors and unavailable moderation state produce zero AP activities."""
    db, gateway, service = world(tmp_path)
    try:
        event, starter = payload()
        if ban_error:
            moderation = SimpleNamespace(check_discord_user=lambda **_: (_ for _ in ()).throw(RuntimeError('db unavailable')))
        else:
            moderation = SimpleNamespace(check_discord_user=lambda **_: SimpleNamespace(banned=True, scope="global", reason_text="spam"))
        service.user_ban_service = moderation
        result = await service.publish_normalized_post_to_community(
            event=event, starter=starter, community_actor_url=COMMUNITY,
            publish_call=gateway.publish_content)
        assert result.status == 'rejected'
        assert result.reason == ('ban_check_failed' if ban_error else 'user_banned')
        gateway.publish_content.assert_not_awaited()
        assert db.message_mappings.get_by_source_ref(MessageRef('discord-primary', '200', str(301))) is None
    finally:
        db.close()


@pytest.mark.asyncio
async def test_zero_snowflake_never_reaches_gateway(tmp_path):
    """Positive legacy snowflakes are required before SQLite projection."""
    db, gateway, service = world(tmp_path)
    try:
        event, starter = payload(starter_id='0')
        with pytest.raises(PlatformOperationError):
            await service.publish_normalized_post_to_community(
                event=event, starter=starter, community_actor_url=COMMUNITY,
                publish_call=gateway.publish_content)
        gateway.publish_content.assert_not_awaited()
    finally:
        db.close()



def comment_event(*, connector='discord-primary', author_id='123', kind='human',
                  thread_id='200', parent_id='100', reference='301'):
    """Model one independently verified forum reply into an existing mapped thread."""
    forum = ChannelRef(connector, parent_id, 'forum', '1')
    thread = ThreadRef(forum, thread_id)
    return MessageCreated(
        connector, MessageRef(connector, thread_id, '402'),
        AuthorRef(connector, author_id, kind, 'Alice'),
        ContentDocument('reply body'),
        reply_to=(MessageRef(connector, thread_id, reference) if reference else None),
        parent_channel=forum, thread_context=thread,
        external_guild_id='1', created_at=OBSERVED_AT,
    )


@pytest.mark.asyncio
async def test_verified_comment_persists_reply_to_approved_parent(tmp_path):
    """The canonical comment mapping uses the original reply ID and approved AP parent."""
    db, gateway, service = world(tmp_path)
    try:
        gateway.publish_content.return_value = PublishContentResult(
            activity_id=f'{ACTOR}/activities/create/comments/2',
            object_id=f'{ACTOR}/objects/comments/2', community_actor_url=COMMUNITY)
        # The approved parent is backed by a real source thread and reply receipt.
        root = db.discord_fanout_groups.create_thread_group(
            community_actor_id=COMMUNITY, source_channel_id=100,
            source_thread_id=200, source_starter_message_id=300,
            ap_activity_id=f'{ACTOR}/activities/create/posts/root',
            ap_object_id='https://bridge.example/objects/posts/root')
        db.discord_fanout_groups.add_thread_delivery(thread_group_id=root.id,
            discord_channel_id=100, discord_thread_id=200, discord_starter_message_id=300, role='source')
        parent = db.discord_fanout_groups.create_message_group(
            community_actor_id=COMMUNITY, thread_group_id=root.id,
            source_channel_id=100, source_thread_id=200, source_message_id=401,
            ap_activity_id=f'{ACTOR}/activities/create/comments/parent',
            ap_object_id='https://bridge.example/objects/comments/parent')
        db.discord_fanout_groups.add_message_delivery(message_group_id=parent.id,
            discord_channel_id=100, discord_thread_id=200, discord_message_id=401, role='source')
        result = await service.publish_normalized_comment_to_community(
            event=comment_event(), community_actor_url=COMMUNITY,
            parent_object_id='https://bridge.example/objects/comments/parent',
            publish_call=gateway.publish_content)
        assert result.status == 'published'
        mapping = db.message_mappings.get_by_source_ref(MessageRef('discord-primary', '200', str(402)))
        assert mapping is not None and mapping.discord_channel_id == 100
        stored = db.activitypub_objects.get_published_activity_object_by_object_id(result.object_id)
        assert stored.in_reply_to_object_id == 'https://bridge.example/objects/comments/parent'
        request = gateway.render_community.await_args.args[0]
        assert request.body_markdown.endswith('reply body')
        assert request.kind == 'comment'
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['bot', 'other_connector', 'cross_thread_reply', 'wrong_parent_kind', 'zero_message'])
async def test_invalid_normalized_comment_does_not_federate(tmp_path, change):
    """Invalid source, author or reply containment fails before any AP gateway call."""
    db, gateway, service = world(tmp_path)
    try:
        event = comment_event(kind='bot' if change == 'bot' else 'human',
            connector='fluxer-a' if change == 'other_connector' else 'discord-primary',
            reference=None if change == 'cross_thread_reply' else '301')
        from dataclasses import replace
        if change == 'cross_thread_reply':
            event = replace(event, reply_to=MessageRef('discord-primary', '201', '301'))
        if change == 'wrong_parent_kind':
            forum = ChannelRef('discord-primary', '100', 'text', '1')
            event = replace(event, parent_channel=forum, thread_context=ThreadRef(forum, '200'))
        if change == 'zero_message':
            event = replace(event, message=MessageRef('discord-primary', '200', '0'))
        with pytest.raises(PlatformOperationError):
            await service.publish_normalized_comment_to_community(
                event=event, community_actor_url=COMMUNITY,
                parent_object_id='https://bridge.example/objects/posts/1',
                publish_call=gateway.publish_content)
        gateway.publish_content.assert_not_awaited()
        assert db.message_mappings.get_by_source_ref(MessageRef('discord-primary', '200', str(402))) is None
    finally:
        db.close()


@pytest.mark.asyncio
async def test_unregistered_forum_reply_never_publishes_with_failed_notice(tmp_path):
    """A failed registration notice cannot turn an unauthorized reply into AP Create."""
    db, gateway, service = world(tmp_path)
    try:
        event = comment_event(author_id='999')
        notice = SimpleNamespace(send_message=AsyncMock(side_effect=RuntimeError('offline')))
        result = await service.publish_normalized_comment_to_community(
            event=event, community_actor_url=COMMUNITY,
            parent_object_id='https://bridge.example/objects/posts/1',
            publish_call=gateway.publish_content, notice_adapter=notice)
        assert result.reason == 'unregistered_user'
        gateway.publish_content.assert_not_awaited()
        args, kwargs = notice.send_message.await_args
        assert args[0] == event.thread_context
        assert kwargs['reply_to'] == event.message
        assert kwargs['attempt'].key.endswith(':200:402')
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('ban_error', [False, True])
async def test_blocked_forum_reply_gets_notice_but_no_canonical_comment(tmp_path, ban_error):
    """A ban or an unavailable moderation lookup cannot produce an AP comment."""
    db, gateway, service = world(tmp_path)
    try:
        event = comment_event()
        if ban_error:
            moderator = SimpleNamespace(check_discord_user=lambda **_: (_ for _ in ()).throw(RuntimeError('db')))
        else:
            moderator = SimpleNamespace(check_discord_user=lambda **_: SimpleNamespace(
                banned=True, reason_text='spam', expires_at=None, scope='global'))
        service.user_ban_service = moderator
        notice = SimpleNamespace(send_message=AsyncMock())
        result = await service.publish_normalized_comment_to_community(
            event=event, community_actor_url=COMMUNITY,
            parent_object_id='https://bridge.example/objects/posts/1',
            publish_call=gateway.publish_content, notice_adapter=notice)
        assert result.status == 'rejected'
        assert result.reason == ('ban_check_failed' if ban_error else 'user_banned')
        gateway.publish_content.assert_not_awaited()
        notice.send_message.assert_awaited_once()
        assert notice.send_message.await_args.kwargs['attempt'].key.endswith(':200:402')
    finally:
        db.close()
