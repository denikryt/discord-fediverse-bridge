"""Real SQLite scenarios for per-target durable Stage 04 physical Create."""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import select

from src.db import Database
from src.models import ChannelSurface, LocalCommunity, LocalCommunityThread, PlatformConnector, PlatformDeliveryIntent
from src.platform_types import ChannelRef, ContentDocument, CreateAttempt, CreatedThread, CreatedMessage, DeliveryPresentation, MessageRef, ThreadRef


class PhysicalCreateIntentScenarios(unittest.TestCase):
    """A committed target survives concurrent dispatch and lost responses."""

    def setUp(self):
        """Create an owned forum and canonical local post in current SQLite v1."""
        t=tempfile.TemporaryDirectory(); self.addCleanup(t.cleanup)
        self.db=Database(f'sqlite:///{Path(t.name)/"bridge.db"}'); self.addCleanup(self.db.close)
        self.db.migrate()
        with self.db.immediate_session() as s:
            c=LocalCommunity(slug='demo',display_name='Demo',actor_url='https://bridge.test/communities/demo',inbox_url='https://bridge.test/communities/demo/inbox',outbox_url='https://bridge.test/communities/demo/outbox',followers_url='https://bridge.test/communities/demo/followers',public_key_pem='public',private_key_pem='private',status='active')
            s.add(c);s.flush();self.owner=c.id
            connector=s.scalar(select(PlatformConnector).where(PlatformConnector.connector_key=='discord-primary'))
            surface=ChannelSurface(connector_id=connector.id,external_channel_id='321',external_guild_id='1',kind='forum',local_community_id=c.id)
            s.add(surface);s.flush();self.surface=surface.id
            post=LocalCommunityThread(local_community_id=c.id,ap_activity_id='https://bridge.test/a/create/1',ap_object_id='https://bridge.test/objects/1',direction='outbound',origin_kind='discord')
            s.add(post);s.flush();self.canonical=post.id
        self.forum=ChannelRef('discord-primary','321','forum','1')
        self.attempt=CreateAttempt(f'fanout:v1:local:post:{self.canonical}:discord-primary:{self.surface}','discord-primary','forum_thread',self.forum)

    def _repo(self):
        """Return the current Database's single-writer intent repository."""
        return self.db.platform_delivery_intents

    def _prepare(self):
        """Reserve and freeze a complete request before any HTTP operation."""
        row=self._repo().reserve(domain='local',kind='post',canonical_id=self.canonical,forum=self.forum,channel_surface_id=self.surface,owner_kind='local_host',owner_id=self.owner,attempt=self.attempt,content=ContentDocument('body'),presentation=DeliveryPresentation(),title='title')
        self._repo().materialize(row.id,attempt=self.attempt,content=ContentDocument('body'),presentation=DeliveryPresentation(),title='title')
        return row.id

    def test_disabled_community_blocks_persisted_pending_dispatch(self):
        """A disabling action after reservation must forbid a new physical POST."""
        intent_id = self._prepare()
        with self.db.immediate_session() as session:
            session.get(LocalCommunity, self.owner).status = 'disabled'
        self.assertIsNone(self._repo().claim(intent_id))
        with self.db.session() as session:
            self.assertEqual(session.get(PlatformDeliveryIntent, intent_id).state, 'cancelled')

    def test_pretransport_claim_rejects_revoked_canonical_source(self):
        """A claimed target cannot cross the adapter boundary after Delete."""
        from datetime import datetime, timezone
        ident=self._prepare()
        claimed=self._repo().claim(ident)
        self.assertTrue(self._repo().dispatch_still_authorized(claimed))
        with self.db.immediate_session() as session:
            session.get(LocalCommunityThread,self.canonical).deleted_at=datetime.now(timezone.utc)
        self.assertFalse(self._repo().dispatch_still_authorized(claimed))
        # A committed claim cannot be retried when a change occurs after
        # reservation, regardless of whether the adapter was called.
        self.assertTrue(self._repo().mark_unresolved(ident,token=claimed.token,reason='pre_send_revoked'))
        self.assertIsNone(self._repo().claim(ident))

    def test_unknown_is_sticky_after_restart_and_duplicate_claim_is_refused(self):
        """A lost response is not an authorization to send another POST."""
        ident=self._prepare()
        claimed=self._repo().claim(ident)
        self.assertIsNotNone(claimed)
        self.assertIsNone(self._repo().claim(ident))
        self.assertTrue(self._repo().mark_unresolved(ident,token=claimed.token,reason='unknown_delivery'))
        self.assertIsNone(self._repo().claim(ident))
        with self.db.session() as s:
            self.assertEqual(s.get(PlatformDeliveryIntent,ident).state,'unresolved')

    def test_frozen_request_conflict_is_rejected(self):
        """A duplicate reservation cannot replace frozen body or target identity."""
        ident=self._prepare()
        with self.assertRaises(ValueError):
            self._repo().materialize(ident,attempt=self.attempt,content=ContentDocument('different'),presentation=DeliveryPresentation(),title='title')

    def test_verified_thread_receipt_is_atomic_with_local_surface(self):
        """A committed receipt includes independent thread/starter IDs on one copy."""
        ident=self._prepare()
        claim=self._repo().claim(ident)
        receipt=CreatedThread(ThreadRef(self.forum,'987'),MessageRef('discord-primary','987','654'),'bot')
        self.assertTrue(self._repo().commit_receipt(ident,token=claim.token,receipt=receipt))
        with self.db.session() as s:
            row=s.get(PlatformDeliveryIntent,ident)
            self.assertEqual(row.state,'delivered')
            from src.models import LocalCommunityThreadSurface
            copy=s.scalar(select(LocalCommunityThreadSurface).where(LocalCommunityThreadSurface.local_community_thread_id==self.canonical))
            self.assertEqual((copy.external_thread_id,copy.external_starter_message_id),('987','654'))
            self.assertEqual(copy.applied_revision,0)
        self.assertIsNone(self._repo().claim(ident))

    def test_rebound_target_retains_orphan_receipt_instead_of_attaching_to_new_owner(self):
        """A successful POST during ownership change is never foreign-owned."""
        ident=self._prepare(); claim=self._repo().claim(ident)
        with self.db.immediate_session() as s:
            surface=s.get(ChannelSurface,self.surface);surface.local_community_id=None
        receipt=CreatedThread(ThreadRef(self.forum,'987'),MessageRef('discord-primary','987','654'),'bot')
        self.assertTrue(self._repo().commit_receipt(ident,token=claim.token,receipt=receipt))
        with self.db.session() as s:
            row=s.get(PlatformDeliveryIntent,ident)
            self.assertEqual(row.state,'cancelled_after_send')
            self.assertIn('654',row.orphan_receipt_json)
            from src.models import LocalCommunityThreadSurface
            self.assertEqual(s.scalars(select(LocalCommunityThreadSurface)).all(),[])

    def test_blocked_nested_parent_requires_real_copy_in_same_target_thread(self):
        """A claimed child cannot forge readiness or cross-container parent proof."""
        from src.models import LocalCommunityMessage,LocalCommunityMessageSurface,LocalCommunityThreadSurface
        ident=self._prepare();post_claim=self._repo().claim(ident)
        self._repo().commit_receipt(ident,token=post_claim.token,
            receipt=CreatedThread(ThreadRef(self.forum,'987'),
            MessageRef('discord-primary','987','654'),'bot'))
        with self.db.immediate_session() as s:
            parent=LocalCommunityMessage(local_community_thread_id=self.canonical,
                ap_activity_id='https://bridge.test/a/parent',ap_object_id='https://bridge.test/o/parent',direction='inbound')
            s.add(parent);s.flush()
            child=LocalCommunityMessage(local_community_thread_id=self.canonical,
                ap_activity_id='https://bridge.test/a/child',ap_object_id='https://bridge.test/o/child',
                parent_ap_object_id=parent.ap_object_id,direction='inbound')
            s.add(child);s.flush();child_id=child.id;parent_id=parent.id
        dest=ThreadRef(self.forum,'987')
        attempt=CreateAttempt(f'fanout:v1:local:comment:{child_id}:discord-primary:{self.surface}',
                              'discord-primary','message',dest)
        blocked=self._repo().reserve(domain='local',kind='comment',canonical_id=child_id,
            forum=self.forum,channel_surface_id=self.surface,owner_kind='local_host',
            owner_id=self.owner,blocked_parent_canonical_id=parent_id)
        self.assertEqual(blocked.state,'blocked')
        self.assertIsNone(self._repo().claim(blocked.id))
        with self.assertRaises(ValueError):
            self._repo().materialize(blocked.id,attempt=attempt,
                content=ContentDocument('body'),presentation=DeliveryPresentation(),
                title=None,reply_to=MessageRef('discord-primary','987','999'))
        # A real parent physically delivered in the exact same thread is
        # necessary before the immutable child request can be materialized.
        with self.db.immediate_session() as s:
            parent_thread=s.scalar(select(LocalCommunityThreadSurface).where(
                LocalCommunityThreadSurface.local_community_thread_id==self.canonical))
            s.add(LocalCommunityMessageSurface(local_community_message_id=parent_id,
                local_community_thread_surface_id=parent_thread.id,
                connector_id=parent_thread.connector_id,channel_surface_id=self.surface,
                external_message_id='900',role='host'))
        with self.assertRaises(ValueError):
            self._repo().materialize(blocked.id,attempt=attempt,
                content=ContentDocument('body'),presentation=DeliveryPresentation(),
                title=None,reply_to=MessageRef('discord-primary','987','999'))
        self._repo().materialize(blocked.id,attempt=attempt,
            content=ContentDocument('body'),presentation=DeliveryPresentation(),
            title=None,reply_to=MessageRef('discord-primary','987','900'))
        claim=self._repo().claim(blocked.id)
        self.assertIsNotNone(claim)
        with self.db.session() as s:
            self.assertEqual(s.get(PlatformDeliveryIntent,blocked.id).state,'dispatching')

    def test_blocked_source_delete_is_terminal_after_parent_wake(self):
        """A tombstoned nested child cannot be rematerialized or POSTed."""
        from src.models import LocalCommunityMessage
        with self.db.immediate_session() as s:
            parent=LocalCommunityMessage(local_community_thread_id=self.canonical,
                ap_activity_id='https://bridge.test/a/parent',ap_object_id='https://bridge.test/o/parent',direction='inbound')
            s.add(parent);s.flush()
            child=LocalCommunityMessage(local_community_thread_id=self.canonical,
                ap_activity_id='https://bridge.test/a/child',ap_object_id='https://bridge.test/o/child',
                parent_ap_object_id=parent.ap_object_id,direction='inbound')
            s.add(child);s.flush();child_id=child.id;parent_id=parent.id
        blocked=self._repo().reserve(domain='local',kind='comment',canonical_id=child_id,
            forum=self.forum,channel_surface_id=self.surface,owner_kind='local_host',
            owner_id=self.owner,blocked_parent_canonical_id=parent_id)
        with self.db.immediate_session() as s:
            from datetime import datetime, timezone
            s.get(LocalCommunityMessage,child_id).deleted_at=datetime.now(timezone.utc)
        attempt=CreateAttempt(f'fanout:v1:local:comment:{child_id}:discord-primary:{self.surface}',
                              'discord-primary','message',ThreadRef(self.forum,'987'))
        self._repo().materialize(blocked.id,attempt=attempt,content=ContentDocument('body'),
            presentation=DeliveryPresentation(),title=None,
            reply_to=MessageRef('discord-primary','987','900'))
        self.assertIsNone(self._repo().claim(blocked.id))
        with self.db.session() as s:
            self.assertEqual(s.get(PlatformDeliveryIntent,blocked.id).state,'cancelled')

    def test_remote_group_receipt_is_one_copy_at_exact_forum(self):
        """Remote Community fanout cannot map a receipt to a foreign forum."""
        from src.models import ChannelCommunitySubscription, CommunityThreadGroup, CommunityThreadGroupDelivery
        with self.db.immediate_session() as s:
            connector=s.scalar(select(PlatformConnector).where(PlatformConnector.connector_key=='discord-primary'))
            subscription=ChannelCommunitySubscription(discord_channel_id=322,discord_guild_id=1,
                lemmy_community_actor_id='https://remote.test/c/abc',status='accepted')
            s.add(subscription);s.flush()
            surface=ChannelSurface(connector_id=connector.id,external_channel_id='322',
                external_guild_id='1',kind='forum',remote_subscription_id=subscription.id)
            s.add(surface)
            group=CommunityThreadGroup(community_actor_id='https://remote.test/c/abc',
                ap_activity_id='https://remote.test/a/1',ap_object_id='https://remote.test/objects/1')
            s.add(group);s.flush();gid=group.id;sid=surface.id;sub_id=subscription.id
        forum=ChannelRef('discord-primary','322','forum','1')
        key=f'fanout:v1:remote:post:{gid}:discord-primary:{sid}'
        attempt=CreateAttempt(key,'discord-primary','forum_thread',forum)
        row=self._repo().reserve(domain='remote',kind='post',canonical_id=gid,forum=forum,
            channel_surface_id=sid,owner_kind='remote_subscription',owner_id=sub_id,
            attempt=attempt,content=ContentDocument('remote body'),presentation=DeliveryPresentation(),title='a post')
        claim=self._repo().claim(row.id)
        with self.assertRaises(Exception):
            self._repo().commit_receipt(row.id,token=claim.token,receipt=CreatedThread(
                ThreadRef(ChannelRef('discord-primary','999','forum','1'),'111'),
                MessageRef('discord-primary','111','112'),'bot'))
        self.assertTrue(self._repo().commit_receipt(row.id,token=claim.token,receipt=CreatedThread(
            ThreadRef(forum,'333'),MessageRef('discord-primary','333','334'),'bot')))
        with self.db.session() as s:
            copy=s.scalar(select(CommunityThreadGroupDelivery).where(
                CommunityThreadGroupDelivery.thread_group_id==gid))
            self.assertEqual((copy.external_thread_id,copy.external_starter_message_id),('333','334'))

    def test_local_comment_receipt_requires_matching_parent_thread_surface(self):
        """A comment can only be persisted in its canonical target thread."""
        from src.models import LocalCommunityMessage, LocalCommunityMessageSurface
        self._repo().commit_receipt(self._prepare(),token=(claim:=self._repo().claim(self._prepare())).token,
            receipt=CreatedThread(ThreadRef(self.forum,'987'),MessageRef('discord-primary','987','654'),'bot'))
        with self.db.immediate_session() as s:
            comment=LocalCommunityMessage(local_community_thread_id=self.canonical,
                ap_activity_id='https://bridge.test/a/comment/1',ap_object_id='https://bridge.test/o/comment/1',
                direction='inbound')
            s.add(comment);s.flush();cid=comment.id
        attempt=CreateAttempt(f'fanout:v1:local:comment:{cid}:discord-primary:{self.surface}',
                              'discord-primary','message',ThreadRef(self.forum,'987'))
        row=self._repo().reserve(domain='local',kind='comment',canonical_id=cid,
            forum=self.forum,channel_surface_id=self.surface,owner_kind='local_host',owner_id=self.owner,
            attempt=attempt,content=ContentDocument('reply'),presentation=DeliveryPresentation(),
            reply_to=MessageRef('discord-primary','987','654'))
        claim=self._repo().claim(row.id)
        self.assertTrue(self._repo().commit_receipt(row.id,token=claim.token,
            receipt=CreatedMessage(MessageRef('discord-primary','987','765'),'bot')))
        with self.db.session() as s:
            copy=s.scalar(select(LocalCommunityMessageSurface).where(
                LocalCommunityMessageSurface.local_community_message_id==cid))
            self.assertEqual((copy.external_message_id,copy.external_parent_message_id),('765','654'))

    def test_restart_worker_sends_pending_once_then_skips_confirmed_receipt(self):
        """A pending Create survives a lost worker wake without duplicating POST."""
        from src.content_sync.platform_fanout import PlatformFanoutCoordinator
        ident = self._prepare()
        calls = []
        class Adapter:
            """Provide one verified forum result at the network boundary."""
            async def create_forum_thread(self, forum, content, *, title, presentation, attempt):
                """Record each actual physical forum Create."""
                calls.append(attempt.key)
                return CreatedThread(ThreadRef(forum, '987'),
                    MessageRef('discord-primary','987','654'), 'bot')
        class Registry:
            """Restrict the fake adapter to its requested connector."""
            def get(self, key):
                """Refuse unknown connector names rather than defaulting to Discord."""
                if key != 'discord-primary': raise LookupError(key)
                return Adapter()
        coordinator=PlatformFanoutCoordinator(self.db,Registry())
        self.assertEqual(asyncio.run(coordinator.recover_pending()), [ident])
        self.assertEqual(asyncio.run(coordinator.recover_pending()), [])
        self.assertEqual(len(calls),1)

    def test_restart_worker_never_reposts_unknown_attempt(self):
        """A previously transmitted Create with lost response is not a pending job."""
        from src.content_sync.platform_fanout import PlatformFanoutCoordinator
        ident=self._prepare();claim=self._repo().claim(ident)
        self._repo().mark_unresolved(ident,token=claim.token,reason='unknown_delivery')
        class Registry:
            """A forbidden adapter lookup proves the scanner skips uncertainty."""
            def get(self,key):
                """Reject if recovery incorrectly tries to send an unknown Create."""
                raise AssertionError('unknown delivery cannot be redispatched')
        coordinator=PlatformFanoutCoordinator(self.db,Registry())
        self.assertEqual(asyncio.run(coordinator.recover_pending()),[])
        with self.db.session() as session:
            self.assertEqual(session.get(PlatformDeliveryIntent,ident).state,'unresolved')

    def test_restart_scan_excludes_unknown_deleted_or_blocked_claims(self):
        """A restart only wakes materialized, definitely unsent pending sends."""
        ident=self._prepare()
        self.assertEqual(self._repo().recoverable_pending_ids(), [ident])
        claim=self._repo().claim(ident)
        self.assertEqual(self._repo().recoverable_pending_ids(), [])
        self._repo().mark_unresolved(ident,token=claim.token,reason='unknown_delivery')
        self.assertEqual(self._repo().recoverable_pending_ids(), [])

    def test_restart_scan_does_not_claim_deleted_pending_source(self):
        """A source deleted while pending must not be redispatched on recovery."""
        from datetime import datetime,timezone
        ident=self._prepare()
        with self.db.immediate_session() as session:
            session.get(LocalCommunityThread,self.canonical).deleted_at=datetime.now(timezone.utc)
        self.assertEqual(self._repo().recoverable_pending_ids(), [])
        self.assertIsNone(self._repo().claim(ident))

    def test_dispatch_expiration_marks_unresolved_without_reclaim(self):
        """A crashed worker may have transmitted; lease expiration is not proof."""
        from datetime import datetime,timezone,timedelta
        ident=self._prepare(); claim=self._repo().claim(ident)
        with self.db.immediate_session() as s:
            s.get(PlatformDeliveryIntent,ident).lease_until=datetime.now(timezone.utc)-timedelta(hours=2)
        self.assertEqual(self._repo().expire_claims(),1)
        self.assertIsNone(self._repo().claim(ident))
        with self.db.session() as s:
            self.assertEqual(s.get(PlatformDeliveryIntent,ident).state,'unresolved')

    def test_old_attempt_token_does_not_commit_foreign_receipt(self):
        """An unrelated attempt cannot claim another source's immutable copy."""
        ident=self._prepare(); claim=self._repo().claim(ident)
        receipt=CreatedThread(ThreadRef(self.forum,'987'),MessageRef('discord-primary','987','654'),'bot')
        self.assertFalse(self._repo().commit_receipt(ident,token='stolen',receipt=receipt))
        with self.db.session() as s:
            self.assertEqual(s.get(PlatformDeliveryIntent,ident).state,'dispatching')
        self.assertTrue(self._repo().commit_receipt(ident,token=claim.token,receipt=receipt))




class CoordinatorConcurrencyScenarios(PhysicalCreateIntentScenarios):
    """Concurrent coordinator worker retries must not duplicate a Discord POST."""

    def test_two_workers_one_create_and_one_mapping(self):
        """One SQLite claim provides at most one committed send receipt."""
        from src.content_sync.platform_fanout import PlatformFanoutCoordinator
        ident=self._prepare()
        calls=[]
        class FakeAdapter:
            """Return a distinct thread and starter from the physical boundary."""
            async def create_forum_thread(self, forum,content,*,title,presentation,attempt):
                """Log one transport request and produce a typed receipt."""
                calls.append(attempt.key)
                await asyncio.sleep(0.01)
                return CreatedThread(ThreadRef(forum,'1001'),MessageRef('discord-primary','1001','1002'),'bot')
        class Registry:
            """Expose a single configured connector for these local tests."""
            def get(self,key):
                """Reject any connector except the registered primary key."""
                if key!='discord-primary':raise LookupError(key)
                return FakeAdapter()
        coordinator=PlatformFanoutCoordinator(self.db,Registry())
        results=asyncio.run(self._deliver_pair(coordinator,ident))
        self.assertEqual(sorted(results),[False,True])
        self.assertEqual(len(calls),1)

    @staticmethod
    async def _deliver_pair(coordinator,ident):
        """Start both attempts concurrently without relying on event loop internals."""
        return await asyncio.gather(coordinator.deliver(ident),coordinator.deliver(ident))

    def test_unknown_write_stays_unresolved_on_replay(self):
        """An adapter error after dispatch cannot authorize a second POST."""
        from src.content_sync.platform_fanout import PlatformFanoutCoordinator
        ident=self._prepare();calls=[]
        class FakeAdapter:
            """Simulate a connection loss after Discord may have accepted POST."""
            async def create_forum_thread(self, forum,content,*,title,presentation,attempt):
                """Fail ambiguously even though a physical send was attempted."""
                calls.append(attempt.key)
                raise ConnectionError('lost response')
        class Registry:
            """Resolve primary transport for test intents."""
            def get(self,key):
                """Return a deterministic fake physical boundary."""
                return FakeAdapter()
        coordinator=PlatformFanoutCoordinator(self.db,Registry())
        self.assertFalse(asyncio.run(coordinator.deliver(ident)))
        self.assertFalse(asyncio.run(coordinator.deliver(ident)))
        self.assertEqual(len(calls),1)
        with self.db.session() as s:
            self.assertEqual(s.get(PlatformDeliveryIntent,ident).state,'unresolved')

    def test_crash_windows_preserve_post_without_automatic_duplicate(self):
        """A new Database connection honors pending, unknown and delivered states."""
        from datetime import datetime,timezone,timedelta
        from src.models import LocalCommunityThreadSurface
        path=str(self.db.engine.url)
        # Before reservation commit, a rejected target has no durable work.
        with self.assertRaises(ValueError):
            self._repo().reserve(domain='local',kind='post',canonical_id=self.canonical,
                forum=ChannelRef('discord-primary','other','forum','1'),
                channel_surface_id=self.surface,owner_kind='local_host',owner_id=self.owner,
                attempt=self.attempt,content=ContentDocument('body'),
                presentation=DeliveryPresentation(),title='title')
        with self.db.session() as s:
            self.assertEqual(s.scalars(select(PlatformDeliveryIntent)).all(),[])
        # Commit before claim: restart may claim the one frozen request.
        ident=self._prepare()
        resumed=Database(path);self.addCleanup(resumed.close);
        claimed=resumed.platform_delivery_intents.claim(ident)
        self.assertIsNotNone(claimed)
        self.assertIsNone(self._repo().claim(ident))
        # Crash after claim and even after remote acceptance but before reply:
        # a new process cannot tell those windows apart, so both are unknown.
        with self.db.immediate_session() as s:
            s.get(PlatformDeliveryIntent,ident).lease_until=datetime.now(timezone.utc)-timedelta(hours=3)
        self.assertEqual(resumed.platform_delivery_intents.expire_claims(),1)
        self.assertIsNone(resumed.platform_delivery_intents.claim(ident))
        with self.db.session() as s:
            row=s.get(PlatformDeliveryIntent,ident)
            self.assertEqual(row.state,'unresolved')
            self.assertIsNone(s.scalar(select(LocalCommunityThreadSurface)))

    def test_receipt_persisted_before_callback_prevents_repost_on_restart(self):
        """A complete physical receipt survives loss of the HTTP callback."""
        from src.models import LocalCommunityThreadSurface
        ident=self._prepare()
        claim=self._repo().claim(ident)
        receipt=CreatedThread(ThreadRef(self.forum,'987'),
            MessageRef('discord-primary','987','654'),'bot')
        self.assertTrue(self._repo().commit_receipt(ident,token=claim.token,receipt=receipt))
        resumed=Database(str(self.db.engine.url));self.addCleanup(resumed.close);
        self.assertIsNone(resumed.platform_delivery_intents.claim(ident))
        with resumed.session() as s:
            self.assertEqual(s.get(PlatformDeliveryIntent,ident).state,'delivered')
            self.assertEqual(s.scalar(select(LocalCommunityThreadSurface)).external_thread_id,'987')


if __name__=="__main__":
    unittest.main(verbosity=2)

    def test_authorized_delete_cancels_unsent_create_in_same_writer_commit(self):
        """A tombstone and pending intent cancellation survive the same restart."""
        import json
        from src.models import LocalCommunityThread
        with self.db.immediate_session() as session:
            row = session.get(LocalCommunityThread, self.canonical)
            row.current_content_json = json.dumps({
                'schemaVersion': 1, 'kind': 'post', 'apObjectId': row.ap_object_id,
                'authorActorId': 'https://remote.test/u/actor', 'bodyMarkdown': 'content',
            })
        intent_id = self._prepare()
        assert self.db.local_community_content.mark_inbound_deleted_if_live(
            kind='post', ap_object_id='https://bridge.test/objects/1',
            local_community_id=self.owner, actor_id='https://remote.test/u/actor')
        with self.db.session() as session:
            intent = session.get(PlatformDeliveryIntent, intent_id)
            canonical = session.get(LocalCommunityThread, self.canonical)
            self.assertIsNotNone(canonical.deleted_at)
            self.assertEqual(canonical.content_revision, 1)
            self.assertEqual(intent.state, 'cancelled')
            self.assertTrue(intent.desired_deleted)
            self.assertEqual(intent.desired_revision, 1)
        self.assertIsNone(self._repo().claim(intent_id))

    def test_authorized_delete_keeps_possibly_sent_claim_unresolved(self):
        """A claimed POST is not incorrectly pronounced cancelled by Delete."""
        import json
        from src.models import LocalCommunityThread
        with self.db.immediate_session() as session:
            row = session.get(LocalCommunityThread, self.canonical)
            row.current_content_json = json.dumps({
                'schemaVersion': 1, 'kind': 'post', 'apObjectId': row.ap_object_id,
                'authorActorId': 'https://remote.test/u/actor', 'bodyMarkdown': 'content',
            })
        intent_id = self._prepare()
        claimed = self._repo().claim(intent_id)
        assert claimed is not None
        assert self.db.local_community_content.mark_inbound_deleted_if_live(
            kind='post', ap_object_id='https://bridge.test/objects/1',
            local_community_id=self.owner, actor_id='https://remote.test/u/actor')
        with self.db.session() as session:
            row = session.get(PlatformDeliveryIntent, intent_id)
            self.assertEqual(row.state, 'unresolved')
            self.assertTrue(row.desired_deleted)
            self.assertEqual(row.dispatch_token, claimed.token)
        self.assertIsNone(self._repo().claim(intent_id))

class OrphanOperatorRecoveryScenarios(PhysicalCreateIntentScenarios):
    """Operator inspection of an orphan must be durable and fail closed."""

    def _orphan(self) -> int:
        """Simulate the old owner losing its forum while Discord accepted Create."""
        intent_id = self._prepare()
        claimed = self._repo().claim(intent_id)
        with self.db.immediate_session() as session:
            session.get(ChannelSurface, self.surface).local_community_id = None
        receipt = CreatedThread(
            ThreadRef(self.forum, '987'),
            MessageRef('discord-primary', '987', '654'), 'bot',
        )
        self.assertTrue(self._repo().commit_receipt(
            intent_id, token=claimed.token, receipt=receipt,
        ))
        return intent_id

    def test_restart_inventory_has_original_typed_receipt_and_never_redispatches(self):
        """A rebinding leaves full thread and starter proof for inspection only."""
        orphan_id = self._orphan()
        pending = self._repo().recoverable_pending_ids()
        self.assertNotIn(orphan_id, pending)
        inventory = self._repo().inspect_orphans()
        self.assertEqual(len(inventory), 1)
        self.assertEqual(inventory[0]['intent_id'], orphan_id)
        self.assertEqual(inventory[0]['cleanup_state'], 'review_required')
        self.assertEqual(inventory[0]['receipt']['receipt']['thread']['thread'], '987')
        self.assertEqual(inventory[0]['receipt']['receipt']['starter']['message'], '654')
        self.assertEqual(inventory[0]['original_owner_id'], self.owner)
        self.assertEqual(inventory[0]['original_surface_id'], self.surface)

    def test_operator_may_record_retention_but_cannot_claim_unverified_removal(self):
        """Operator decision retains full evidence without authorizing a delete."""
        orphan_id = self._orphan()
        with self.assertRaises(ValueError):
            self._repo().record_orphan_retention(
                orphan_id, operator_id='ops@example.test',
                note='pretend remote deletion', cleanup_state='removed_verified')
        with self.assertRaises(ValueError):
            self._repo().record_orphan_retention(
                orphan_id, operator_id='', note='no accountable operator')
        self.assertTrue(self._repo().record_orphan_retention(
            orphan_id, operator_id='ops@example.test',
            note='Thread deletion not supported by typed port: platform admin required'))
        self.assertFalse(self._repo().record_orphan_retention(
            orphan_id, operator_id='ops@example.test', note='late duplicate'))
        with self.db.session() as session:
            row = session.get(PlatformDeliveryIntent, orphan_id)
            self.assertEqual(row.orphan_cleanup_state, 'retained')
            self.assertIn('ops@example.test', row.orphan_cleanup_evidence_json)
            self.assertIn('654', row.orphan_receipt_json)
        self.assertEqual(self._repo().inspect_orphans(), [])
