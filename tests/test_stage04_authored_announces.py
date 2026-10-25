"""SQLite scenarios for durable Group-signed authored Create and causal claims."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import select

from src.db import Database
from src.models import (
    LocalCommunity, LocalCommunityAuthoredAnnounceDelivery,
    PublishedActivityObject, RemoteSubscriber,
)
from src.local_communities.federation_fanout import LocalCommunityFederationFanout


class AuthoredAnnounceScenarios(unittest.TestCase):
    """An exact accepted Follow gets one stable Group activity, never a replay."""

    def setUp(self) -> None:
        """Create isolated current-v1 SQLite containing a bridge-owned community."""
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.db = Database(f"sqlite:///{Path(self.temporary.name) / 'bridge.sqlite'}")
        self.addCleanup(self.db.close)
        self.db.migrate()
        self.actor='https://bridge.test/actors/communities/test'
        with self.db.immediate_session() as session:
            community = LocalCommunity(
                slug='test',display_name='Test',actor_url=self.actor,
                inbox_url=f'{self.actor}/inbox',outbox_url=f'{self.actor}/outbox',
                followers_url=f'{self.actor}/followers',
                public_key_pem='public',private_key_pem='private',status='active',
            )
            session.add(community)
            session.flush()
            self.community_id=community.id
            published = PublishedActivityObject(
                actor_username='alice',actor_url='https://bridge.test/actors/users/alice',
                publication_context='community',community_actor_url=self.actor,
                activity_id='https://bridge.test/activities/create/1',
                object_id='https://bridge.test/objects/1',
                kind='post',title='Title',body_markdown='Hello',
                conversation_root_object_id='https://bridge.test/objects/1',
            )
            session.add(published)
            session.flush()
            self.object_id=published.id
            session.add(RemoteSubscriber(
                local_community_id=community.id,
                remote_actor_id='https://remote.test/actor/abc',
                remote_inbox_url='https://remote.test/actor/abc/inbox',
                follow_activity_id='https://remote.test/follows/F1',status='accepted',
            ))
        self.inner={'id':'https://bridge.test/activities/create/1','type':'Create','object':{'id':'https://bridge.test/objects/1'}}

    def test_atomic_admission_and_frozen_follow_identity(self) -> None:
        """A committed Create reserves one immutable Group Announce for F1."""
        with self.db.immediate_session() as session:
            created=self.db.local_community_authored_announces.stage_create(
                session, published=session.get(PublishedActivityObject,self.object_id),
                community=session.get(LocalCommunity,self.community_id),
                activity_json=self.inner,
            )
            self.assertEqual(created,1)
        with self.db.session() as session:
            row=session.scalar(select(LocalCommunityAuthoredAnnounceDelivery))
            self.assertEqual(row.status,'pending')
            self.assertEqual(row.target_follow_activity_id,'https://remote.test/follows/F1')
            self.assertEqual(json.loads(row.activity_json)['object'],self.inner)
            self.assertIn('/activities/announce/',row.announce_activity_id)
        claims=self.db.local_community_authored_announces.claim_due()
        self.assertEqual(len(claims),1)
        self.assertTrue(self.db.local_community_authored_announces.revalidate(claims[0]))
        self.assertTrue(self.db.local_community_authored_announces.record_result(claims[0],delivered=True))
        self.assertFalse(self.db.local_community_authored_announces.record_result(claims[0],delivered=True))
        self.assertEqual(self.db.local_community_authored_announces.claim_due(),[])

    def test_follow_replacement_never_retargets_claim(self) -> None:
        """F2 must not inherit the already reserved or dispatched F1 snapshot."""
        with self.db.immediate_session() as session:
            self.db.local_community_authored_announces.stage_create(
                session,published=session.get(PublishedActivityObject,self.object_id),
                community=session.get(LocalCommunity,self.community_id),
                activity_json=self.inner,
            )
        with self.db.immediate_session() as session:
            row=session.scalar(select(RemoteSubscriber))
            row.follow_activity_id='https://remote.test/follows/F2'
        self.assertEqual(self.db.local_community_authored_announces.claim_due(),[])
        with self.db.session() as session:
            row=session.scalar(select(LocalCommunityAuthoredAnnounceDelivery))
            self.assertEqual(row.status,'cancelled')
            self.assertEqual(row.target_follow_activity_id,'https://remote.test/follows/F1')

    def test_follow_refresh_atomically_cancels_old_untransmitted_announces(self) -> None:
        """F1 work cannot remain pending after its current subscriber becomes F2."""
        with self.db.immediate_session() as session:
            self.db.local_community_authored_announces.stage_create(
                session,published=session.get(PublishedActivityObject,self.object_id),
                community=session.get(LocalCommunity,self.community_id),
                activity_json=self.inner,
            )
            self.db.remote_subscribers.create_or_refresh_accepted(
                session,local_community_id=self.community_id,
                remote_actor_id='https://remote.test/actor/abc',
                remote_inbox_url='https://remote.test/actor/abc/inbox',
                follow_activity_id='https://remote.test/follows/F2',
            )
            old=session.scalar(select(LocalCommunityAuthoredAnnounceDelivery))
            self.assertEqual(old.status,'cancelled')
        self.assertEqual(self.db.local_community_authored_announces.claim_due(),[])

    def test_stale_undo_never_removes_new_follow(self) -> None:
        """A delayed Undo(F1) is not authority to delete accepted F2."""
        with self.db.immediate_session() as session:
            self.db.remote_subscribers.create_or_refresh_accepted(
                session,local_community_id=self.community_id,
                remote_actor_id='https://remote.test/actor/abc',
                remote_inbox_url='https://remote.test/actor/abc/inbox',
                follow_activity_id='https://remote.test/follows/F2',
            )
        with self.db.immediate_session() as session:
            result=self.db.remote_subscribers.delete_if_current_follow(
                session,local_community_id=self.community_id,
                remote_actor_id='https://remote.test/actor/abc',
                follow_activity_id='https://remote.test/follows/F1',
            )
            self.assertEqual(result,'stale')
        current=self.db.remote_subscribers.get_remote_subscriber(
            local_community_id=self.community_id,
            remote_actor_id='https://remote.test/actor/abc')
        self.assertIsNotNone(current)
        self.assertEqual(current.follow_activity_id,'https://remote.test/follows/F2')

    def test_unknown_create_blocks_unverified_mutations(self) -> None:
        """An ambiguous signed Create is a durable fence, not retry authorization."""
        with self.db.immediate_session() as session:
            self.db.local_community_authored_announces.stage_create(
                session,published=session.get(PublishedActivityObject,self.object_id),
                community=session.get(LocalCommunity,self.community_id),
                activity_json=self.inner,
            )
        attempt=self.db.local_community_authored_announces.claim_due()[0]
        self.assertTrue(self.db.local_community_authored_announces.record_result(
            attempt,delivered=False,error='unknown HTTP response'))
        with self.db.immediate_session() as session:
            row=session.scalar(select(LocalCommunityAuthoredAnnounceDelivery))
            session.add(LocalCommunityAuthoredAnnounceDelivery(
                published_object_id=row.published_object_id,
                local_community_id=row.local_community_id,
                target_remote_actor_id=row.target_remote_actor_id,
                target_follow_activity_id=row.target_follow_activity_id,
                target_remote_inbox_url=row.target_remote_inbox_url,
                delivery_profile=row.delivery_profile,
                group_actor_id=row.group_actor_id,
                activity_kind='update',content_revision=1,
                announce_activity_id=row.announce_activity_id+'/update/1',
                activity_json=row.activity_json,status='pending',
            ))
        self.assertEqual(self.db.local_community_authored_announces.claim_due(),[])


if __name__ == '__main__':
    unittest.main(verbosity=2)

class AuthoredCanonicalAdmissionScenarios(unittest.TestCase):
    """A real authored post reserves canonical and Group rows in one SQLite commit."""

    def test_rendered_create_and_group_target_are_committed_together(self) -> None:
        """A no-send renderer cannot accidentally create an untracked Group delivery."""
        from datetime import datetime, timezone
        from unittest.mock import AsyncMock
        from src.content_publish_service import ContentPublishService
        from src.fedify_gateway_client import RenderCommunityResult, SendLocalCommunityRelayResult, SendLocalCommunityRelayOutcome
        from src.platform_types import AuthorRef, ChannelRef, ContentDocument, MessageRef, MessageView, ThreadRef, ThreadCreated
        from src.models import MessageMapping, UserActorOutboundSource

        with tempfile.TemporaryDirectory() as d:
            db=Database(f"sqlite:///{Path(d)/'canon.db'}")
            db.migrate()
            db.users.create_user(discord_user_id='123',activitypub_username='alice',
                actor_url='https://bridge.test/actors/users/alice',
                inbox_url='https://bridge.test/actors/users/alice/inbox',
                outbox_url='https://bridge.test/actors/users/alice/outbox',
                followers_url='https://bridge.test/actors/users/alice/followers',
                public_key_pem='public',private_key_pem='private')
            community=db.local_communities.create_local_community(
                discord_guild_id=1,discord_forum_channel_id=500,
                slug='test',display_name='Test',summary=None,created_by_discord_user_id='123',
                actor_url='https://bridge.test/actors/communities/test',
                inbox_url='https://bridge.test/actors/communities/test/inbox',
                outbox_url='https://bridge.test/actors/communities/test/outbox',
                followers_url='https://bridge.test/actors/communities/test/followers',
                public_key_pem='public',private_key_pem='private',
            )
            with db.immediate_session() as session:
                session.add(RemoteSubscriber(
                    local_community_id=community.id,remote_actor_id='https://remote.test/actor/abc',
                    remote_inbox_url='https://remote.test/actor/abc/inbox',
                    follow_activity_id='https://remote.test/follows/F1',status='accepted'))
            actor='https://bridge.test/actors/users/alice'
            async def render(request):
                return RenderCommunityResult(
                    activity_id=request.activity_id,object_id=request.object_id,
                    community_actor_url=request.community_actor_url,published_at=request.published_at,
                    activity_json={'id':request.activity_id,'type':'Create','actor':actor,
                                   'to':[request.community_actor_url],
                                   'object':{'id':request.object_id,'type':'Page',
                                             'attributedTo':actor,'published':request.published_at,
                                             'name':request.title,
                                             'source':{'content':request.body_markdown}}})
            class GroupGateway:
                async def send_local_community_relay(self,*,signing_actor_url,deliveries):
                    assert signing_actor_url==community.actor_url
                    assert len(deliveries)==1
                    assert deliveries[0].activity_json['object']['type']=='Create'
                    return SendLocalCommunityRelayResult(outcomes=[SendLocalCommunityRelayOutcome(
                        delivery_id=deliveries[0].delivery_id,ok=True,
                        target_remote_actor_id=deliveries[0].target_remote_actor_id,
                        activity_id=deliveries[0].activity_json['id'],error=None)])
            gateway=SimpleNamespace(render_community=render,
                                    publish_content=AsyncMock(side_effect=AssertionError('old gateway POST')),
                                    send_local_community_relay=GroupGateway().send_local_community_relay)
            service=ContentPublishService(database=db,fedify_gateway=gateway,
                bridge_prefix='[bridge]',bridge_policy_service=SimpleNamespace())
            forum=ChannelRef('discord-primary','500','forum','1')
            thread=ThreadRef(forum,'501')
            starter=MessageRef('discord-primary','501','502')
            published=asyncio.run(service.publish_normalized_post_to_community(
                event=ThreadCreated('discord-primary',thread,'Title',starter=starter,external_guild_id='1'),
                starter=MessageView(starter,AuthorRef('discord-primary','123','human','Alice'),
                    ContentDocument('Hello'),created_at=datetime(2025,7,1,tzinfo=timezone.utc)),
                community_actor_url=community.actor_url,publish_call=gateway.publish_content))
            assert published.status=='published'
            with db.session() as session:
                assert len(list(session.scalars(select(MessageMapping))))==1
                assert len(list(session.scalars(select(PublishedActivityObject))))==1
                assert len(list(session.scalars(select(UserActorOutboundSource))))==1
                assert len(list(session.scalars(select(LocalCommunityAuthoredAnnounceDelivery))))==1
                from src.models import LocalCommunityThread, LocalCommunityThreadSurface
                assert len(list(session.scalars(select(LocalCommunityThread)))) == 1
                assert len(list(session.scalars(select(LocalCommunityThreadSurface)))) == 1
                canonical=session.scalar(select(LocalCommunityThread))
                assert canonical.current_content_json is not None
                assert canonical.source_external_thread_id == '501'
            class Allow:
                def federation_decision(self, _url):return SimpleNamespace(allowed=True)
            fanout=LocalCommunityFederationFanout(database=db,fedify_gateway=gateway,policy_service=Allow())
            summary=asyncio.run(fanout.drain_authored())
            assert (summary.attempted,summary.delivered,summary.failed)==(1,1,0)
            with db.session() as session:
                row=session.scalar(select(LocalCommunityAuthoredAnnounceDelivery))
                assert row.status=='delivered'
            gateway.publish_content.assert_not_awaited()
            db.close()
