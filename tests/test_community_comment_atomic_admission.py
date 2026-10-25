"""Authored forum replies join the first canonical ActivityPub writer commit."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import select

from src.content_publish_service import ContentPublishService
from src.db import Database
from src.fedify_gateway_client import RenderCommunityResult
from src.models import (
    CommunityMessageGroup, CommunityMessageGroupDelivery, CommunityThreadGroup,
    LocalCommunityMessage, LocalCommunityMessageSurface, LocalCommunityThread,
    MessageMapping, PublishedActivityObject, UserActorOutboundSource,
)
from src.platform_types import (
    AuthorRef, ChannelRef, ContentDocument, MessageCreated, MessageRef,
    MessageView, ThreadCreated, ThreadRef,
)


class AtomicReplyAdmissionTests(unittest.TestCase):
    """Compare old separate commits with one domain+receipt+outbox transaction."""

    def test_remote_and_local_comment_graph_is_atomic(self) -> None:
        """Both domains persist one complete comment graph and reject forged parent."""
        for domain in ('remote', 'local'):
            with self.subTest(domain=domain), tempfile.TemporaryDirectory() as d:
                db=Database(f"sqlite:///{Path(d)/'comments.db'}")
                self.addCleanup(db.close)
                db.migrate()
                actor='https://bridge.example/actors/users/alice'
                db.users.create_user(discord_user_id='123',activitypub_username='alice',
                    actor_url=actor,inbox_url=f'{actor}/inbox',outbox_url=f'{actor}/outbox',
                    followers_url=f'{actor}/followers',public_key_pem='public',private_key_pem='private')
                if domain=='remote':
                    community='https://remote.example/c/group'
                    db.remote_subscriptions.create_subscription(
                        discord_channel_id=500,discord_guild_id=1,
                        lemmy_community_actor_id=community,
                        lemmy_community_name='Group',lemmy_community_id=None,
                        community_inbox_url=f'{community}/inbox',
                        follow_activity_id='https://bridge.example/follows/one',status='accepted')
                else:
                    community='https://bridge.example/actors/communities/test'
                    db.local_communities.create_local_community(
                        discord_guild_id=1,discord_forum_channel_id=500,slug='test',
                        display_name='Test',summary=None,created_by_discord_user_id='123',
                        actor_url=community,inbox_url=f'{community}/inbox',
                        outbox_url=f'{community}/outbox',followers_url=f'{community}/followers',
                        public_key_pem='public',private_key_pem='private')
                async def render(req):
                    obj={'id':req.object_id,'type':'Page' if req.kind=='post' else 'Note',
                         'attributedTo':actor,'published':req.published_at,
                         'source':{'content':req.body_markdown}}
                    if req.title is not None: obj['name']=req.title
                    if req.in_reply_to_object_id: obj['inReplyTo']=req.in_reply_to_object_id
                    return RenderCommunityResult(
                        activity_id=req.activity_id,object_id=req.object_id,
                        community_actor_url=req.community_actor_url,published_at=req.published_at,
                        activity_json={'id':req.activity_id,'type':'Create','actor':actor,
                                       'to':[req.community_actor_url],'object':obj})
                gateway=SimpleNamespace(render_community=render,publish_content=AsyncMock(
                    side_effect=AssertionError('legacy send-before-commit')),
                    publish_local_community_content=AsyncMock(side_effect=AssertionError('legacy send')))
                service=ContentPublishService(database=db,fedify_gateway=gateway,bridge_prefix='[bridge]',
                    bridge_policy_service=SimpleNamespace())
                forum=ChannelRef('discord-primary','500','forum','1')
                thread=ThreadRef(forum,'501')
                starter=MessageRef('discord-primary','501','502')
                author=AuthorRef('discord-primary','123','human','Alice')
                created=datetime(2025,7,1,12,30,tzinfo=timezone.utc)
                async def scenario():
                    post=await service.publish_normalized_post_to_community(
                        event=ThreadCreated('discord-primary',thread,'Title',starter=starter,external_guild_id='1'),
                        starter=MessageView(starter,author,ContentDocument('Hello'),created_at=created),
                        community_actor_url=community,publish_call=gateway.publish_content)
                    reply=MessageRef('discord-primary','501','503')
                    result=await service.publish_normalized_comment_to_community(
                        event=MessageCreated('discord-primary',reply,author,ContentDocument('Reply'),
                            parent_channel=forum,thread_context=thread,external_guild_id='1',created_at=created),
                        community_actor_url=community,parent_object_id=post.object_id,
                        publish_call=gateway.publish_content)
                    assert result.status=='published'
                    return post,result
                post,reply=asyncio.run(scenario())
                with db.session() as session:
                    assert len(list(session.scalars(select(MessageMapping)))) == 2
                    assert len(list(session.scalars(select(PublishedActivityObject)))) == 2
                    assert len(list(session.scalars(select(UserActorOutboundSource)))) == 2
                    if domain=='remote':
                        rows=list(session.scalars(select(CommunityMessageGroup)))
                        assert len(rows)==1
                        assert rows[0].ap_object_id==reply.object_id
                        assert rows[0].thread_group_id==session.scalar(select(CommunityThreadGroup)).id
                        receipts=list(session.scalars(select(CommunityMessageGroupDelivery)))
                    else:
                        rows=list(session.scalars(select(LocalCommunityMessage)))
                        assert len(rows)==1
                        assert rows[0].ap_object_id==reply.object_id
                        assert rows[0].local_community_thread_id==session.scalar(select(LocalCommunityThread)).id
                        receipts=list(session.scalars(select(LocalCommunityMessageSurface)))
                    assert len(receipts)==1
                    assert receipts[0].role in ('source','host')
                    assert receipts[0].applied_revision==0
                # A forged cross-thread AP parent is checked before any of
                # the comment graph can commit or become signed delivery work.
                async def foreign_parent():
                    await service.publish_normalized_comment_to_community(
                        event=MessageCreated('discord-primary',
                            MessageRef('discord-primary','501','504'),author,
                            ContentDocument('Forged reply'),parent_channel=forum,
                            thread_context=thread,external_guild_id='1',
                            created_at=created),
                        community_actor_url=community,
                        parent_object_id='https://another.example/objects/foreign',
                        publish_call=gateway.publish_content)
                with self.assertRaisesRegex(ValueError,'parent'):
                    asyncio.run(foreign_parent())
                with db.session() as session:
                    assert len(list(session.scalars(select(MessageMapping)))) == 2
                    assert len(list(session.scalars(select(PublishedActivityObject)))) == 2
                    assert len(list(session.scalars(select(UserActorOutboundSource)))) == 2
                gateway.publish_content.assert_not_awaited()
                db.close()


if __name__=='__main__':
    unittest.main()
