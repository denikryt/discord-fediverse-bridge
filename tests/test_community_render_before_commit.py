"""Community publication must persist a canonical Create before any signed delivery."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from src.content_publish_service import ContentPublishService
from src.content_sync.outbound_publish import canonical_community_create_ids
from src.db import Database
from src.fedify_gateway_client import RenderCommunityResult
from src.models import MessageMapping, PublishedActivityObject, UserActorOutboundSource, UserActorOutboundDelivery
from src.platform_types import AuthorRef, ChannelRef, ContentDocument, MessageRef, MessageView, ThreadCreated, ThreadRef


@pytest.mark.asyncio
async def test_forum_create_is_render_only_until_atomic_sqlite_commit(tmp_path):
    """A human post succeeds despite a gateway that forbids direct publication."""
    db = Database(f"sqlite:///{tmp_path / 'render-first.db'}")
    db.migrate()
    db.users.create_user(
        discord_user_id='123', activitypub_username='alice',
        actor_url='https://bridge.example/actors/users/alice',
        inbox_url='https://bridge.example/actors/users/alice/inbox',
        outbox_url='https://bridge.example/actors/users/alice/outbox',
        followers_url='https://bridge.example/actors/users/alice/followers',
        public_key_pem='public', private_key_pem='private',
    )
    forum = ChannelRef('discord-primary', '500', 'forum', '1')
    thread = ThreadRef(forum, '501')
    physical = MessageRef('discord-primary', '501', '502')
    created = datetime(2025, 7, 1, 12, 30, tzinfo=timezone.utc)
    rendered_calls = 0

    async def render(request):
        nonlocal rendered_calls
        rendered_calls += 1
        with db.session() as session:
            if rendered_calls == 1:
                assert session.scalar(select(MessageMapping).where(MessageMapping.source_id == '502')) is None
        return RenderCommunityResult(
            activity_id=request.activity_id, object_id=request.object_id,
            community_actor_url=request.community_actor_url,
            published_at=request.published_at,
            activity_json={
                'id': request.activity_id, 'type': 'Create',
                'actor': 'https://bridge.example/actors/users/alice',
                'object': {'id': request.object_id, 'type': 'Page', 'published': request.published_at,
                           'attributedTo': 'https://bridge.example/actors/users/alice',
                           'source': {'content': request.body_markdown}, 'name': request.title},
                'to': [request.community_actor_url],
            },
        )

    gateway = SimpleNamespace(
        render_community=render,
        publish_content=AsyncMock(side_effect=AssertionError('signed delivery before commit')),
        publish_local_community_content=AsyncMock(side_effect=AssertionError('signed delivery before commit')),
    )
    service = ContentPublishService(
        database=db, fedify_gateway=gateway, bridge_prefix='[bridge]',
        bridge_policy_service=SimpleNamespace(),
    )
    starter = MessageView(
        physical, AuthorRef('discord-primary', '123', 'human', 'Alice'),
        ContentDocument('Hello'), created_at=created,
    )
    event = ThreadCreated('discord-primary', thread, 'Title', starter=physical, external_guild_id='1')
    try:
        for _ in range(2):
            result = await service.publish_normalized_post_to_community(
                event=event, starter=starter,
                community_actor_url='https://remote.example/c/group',
                publish_call=gateway.publish_content,
            )
            assert result.status == 'published'
        operation_id, activity_id, object_id = canonical_community_create_ids(
            source=physical, actor_username='alice',
            actor_url='https://bridge.example/actors/users/alice', kind='post',
        )
        assert len(operation_id) == 64
        assert result.activity_id == activity_id and result.object_id == object_id
        gateway.publish_content.assert_not_awaited()
        gateway.publish_local_community_content.assert_not_awaited()
        with db.session() as session:
            assert len(list(session.scalars(select(MessageMapping)))) == 1
            assert len(list(session.scalars(select(PublishedActivityObject)))) == 1
            assert len(list(session.scalars(select(UserActorOutboundSource)))) == 1
        assert rendered_calls >= 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_render_failure_cannot_commit_partial_canonical_graph(tmp_path):
    """A failed render leaves no durable AP object or outbound send to retry."""
    db = Database(f"sqlite:///{tmp_path / 'render-error.db'}")
    db.migrate()
    db.users.create_user(discord_user_id='123', activitypub_username='alice',
        actor_url='https://bridge.example/actors/users/alice',
        inbox_url='https://bridge.example/actors/users/alice/inbox',
        outbox_url='https://bridge.example/actors/users/alice/outbox',
        followers_url='https://bridge.example/actors/users/alice/followers',
        public_key_pem='public',private_key_pem='private')
    forum=ChannelRef('discord-primary','500','forum','1')
    thread=ThreadRef(forum,'501')
    msg=MessageRef('discord-primary','501','502')
    gateway=SimpleNamespace(render_community=AsyncMock(side_effect=RuntimeError('invalid render')),
                            publish_content=AsyncMock(side_effect=AssertionError('network')))
    service=ContentPublishService(database=db,fedify_gateway=gateway,bridge_prefix='[bridge]',bridge_policy_service=SimpleNamespace())
    with pytest.raises(RuntimeError,match='invalid render'):
        await service.publish_normalized_post_to_community(
            event=ThreadCreated('discord-primary',thread,'Title',starter=msg,external_guild_id='1'),
            starter=MessageView(msg,AuthorRef('discord-primary','123','human','Alice'),ContentDocument('Hello'),
                                created_at=datetime(2025,7,1,tzinfo=timezone.utc)),
            community_actor_url='https://remote.example/c/group',publish_call=gateway.publish_content)
    with db.session() as session:
        assert session.scalar(select(PublishedActivityObject)) is None
        assert session.scalar(select(UserActorOutboundSource)) is None
    gateway.publish_content.assert_not_awaited()
    db.close()

@pytest.mark.asyncio
async def test_direct_only_remote_community_claim_and_follow_revocation(tmp_path):
    """A verified accepted Group subscription can claim without User Actor followers."""
    db = Database(f"sqlite:///{tmp_path / 'direct.db'}")
    db.migrate()
    db.users.create_user(
        discord_user_id='123', activitypub_username='alice',
        actor_url='https://bridge.example/actors/users/alice',
        inbox_url='https://bridge.example/actors/users/alice/inbox',
        outbox_url='https://bridge.example/actors/users/alice/outbox',
        followers_url='https://bridge.example/actors/users/alice/followers',
        public_key_pem='public', private_key_pem='private',
    )
    sub = db.remote_subscriptions.create_subscription(
        discord_channel_id=500, discord_guild_id=1,
        lemmy_community_actor_id='https://remote.example/c/group',
        lemmy_community_name='Group', lemmy_community_id=None,
        community_inbox_url='https://remote.example/c/group/inbox',
        follow_activity_id='https://bridge.example/follows/F1', status='accepted',
    )
    forum=ChannelRef('discord-primary','500','forum','1')
    thread=ThreadRef(forum,'501')
    msg=MessageRef('discord-primary','501','502')
    actor='https://bridge.example/actors/users/alice'
    async def render(request):
        return RenderCommunityResult(
            activity_id=request.activity_id, object_id=request.object_id,
            community_actor_url=request.community_actor_url,
            published_at=request.published_at,
            activity_json={
                'id': request.activity_id, 'type': 'Create', 'actor': actor,
                'to': [request.community_actor_url],
                'object': {'id': request.object_id, 'attributedTo': actor,
                           'type': 'Page', 'published': request.published_at,
                           'name': request.title, 'source': {'content': request.body_markdown}},
            },
        )
    gateway=SimpleNamespace(render_community=render,
                            publish_content=AsyncMock(side_effect=AssertionError('legacy network send')))
    service=ContentPublishService(database=db,fedify_gateway=gateway,
                                  bridge_prefix='[bridge]',bridge_policy_service=SimpleNamespace())
    await service.publish_normalized_post_to_community(
        event=ThreadCreated('discord-primary',thread,'Title',starter=msg,external_guild_id='1'),
        starter=MessageView(msg,AuthorRef('discord-primary','123','human','Alice'),ContentDocument('Hello'),
                            created_at=datetime(2025,7,1,tzinfo=timezone.utc)),
        community_actor_url='https://remote.example/c/group',publish_call=gateway.publish_content)
    with db.session() as session:
        deliveries=list(session.scalars(select(UserActorOutboundDelivery)))
        assert len(deliveries)==1
        assert deliveries[0].target_endpoint_url == 'https://remote.example/c/group/inbox'
        assert 'remote_community_publication' in deliveries[0].direct_authorizations_json
        from src.models import CommunityThreadGroup, CommunityThreadGroupDelivery
        assert len(list(session.scalars(select(CommunityThreadGroup)))) == 1
        assert len(list(session.scalars(select(CommunityThreadGroupDelivery)))) == 1
        canonical=session.scalar(select(CommunityThreadGroup))
        assert canonical.current_content_json is not None
        assert canonical.source_external_thread_id == '501'
    claims=db.user_actor_deliveries.claim_due(limit=5,per_user_limit=5,stale_after_seconds=3600)
    assert len(claims)==1
    assert claims[0].represented_actor_ids==('https://remote.example/c/group',)
    db.user_actor_deliveries.release_unsubmitted_claim(claims[0].delivery_id,claims[0].claim_token,'temporary')
    with db.immediate_session() as session:
        from src.models import ChannelCommunitySubscription
        current=session.get(ChannelCommunitySubscription,sub.id)
        current.follow_activity_id='https://bridge.example/follows/F2'
    with db.immediate_session() as session:
        from src.db.repositories.user_actor_delivery_claims import UserActorDeliveryClaimRepository
        delivery=session.get(UserActorOutboundDelivery,claims[0].delivery_id)
        from src.models import UserActorOutboundSource
        source=session.get(UserActorOutboundSource,delivery.source_id)
        assert not UserActorDeliveryClaimRepository._eligible_direct_actor_ids(session,delivery,source)
    gateway.publish_content.assert_not_awaited()
    db.close()
