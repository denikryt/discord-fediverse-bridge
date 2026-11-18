"""Authenticated comments reserve one durable Discord placement per retained root."""
from __future__ import annotations

import tempfile
import asyncio
import importlib.util
import sys
import types
from types import SimpleNamespace

# The persistence service imports a shared package with a Discord type-only
# annotation. Offline tests supply only this SDK boundary, never AP logic.
if importlib.util.find_spec('discord') is None:
    discord_stub = types.ModuleType('discord')
    discord_stub.MessageReference = type('MessageReference', (), {})
    sys.modules.setdefault('discord', discord_stub)
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select

from src.activitypub_models import RemotePersonCommentEvent, RemotePersonContentEvent
from src.db import Database
from src.models import (InboundConversationObject, RemotePersonChannelAssignment,
                        RemotePersonCommentPlacement, RemotePersonFollow,
                        RemotePersonPostDelivery, PublishedActivityObject, MessageMapping, User)
from src.remote_person_comments import RemotePersonCommentService
from src.remote_person_comment_delivery import RemotePersonCommentDeliveryWorker
from src.remote_person_conversations import RemotePersonConversationRuntime
from src.fedify_gateway_client import ResolvedRemotePerson, RenderPersonalFeedResult
from src.remote_person_inbound import PUBLIC, RemotePersonInboundService
from src.remote_person_feeds import RemotePersonChannelService

BOB = 'https://remote.example/users/bob'
NOTE = 'https://remote.example/notes/1'
ALICE = 'https://other.example/users/alice'
COMMENT = 'https://other.example/notes/2'


class RemotePersonComments(unittest.TestCase):
    """Run signed inbox scenarios against the real SQLite v2 schema."""

    def setUp(self):
        """Create one accepted Follow routed into two independent Discord channels."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Database(f'sqlite:///{Path(tmp.name) / "db.sqlite"}')
        self.addCleanup(self.db.close)
        self.db.migrate()
        self.when = datetime(2026, 11, 16, tzinfo=timezone.utc)
        with self.db.session() as session:
            user = User(discord_user_id='alice', activitypub_username='alice',
                actor_url='https://bridge.test/actors/users/alice',
                inbox_url='https://bridge.test/actors/users/alice/inbox',
                outbox_url='https://bridge.test/actors/users/alice/outbox',
                followers_url='https://bridge.test/actors/users/alice/followers',
                public_key_pem='pub', private_key_pem='priv')
            session.add(user)
            session.flush()
            follow = RemotePersonFollow(user_id=user.id, remote_actor_id=BOB,
                remote_inbox_url=f'{BOB}/inbox', follow_activity_id='https://bridge.test/follows/1',
                generation=1, relationship_status='accepted', follow_dispatch_state='sent')
            session.add(follow)
            session.flush()
            for channel in (100, 101):
                session.add(RemotePersonChannelAssignment(follow_id=follow.id,
                    discord_guild_id=(1 if channel == 100 else 2), discord_channel_id=channel))
        self.roots = RemotePersonInboundService(self.db)
        self.comments = RemotePersonCommentService(self.db)
        root = {'id': NOTE, 'type': 'Note', 'attributedTo': BOB,
            'to': [PUBLIC], 'content': 'Root'}
        source = {'id': 'https://remote.example/create/1', 'type': 'Create',
            'actor': BOB, 'to': [PUBLIC], 'object': root}
        event = RemotePersonContentEvent(event_type='remote.person.created',
            delivery_id=source['id'], occurred_at=self.when,
            actor_id=BOB, verified_sender_actor_id=BOB,
            source_activity_id=source['id'], source_activity_json=source,
            object={'ap_id': NOTE, 'author_name': 'bob', 'body_markdown': 'Root'})
        self.assertEqual(self.roots.reserve(event).status, 'reserved')

    def event(self, *, author=ALICE, parent=NOTE, object_id=COMMENT, public=True,
              operation='created', when=None):
        """Construct one normalized independently signed ActivityPub comment."""
        kind = {'created': 'Create', 'updated': 'Update', 'deleted': 'Delete'}[operation]
        note = {'id': object_id, 'type': 'Note', 'attributedTo': author,
            'inReplyTo': parent, 'to': [PUBLIC] if public else [], 'content': 'Hello'}
        source = {'id': f'https://other.example/activities/{operation}/{object_id.rsplit("/", 1)[-1]}',
            'type': kind, 'actor': author, 'to': [PUBLIC] if public else [],
            'object': object_id if operation == 'deleted' else note}
        return RemotePersonCommentEvent(event_type=f'remote.person.comment.{operation}',
            delivery_id=source['id'], occurred_at=when or self.when, actor_id=author,
            verified_sender_actor_id=author, source_activity_id=source['id'],
            source_activity_json=source,
            object={'ap_id': object_id, 'parent_ap_id': parent if operation != 'deleted' else None,
                    'body_markdown': 'Hello', 'author_name': 'Alice'})

    def worker(self, *, timeout=False):
        """Exercise the durable claim and per-copy Reply path with a fake Discord edge."""
        class Transport:
            """Capture outbound native references without a Discord connection."""
            def __init__(self):
                self.calls = []
                self.edits = []
                self.deletes = []
            async def edit(self, thread_id, message_id, content):
                self.edits.append((thread_id, message_id, content))
            async def delete(self, thread_id, message_id):
                self.deletes.append((thread_id, message_id))
            async def send(self, claim):
                self.calls.append(claim)
                if timeout:
                    raise TimeoutError('ambiguous Discord POST')
                return claim.channel_id * 10000 + len(self.calls)
            async def find_uncertain(self, claim):
                return None
        class Policy:
            """Permit the fixture's guild and verified author."""
            def snapshot(self):
                return self
            def is_discord_guild_allowed(self, guild):
                return guild in (1, 2)
            def federation_decision(self, actor):
                return SimpleNamespace(allowed=True)
        transport = Transport()
        worker = RemotePersonCommentDeliveryWorker(
            database=self.db, bot=None, policy_service=Policy(), transport=transport)
        return worker, transport

    def deliver_roots(self):
        """Mark fixture webhooks delivered in two distinct native Discord threads."""
        with self.db.immediate_session() as session:
            for copy in session.scalars(select(RemotePersonPostDelivery)):
                copy.state = 'delivered'
                copy.discord_root_message_id = 80000 + copy.discord_channel_id
                copy.discord_thread_id = copy.discord_root_message_id

    def test_native_parent_resolves_per_copy_and_never_flattens(self):
        """A nested comment replies to the correct Discord parent in each copy."""
        self.deliver_roots()
        self.comments.reserve(self.event())
        worker, transport = self.worker()
        asyncio.run(worker.drain_once())
        self.assertEqual(len(transport.calls), 2)
        # The child is authored by a different Actor from the root Bob.
        self.assertEqual({call.actor_id for call in transport.calls}, {ALICE})
        self.assertTrue(all(call.parent_message_id is None for call in transport.calls))
        child = self.event(parent=COMMENT, object_id='https://other.example/notes/3')
        self.comments.reserve(child)
        asyncio.run(worker.drain_once())
        self.assertEqual(len(transport.calls), 4)
        parents = {call.channel_id: call.parent_message_id for call in transport.calls[2:]}
        self.assertEqual(parents, {100: 1000001, 101: 1010002})

    def test_ready_comment_progresses_past_eighty_waiting_threads(self):
        """A ready placement cannot starve behind older unavailable root copies."""
        self.deliver_roots()
        with self.db.immediate_session() as session:
            copy = session.scalar(select(RemotePersonPostDelivery).where(
                RemotePersonPostDelivery.discord_channel_id == 101))
            copy.state = 'pending'
            copy.discord_thread_id = None
        # Each reply is retained in the delivered A thread and pending B copy;
        # suppress A for the older rows, leaving eighty genuine blocked jobs.
        for index in range(80):
            item = self.event(object_id=f'https://other.example/notes/waiting-{index}')
            self.assertEqual(self.comments.reserve(item).targets, 2)
        with self.db.immediate_session() as session:
            for row in session.scalars(select(RemotePersonCommentPlacement)):
                copy = session.get(RemotePersonPostDelivery, row.root_delivery_id)
                if copy.discord_channel_id == 100:
                    row.state = 'suppressed'
        healthy = self.event(object_id='https://other.example/notes/ready-after-eighty')
        self.assertEqual(self.comments.reserve(healthy).targets, 2)
        worker, edge = self.worker()
        asyncio.run(worker.drain_once())
        self.assertEqual(edge.calls, [])  # The first bounded window was blocked.
        # A fresh worker must continue from persisted ordering rather than
        # repeatedly selecting the original eighty pending roots.
        recovered, edge = self.worker()
        for _ in range(2):
            asyncio.run(recovered.drain_once())
            if any(call.object_id == healthy.object.ap_id for call in edge.calls):
                break
        self.assertEqual([call.channel_id for call in edge.calls if call.object_id == healthy.object.ap_id], [100])
        self.assertTrue(all(call.channel_id != 101 for call in edge.calls))
        with self.db.session() as session:
            self.assertTrue(any(row.state == 'pending_thread' for row in session.scalars(
                select(RemotePersonCommentPlacement))))

    def test_missing_parent_is_deferred_without_flattening(self):
        """A child stays pending_parent until the copy-local parent is delivered."""
        self.deliver_roots()
        self.comments.reserve(self.event())
        self.comments.reserve(self.event(parent=COMMENT, object_id='https://other.example/notes/3'))
        with self.db.immediate_session() as session:
            for parent in session.scalars(select(RemotePersonCommentPlacement).join(
                InboundConversationObject,
                RemotePersonCommentPlacement.inbound_object_id == InboundConversationObject.id,
            ).where(InboundConversationObject.object_id == COMMENT)):
                parent.state = 'failed'
        worker, edge = self.worker()
        asyncio.run(worker.drain_once())
        self.assertEqual(edge.calls, [])
        with self.db.session() as session:
            children = session.scalars(select(RemotePersonCommentPlacement).join(
                InboundConversationObject,
                RemotePersonCommentPlacement.inbound_object_id == InboundConversationObject.id,
            ).where(InboundConversationObject.object_id.endswith('/3'))).all()
            self.assertEqual({c.state for c in children}, {'pending_parent'})

    def test_ambiguous_post_stays_uncertain_without_duplicate(self):
        """A timed-out bot send does not retry automatically after a restart."""
        self.deliver_roots()
        self.comments.reserve(self.event())
        worker, edge = self.worker(timeout=True)
        asyncio.run(worker.drain_once())
        self.assertEqual(len(edge.calls), 2)
        asyncio.run(worker.drain_once())
        self.assertEqual(len(edge.calls), 2)
        with self.db.session() as session:
            self.assertEqual({p.state for p in session.scalars(select(RemotePersonCommentPlacement))},
                             {'uncertain'})


    def test_local_reply_uses_one_canonical_person_create_and_copy_specific_mirror(self):
        """A-origin User Actor Note is mirrored as bot in B, never republished."""
        self._assert_local_reply_mirroring()

    def test_twenty_unresolved_history_checks_do_not_starve_later_uncertain(self):
        """Later history-proven copies are reconciled without a second Discord POST."""
        self.deliver_roots()
        for index in range(11):
            self.assertEqual(self.comments.reserve(self.event(
                object_id=f'https://other.example/notes/reconcile-{index}')).targets, 2)
        with self.db.immediate_session() as session:
            for placement in session.scalars(select(RemotePersonCommentPlacement)):
                copy = session.get(RemotePersonPostDelivery, placement.root_delivery_id)
                canonical = session.get(InboundConversationObject, placement.inbound_object_id)
                placement.state = 'uncertain'
                placement.destination_thread_id = copy.discord_thread_id
                placement.last_applied_revision = canonical.revision_fingerprint
        worker, edge = self.worker()
        checked = []
        async def read_history(claim):
            """The first twenty Discord POSTs remain ambiguous; the last two exist."""
            checked.append(claim.object_id)
            return claim.channel_id * 1000 + 7 if claim.object_id.endswith('/reconcile-10') else None
        edge.find_uncertain = read_history
        asyncio.run(worker.drain_once())
        self.assertNotIn('https://other.example/notes/reconcile-10', checked)
        recovered, _ = self.worker()
        recovered.transport = edge
        for _ in range(2):
            asyncio.run(recovered.drain_once())
        with self.db.session() as session:
            rows = list(session.scalars(select(RemotePersonCommentPlacement)))
            self.assertEqual({row.state for row in rows[-2:]}, {'delivered'})
            self.assertEqual({row.state for row in rows[:-2]}, {'uncertain'})
            self.assertTrue(all(row.discord_message_id is not None for row in rows[-2:]))
        self.assertEqual(edge.calls, [])

    def test_unreadable_comment_history_never_authorizes_repost(self):
        """A denied Discord history read leaves uncertain identity quarantined."""
        self.deliver_roots()
        self.comments.reserve(self.event())
        worker, edge = self.worker(timeout=True)
        asyncio.run(worker.drain_once())
        self.assertEqual(len(edge.calls), 2)
        async def denied(_claim):
            """Refuse the history proof without implying the message was absent."""
            raise PermissionError('history access denied')
        edge.find_uncertain = denied
        worker.recover()
        asyncio.run(worker.drain_once())
        with self.db.session() as session:
            self.assertEqual({row.state for row in session.scalars(
                select(RemotePersonCommentPlacement))}, {'uncertain'})
        self.assertEqual(len(edge.calls), 2)

    def test_uncertain_remote_comment_recovers_sent_revision_then_edits_to_update(self):
        """A timed-out v1 bot POST must not acknowledge a later canonical v2 Update."""
        self.deliver_roots()
        self.assertEqual(self.comments.reserve(self.event()).targets, 2)
        worker, edge = self.worker(timeout=True)
        asyncio.run(worker.drain_once())
        self.assertEqual(len(edge.calls), 2)
        sent_revision = edge.calls[0].revision
        with self.db.session() as session:
            self.assertEqual({row.state for row in session.scalars(
                select(RemotePersonCommentPlacement))}, {'uncertain'})
        update = self.event(operation='updated', when=self.when + timedelta(minutes=1))
        update.source_activity_json['object']['content'] = 'Updated v2'
        update.object.body_markdown = 'Updated v2'
        self.assertEqual(self.comments.reserve(update).status, 'updated')
        with self.db.session() as session:
            canonical = session.scalar(select(InboundConversationObject))
            self.assertNotEqual(canonical.revision_fingerprint, sent_revision)
            newer_revision = canonical.revision_fingerprint
        # Missing history proof must neither retry the POST nor mark v2 sent.
        worker.recover()
        asyncio.run(worker.drain_once())
        self.assertEqual(len(edge.calls), 2)
        self.assertEqual(edge.edits, [])
        with self.db.session() as session:
            self.assertEqual({row.last_applied_revision for row in session.scalars(
                select(RemotePersonCommentPlacement))}, {sent_revision})
        async def found_in_history(claim):
            """Discord accepted v1 despite losing the POST response."""
            return claim.channel_id * 10000 + 99
        edge.find_uncertain = found_in_history
        asyncio.run(worker.drain_once())
        self.assertEqual(len(edge.calls), 2)  # no repeated POSTs
        self.assertEqual(len(edge.edits), 2)
        self.assertTrue(all('Updated v2' in content for _, _, content in edge.edits))
        with self.db.session() as session:
            placements = session.scalars(select(RemotePersonCommentPlacement)).all()
            self.assertEqual({row.state for row in placements}, {'delivered'})
            self.assertEqual({row.last_applied_revision for row in placements}, {newer_revision})
            self.assertTrue(all(row.discord_message_id is not None for row in placements))
        asyncio.run(worker.drain_once())
        self.assertEqual(len(edge.edits), 2)

    def test_uncertain_local_mirror_reconciles_without_blocking_other_comments(self):
        """A timed-out local mirror is found once and does not stop unrelated deliveries."""
        self._assert_local_reply_mirroring(uncertain_mirror=True)

    def test_local_mirror_waits_for_an_undelivered_webhook_root(self):
        """A pending root in B retains A's reply until B is sent and threaded."""
        self._assert_local_reply_mirroring(pending_root=True)

    def test_unbinding_pending_root_suppresses_its_reserved_local_mirror(self):
        """Last-link removal cancels old pending root and mirror in one transaction."""
        self._assert_local_reply_mirroring(pending_root=True, unbind_before_delivery=True)

    def _assert_local_reply_mirroring(self, *, pending_root=False,
                                     unbind_before_delivery=False,
                                     uncertain_mirror=False):
        """Run canonical publish and native parent resolution across two copies."""
        self.deliver_roots()
        with self.db.immediate_session() as session:
            user = session.scalar(select(User))
            user.discord_user_id = '42'
        class Gateway:
            """Capture the exact canonical render and verified remote recipient."""
            def __init__(self):
                self.renders = []
            async def resolve_remote_person(self, actor_id):
                return ResolvedRemotePerson(actor_id=actor_id, inbox_url=f'{actor_id}/inbox')
            async def render_personal_feed(self, request):
                self.renders.append(request)
                return RenderPersonalFeedResult(
                    activity_id=request.activity_id, object_id=request.object_id,
                    published_at=request.published_at,
                    activity_json={'id': request.activity_id, 'type': 'Create',
                        'actor': 'https://bridge.test/actors/users/alice',
                        'to': [PUBLIC],
                        'cc': ['https://bridge.test/actors/users/alice', 'https://bridge.test/actors/users/alice/followers', *request.direct_person_actor_ids],
                        'object': {'id': request.object_id, 'type': 'Note',
                            'attributedTo': 'https://bridge.test/actors/users/alice',
                            'inReplyTo': request.in_reply_to_object_id, 'to': [PUBLIC],
                            'cc': ['https://bridge.test/actors/users/alice/followers', *request.direct_person_actor_ids]}},
                )
        class Policy:
            """Permit authorized public remote-Person and guild routing."""
            def is_discord_guild_allowed(self, guild_id):
                return guild_id in (1, 2)
            def federation_decision(self, actor_id):
                return SimpleNamespace(allowed=True)
            def snapshot(self):
                return self
        class Ban:
            """Use no global bans for this registered user fixture."""
            def check_global_discord_user(self, user_id):
                return SimpleNamespace(banned=False)
        class Wake:
            """Record scheduler wake without starting a background thread."""
            def __init__(self):
                self.calls = 0
            def wake(self):
                self.calls += 1
        gateway = Gateway()
        wake = Wake()
        runtime = RemotePersonConversationRuntime(
            database=self.db, fedify_gateway=gateway, policy_service=Policy(),
            user_ban_service=Ban(), fedify_origin='https://bridge.test',
            delivery_worker=wake, user_delivery_worker=wake)
        with self.db.immediate_session() as session:
            # The second webhook root exists, but its bot-created thread has
            # not yet succeeded when Alice sends the first local reply.
            delayed = session.scalar(select(RemotePersonPostDelivery).where(
                RemotePersonPostDelivery.discord_channel_id == 101))
            delayed.discord_thread_id = None
            if pending_root:
                # B's webhook POST is not yet sent at reply-reservation time.
                delayed.state = 'pending'
                delayed.discord_root_message_id = None
        original_thread = 80100
        message = SimpleNamespace(id=90222, guild=SimpleNamespace(id=1),
            channel=SimpleNamespace(id=original_thread, parent_id=100),
            author=SimpleNamespace(id=42), content='This is Alice',
            reference=None, created_at=self.when)
        first = asyncio.run(runtime.publish_local_reply(message=message))
        second = asyncio.run(runtime.publish_local_reply(message=message))
        self.assertEqual(first.status, 'published')
        self.assertEqual(second.status, 'already_published')
        self.assertEqual(len(gateway.renders), 1)
        self.assertEqual(gateway.renders[0].direct_person_actor_ids, [BOB])
        with self.db.session() as session:
            self.assertEqual(session.scalar(select(PublishedActivityObject.object_id)), first.object_id)
            self.assertEqual(session.scalar(select(MessageMapping.discord_message_id)), 90222)
            mirror = session.scalar(select(RemotePersonCommentPlacement))
            self.assertIsNotNone(mirror.published_object_id)
            self.assertEqual(mirror.state, 'pending_thread')
            self.assertIsNone(mirror.destination_thread_id)
        if unbind_before_delivery:
            removal = RemotePersonChannelService(self.db).remove_channel(
                discord_user_id='42', remote_actor_id=BOB,
                discord_guild_id=2, discord_channel_id=101)
            self.assertEqual(removal.status, 'removed')
            with self.db.session() as session:
                pending_copy = session.scalar(select(RemotePersonPostDelivery).where(
                    RemotePersonPostDelivery.discord_channel_id == 101))
                mirror = session.scalar(select(RemotePersonCommentPlacement).where(
                    RemotePersonCommentPlacement.root_delivery_id == pending_copy.id))
                self.assertEqual((pending_copy.state, mirror.state),
                                 ('suppressed', 'suppressed'))
            worker, edge = self.worker()
            asyncio.run(worker.drain_once())
            self.assertEqual(edge.calls, [])
            return
        if pending_root:
            from src.remote_person_discord_worker import RemotePersonRootDeliveryWorker

            class WebhookTransport:
                """Create exactly one webhook root and public thread in B."""
                def __init__(self):
                    self.created = []
                    self.threads = []
                async def prepare(self, channel_id, *, original_webhook_id=None):
                    """Return the incoming webhook installed in the target channel."""
                    return SimpleNamespace(webhook_id=987)
                async def create(self, prepared, root):
                    """Return Discord's confirmed root message ID."""
                    self.created.append(root.channel_id)
                    return 80101
                async def create_thread(self, channel_id, message_id, root):
                    """Create a thread attached to the delivered webhook root."""
                    self.threads.append((channel_id, message_id))
                    return message_id
                async def edit(self, prepared, message_id, root):
                    """Permit existing delivered fixture roots to reconcile."""
                    return None

            webhook = WebhookTransport()
            root_worker = RemotePersonRootDeliveryWorker(
                database=self.db, bot=None, policy_service=Policy(), transport=webhook)
            asyncio.run(root_worker.drain_once())
            self.assertEqual(webhook.created, [101])
            self.assertEqual(webhook.threads, [(101, 80101)])
        else:
            with self.db.immediate_session() as session:
                delayed = session.scalar(select(RemotePersonPostDelivery).where(
                    RemotePersonPostDelivery.discord_channel_id == 101))
                delayed.discord_thread_id = 80101
        worker, edge = self.worker()
        if uncertain_mirror:
            # A different inbound comment must still be sent on both copies,
            # even when this published local mirror becomes uncertain.
            self.assertEqual(self.comments.reserve(self.event()).targets, 2)
            original_send = edge.send
            async def timed_out_mirror(claim):
                """Only the locally published mirror times out after Discord accepts it."""
                if claim.object_id == first.object_id:
                    edge.calls.append(claim)
                    # The canonical local Note advances after the v1 POST but
                    # before reconciliation discovers its Discord message.
                    with self.db.immediate_session() as session:
                        published = session.scalar(select(PublishedActivityObject).where(
                            PublishedActivityObject.object_id == first.object_id))
                        published.content_revision += 1
                        published.body_markdown = 'This is Alice v2'
                    raise TimeoutError('mirror response lost after Discord accepted it')
                return await original_send(claim)
            async def find_mirror(claim):
                """Return the exact pre-existing bot mirror from thread history."""
                self.assertEqual(claim.object_id, first.object_id)
                return 910101
            edge.send = timed_out_mirror
            edge.find_uncertain = find_mirror
            asyncio.run(worker.drain_once())
            self.assertEqual(len(edge.calls), 3)
            self.assertEqual(len(edge.edits), 1)
            self.assertIn('This is Alice v2', edge.edits[0][2])
            with self.db.session() as session:
                mirror = session.scalar(select(RemotePersonCommentPlacement).where(
                    RemotePersonCommentPlacement.published_object_id.is_not(None)))
                published = session.get(PublishedActivityObject, mirror.published_object_id)
                self.assertEqual(mirror.state, 'delivered')
                self.assertEqual(mirror.discord_message_id, 910101)
                self.assertEqual(mirror.last_applied_revision, f'published:{published.content_revision}')
                self.assertEqual(len(session.scalars(select(RemotePersonCommentPlacement).where(
                    RemotePersonCommentPlacement.inbound_object_id.is_not(None),
                    RemotePersonCommentPlacement.state == 'delivered')).all()), 2)
            asyncio.run(worker.drain_once())
            self.assertEqual(len(edge.calls), 3)  # neither mirror nor comments re-sent
            return
        asyncio.run(worker.drain_once())
        self.assertEqual(len(edge.calls), 1)
        asyncio.run(worker.drain_once())
        self.assertEqual(len(edge.calls), 1)  # no duplicate after the next drain
        self.assertEqual(edge.calls[0].channel_id, 101)
        self.assertIsNone(edge.calls[0].parent_message_id)
        # A remote reply to Alice's canonical Note references her original
        # source message in A and its bot mirror in B.
        child = self.event(parent=first.object_id,
            object_id='https://other.example/notes/reply-to-alice')
        self.assertEqual(self.comments.reserve(child).targets, 2)
        asyncio.run(worker.drain_once())
        expected = {100: 90222, 101: edge.calls[0].channel_id * 10000 + 1}
        self.assertEqual({call.channel_id: call.parent_message_id for call in edge.calls[1:]}, expected)
        # A Discord Reply in B to A's bot mirror maps to the same canonical
        # AP parent and creates one new local Note, never a new copy of A.
        reply_in_b = SimpleNamespace(id=90223, guild=SimpleNamespace(id=2),
            channel=SimpleNamespace(id=80101, parent_id=101),
            author=SimpleNamespace(id=42), content='Reply to mirror',
            reference=SimpleNamespace(message_id=1010001), created_at=self.when)
        reply = asyncio.run(runtime.publish_local_reply(message=reply_in_b))
        self.assertEqual(reply.status, 'published')
        self.assertEqual(gateway.renders[-1].in_reply_to_object_id, first.object_id)
        self.assertEqual(len(gateway.renders), 2)
        # Mutations of Alice's original source affect only its retained bot
        # copies. The source in A is not overwritten by the bridge.
        with self.db.immediate_session() as session:
            source = session.scalar(select(PublishedActivityObject).where(
                PublishedActivityObject.object_id == first.object_id))
            source.body_markdown = 'Edited by Alice'
            source.content_revision += 1
        asyncio.run(worker.drain_once())
        self.assertEqual(len(edge.edits), 1)
        self.assertIn('Edited by Alice', edge.edits[0][2])
        with self.db.immediate_session() as session:
            source = session.scalar(select(PublishedActivityObject).where(
                PublishedActivityObject.object_id == first.object_id))
            source.deleted_at = self.when + timedelta(minutes=2)
        asyncio.run(worker.drain_once())
        self.assertEqual(len(edge.deletes), 1)
        self.assertEqual(edge.deletes[0][0], 80101)

    def test_remote_update_delete_mutates_retained_copy_after_unfollow(self):
        """Original bot message IDs survive Update/Delete and owner cancellation."""
        self.deliver_roots()
        self.comments.reserve(self.event())
        worker, edge = self.worker()
        asyncio.run(worker.drain_once())
        self.assertEqual(len(edge.calls), 2)
        self.assertEqual(edge.edits, [])
        updated = self.event(operation='updated', when=self.when + timedelta(minutes=1))
        updated.source_activity_json['object']['content'] = 'Edited'
        updated.object.body_markdown = 'Edited'
        self.assertEqual(self.comments.reserve(updated).status, 'updated')
        with self.db.immediate_session() as session:
            for follow in session.scalars(select(RemotePersonFollow)):
                follow.relationship_status = 'cancel_requested'
        asyncio.run(worker.drain_once())
        self.assertEqual(len(edge.edits), 2)
        self.assertTrue(all('Edited' in content for _, _, content in edge.edits))
        # The edited bot copies keep the exact canonical comment identity and
        # the comment author's host, not their root publisher's host.
        self.assertTrue(all(content.startswith('`Alice@other.example` [↗](<' + COMMENT + '>)')
                            for _, _, content in edge.edits))
        self.assertTrue(all(content.count('[↗](<') == 1 for _, _, content in edge.edits))
        self.assertEqual(self.comments.reserve(self.event(operation='deleted')).status, 'deleted')
        asyncio.run(worker.drain_once())
        self.assertEqual(len(edge.deletes), 2)
        with self.db.session() as session:
            placements = session.scalars(select(RemotePersonCommentPlacement)).all()
            self.assertEqual({p.state for p in placements}, {'delivered'})
            self.assertEqual({p.last_applied_revision for p in placements}, {'deleted'})

    def test_two_destinations_and_duplicate(self):
        """One signed Note has one canonical row and one copy per destination."""
        self.assertEqual(self.comments.reserve(self.event()).targets, 2)
        self.assertEqual(self.comments.reserve(self.event()).status, 'duplicate')
        with self.db.session() as session:
            self.assertEqual(len(session.scalars(select(InboundConversationObject)).all()), 1)
            self.assertEqual(len(session.scalars(select(RemotePersonCommentPlacement)).all()), 2)

    def test_canonical_reply_survives_unavailable_copy_without_new_placement(self):
        """Accepted destinations retain AP ancestry even when no root can dispatch."""
        with self.db.immediate_session() as session:
            for copy in session.scalars(select(RemotePersonPostDelivery)):
                copy.state = 'failed'
        first = self.comments.reserve(self.event())
        self.assertEqual((first.status, first.targets), ('reserved', 0))
        with self.db.session() as session:
            self.assertEqual(len(session.scalars(select(InboundConversationObject)).all()), 1)
            self.assertEqual(len(session.scalars(select(RemotePersonCommentPlacement)).all()), 0)

        # A new reply may be queued for an accepted channel once its root can
        # be dispatched, without replaying older work in a failed destination.
        with self.db.immediate_session() as session:
            copy = session.scalar(select(RemotePersonPostDelivery).where(
                RemotePersonPostDelivery.discord_channel_id == 101))
            copy.state = 'pending'
        next_reply = self.comments.reserve(self.event(
            parent=COMMENT, object_id='https://other.example/notes/later'))
        self.assertEqual((next_reply.status, next_reply.targets), ('reserved', 1))
        with self.db.session() as session:
            placements = session.scalars(select(RemotePersonCommentPlacement)).all()
            self.assertEqual(len(placements), 1)
            self.assertEqual(session.get(RemotePersonPostDelivery, placements[0].root_delivery_id).discord_channel_id, 101)
            self.assertEqual(placements[0].state, 'pending_thread')

    def test_denied_destination_keeps_other_accepted_copy(self):
        """Guild policy rejects only the forbidden target of a signed reply."""
        result = self.comments.reserve(self.event(), guild_allowed=lambda guild: guild == 1)
        self.assertEqual(result.targets, 1)
        # A denied guild for this transaction must not reserve even an AP row.
        another = self.comments.reserve(self.event(
            object_id='https://other.example/notes/denied'),
            guild_allowed=lambda guild: False)
        self.assertEqual(another.status, 'no_accepted_assignment')
        with self.db.session() as session:
            ids = {row.object_id for row in session.scalars(select(InboundConversationObject))}
            self.assertNotIn('https://other.example/notes/denied', ids)

    def test_child_of_inbound_parent_keeps_root_identity(self):
        """Nested remote replies reuse retained AP ancestry and root copies."""
        self.comments.reserve(self.event())
        child = self.event(parent=COMMENT, object_id='https://other.example/notes/3')
        self.assertEqual(self.comments.reserve(child).targets, 2)
        with self.db.session() as session:
            obj = session.scalar(select(InboundConversationObject).where(
                InboundConversationObject.object_id == child.object.ap_id))
            self.assertEqual(obj.parent_object_id, COMMENT)
            self.assertEqual(obj.conversation_root_object_id, NOTE)

    def test_unrelated_private_and_impersonated_are_not_saved(self):
        """Unknown parents are staged; private/forged Notes never enter storage."""
        bad = self.event(parent='https://other.example/unknown')
        self.assertEqual(self.comments.reserve(bad).status, 'pending_parent')
        self.assertEqual(self.comments.reserve(self.event(public=False)).status,
                         'invalid_identity_or_audience')
        spoof = self.event()
        spoof.source_activity_json['object']['attributedTo'] = BOB
        self.assertEqual(self.comments.reserve(spoof).status, 'invalid_identity_or_audience')
        with self.db.session() as session:
            orphans = session.scalars(select(InboundConversationObject)).all()
            self.assertEqual([row.object_id for row in orphans], [COMMENT])
            self.assertEqual(orphans[0].conversation_root_object_id, '')
            self.assertEqual(session.scalars(select(RemotePersonCommentPlacement)).all(), [])

    def test_signed_child_before_parent_is_restored_without_flattening(self):
        """Delayed canonical parent restores child and its two copy-local replies."""
        self.deliver_roots()
        child_id = 'https://other.example/notes/3'
        self.assertEqual(self.comments.reserve(self.event(parent=COMMENT, object_id=child_id)).status,
                         'pending_parent')
        self.assertEqual(self.comments.reserve(self.event()).status, 'reserved')
        with self.db.session() as session:
            child = session.scalar(select(InboundConversationObject).where(
                InboundConversationObject.object_id == child_id))
            self.assertEqual(child.conversation_root_object_id, NOTE)
            self.assertEqual(len(session.scalars(select(RemotePersonCommentPlacement).where(
                RemotePersonCommentPlacement.inbound_object_id == child.id)).all()), 2)
        worker, edge = self.worker()
        asyncio.run(worker.drain_once())
        self.assertEqual(len(edge.calls), 4)
        self.assertEqual({call.channel_id: call.parent_message_id for call in edge.calls[2:]},
                         {100: 1000001, 101: 1010002})

    def test_unrenderable_comment_is_terminal_without_discord_post(self):
        """Long signed Note identities cannot create permanent uncertain entries."""
        oversized = 'https://other.example/notes/' + 'x' * 2200
        self.deliver_roots()
        self.assertEqual(self.comments.reserve(self.event(object_id=oversized)).status, 'reserved')
        worker, edge = self.worker()
        asyncio.run(worker.drain_once())
        self.assertEqual(edge.calls, [])
        with self.db.session() as session:
            self.assertEqual({row.state for row in session.scalars(
                select(RemotePersonCommentPlacement))}, {'failed'})
        asyncio.run(worker.drain_once())
        self.assertEqual(edge.calls, [])


    def test_signed_delete_before_create_blocks_late_discord_comment(self):
        """A signed tombstone survives reorder without manufacturing a message."""
        self.deliver_roots()
        self.assertEqual(self.comments.reserve(self.event(operation='deleted')).status,
                         'deleted_before_create')
        self.assertEqual(self.comments.reserve(self.event()).status, 'deleted')
        worker, edge = self.worker()
        asyncio.run(worker.drain_once())
        self.assertEqual(edge.calls, [])
        with self.db.session() as session:
            self.assertIsNotNone(session.scalar(select(InboundConversationObject).where(
                InboundConversationObject.object_id == COMMENT)).deleted_at)

    def test_comment_update_order_uses_utc_instant_across_sqlite_sessions(self):
        """Offsets cannot make a chronologically newer signed Note look stale."""
        self.assertEqual(self.comments.reserve(self.event()).status, 'reserved')
        a = datetime(2026, 11, 16, 8, 10, tzinfo=timezone(timedelta(hours=2)))
        b = datetime(2026, 11, 16, 6, 20, tzinfo=timezone.utc)
        self.assertEqual(self.comments.reserve(self.event(operation='updated', when=a)).status, 'updated')
        self.assertEqual(self.comments.reserve(self.event(operation='updated', when=b)).status, 'updated')
        self.assertEqual(self.comments.reserve(self.event(operation='updated', when=a)).status, 'stale')
        with self.db.session() as session:
            self.assertEqual(session.scalar(select(InboundConversationObject.updated_at_remote)),
                             b.replace(tzinfo=None))


    def test_unproven_delete_cannot_claim_another_actor_object(self):
        """An orphan Delete only suppresses a later Create by its own signer."""
        self.assertEqual(self.comments.reserve(self.event(
            author='https://evil.example/users/mallory', operation='deleted')).status,
            'deleted_before_create')
        self.assertEqual(self.comments.reserve(self.event()).status, 'reserved')
        with self.db.session() as session:
            canonical = session.scalar(select(InboundConversationObject).where(
                InboundConversationObject.object_id == COMMENT))
            self.assertEqual(canonical.actor_id, ALICE)
            self.assertIsNone(canonical.deleted_at)

    def test_unrelated_comments_do_not_accumulate_without_any_accepted_follow(self):
        """Unresolved signed Notes have no durable admission without a subscriber."""
        with self.db.immediate_session() as session:
            for follow in session.scalars(select(RemotePersonFollow)):
                follow.relationship_status = 'cancel_requested'
        self.assertEqual(self.comments.reserve(self.event(
            parent='https://other.example/unknown')).status, 'deferred_not_admitted')
        with self.db.session() as session:
            self.assertEqual(session.scalars(select(InboundConversationObject)).all(), [])

    def test_unsubscribe_before_create_does_not_retain_reply(self):
        """A deleted accepted assignment prevents new comment fanout."""
        with self.db.immediate_session() as session:
            for follow in session.scalars(select(RemotePersonFollow)):
                follow.relationship_status = 'cancel_requested'
        self.assertEqual(self.comments.reserve(self.event()).status, 'no_accepted_assignment')

    def test_deleted_comment_does_not_repost(self):
        """Signed Delete marks the exact author's canonical comment tombstoned."""
        self.assertEqual(self.comments.reserve(self.event()).targets, 2)
        self.assertEqual(self.comments.reserve(self.event(operation='deleted')).status, 'deleted')
        with self.db.session() as session:
            obj = session.scalar(select(InboundConversationObject))
            self.assertIsNotNone(obj.deleted_at)
            self.assertEqual({p.state for p in session.scalars(select(RemotePersonCommentPlacement))},
                             {'suppressed'})


    def test_preflight_permission_failure_is_bounded_not_ambiguous(self):
        """Before-send denial is a known failure, never a blind network retry."""
        self.deliver_roots()
        self.assertEqual(self.comments.reserve(self.event()).targets, 2)
        worker, edge = self.worker()
        async def deny(_claim):
            """Represent revoked effective permissions before the Discord POST."""
            raise PermissionError('missing send_messages_in_threads')
        edge.send = deny
        asyncio.run(worker.drain_once())
        with self.db.session() as session:
            rows = list(session.scalars(select(RemotePersonCommentPlacement)))
            self.assertEqual({row.state for row in rows}, {'pending'})
            self.assertEqual({row.attempt_count for row in rows}, {1})
            # A proven-unsent attempt must not leave a false applied revision.
            self.assertTrue(all(row.last_applied_revision is None for row in rows))
            self.assertTrue(all(row.next_attempt_at is not None for row in rows))
        # Make due and exhaust bounded failures without any ambiguous send.
        for _ in range(2):
            with self.db.immediate_session() as session:
                for row in session.scalars(select(RemotePersonCommentPlacement)):
                    row.next_attempt_at = None
            asyncio.run(worker.drain_once())
        with self.db.session() as session:
            self.assertEqual({row.state for row in session.scalars(
                select(RemotePersonCommentPlacement))}, {'failed'})

    def test_missing_native_reply_parent_stays_failed_closed(self):
        """An API 10008 at the send boundary must not flatten a known Reply."""
        self.deliver_roots()
        self.comments.reserve(self.event())
        worker, edge = self.worker()
        asyncio.run(worker.drain_once())
        child = self.event(parent=COMMENT, object_id='https://other.example/notes/child')
        self.assertEqual(self.comments.reserve(child).targets, 2)
        class UnknownMessage(Exception):
            """Discord's native Reply failure after a parent disappears."""
            code = 10008
        async def deny_reference(_claim):
            """Reject a missing parent; never return a flattened message ID."""
            raise UnknownMessage('Unknown Message')
        edge.send = deny_reference
        asyncio.run(worker.drain_once())
        with self.db.session() as session:
            children = list(session.scalars(select(RemotePersonCommentPlacement).join(
                InboundConversationObject,
                RemotePersonCommentPlacement.inbound_object_id == InboundConversationObject.id,
            ).where(InboundConversationObject.object_id == child.object.ap_id)))
            self.assertTrue(all(row.state == 'pending_parent' and row.discord_message_id is None
                                for row in children))

    def test_forty_failing_comment_edits_do_not_starve_healthy_copy(self):
        """An unrelated failed Discord Edit cannot consume the same batch forever."""
        self.deliver_roots()
        for index in range(21):
            self.comments.reserve(self.event(object_id=f'https://other.example/notes/edit-{index}'))
        with self.db.immediate_session() as session:
            for row in session.scalars(select(RemotePersonCommentPlacement)):
                copy = session.get(RemotePersonPostDelivery, row.root_delivery_id)
                row.state = 'delivered'
                row.destination_thread_id = copy.discord_thread_id
                row.discord_message_id = 2000000 + row.id
                row.last_applied_revision = 'stale'
        worker, edge = self.worker()
        calls = []
        async def edit(thread_id, message_id, content):
            """Forty older edits fail; the two youngest copies can be updated."""
            calls.append(message_id)
            if message_id <= 2000040:
                raise TimeoutError('independent Discord edit failure')
            edge.edits.append((thread_id, message_id, content))
        edge.edit = edit
        for _ in range(3):
            asyncio.run(worker._mutate_retained())
        self.assertEqual({message_id for _, message_id, _ in edge.edits}, {2000041, 2000042})
        with self.db.session() as session:
            rows = list(session.scalars(select(RemotePersonCommentPlacement).order_by(
                RemotePersonCommentPlacement.id)))
            self.assertEqual({row.last_applied_revision for row in rows[:40]}, {'stale'})
            self.assertNotEqual(rows[-1].last_applied_revision, 'stale')
        self.assertEqual(edge.calls, [])  # Mutation only; no extra Create.

    def test_removed_bot_copy_delete_is_confirmed_once(self):
        """Deleting a Discord bot copy already absent is an idempotent mutation."""
        self.deliver_roots()
        self.comments.reserve(self.event())
        worker, edge = self.worker()
        asyncio.run(worker.drain_once())
        self.comments.reserve(self.event(operation='deleted'))
        class UnknownMessage(Exception):
            """A definite Discord Unknown Message is not a transient timeout."""
            code = 10008
        async def absent(thread_id, message_id):
            """Simulate an already-deleted copy in the retained thread."""
            edge.deletes.append((thread_id, message_id))
            raise UnknownMessage('Unknown Message')
        edge.delete = absent
        asyncio.run(worker.drain_once())
        asyncio.run(worker.drain_once())
        self.assertEqual(len(edge.deletes), 2)
        with self.db.session() as session:
            self.assertEqual({row.last_applied_revision for row in session.scalars(
                select(RemotePersonCommentPlacement))}, {'deleted'})


    def test_missing_bot_copy_edit_does_not_retry_forever(self):
        """An externally removed bot copy records a terminal error on Update."""
        self.deliver_roots()
        self.comments.reserve(self.event())
        worker, edge = self.worker()
        asyncio.run(worker.drain_once())
        updated = self.event(operation='updated', when=self.when + timedelta(minutes=1))
        updated.source_activity_json['object']['content'] = 'Updated'
        self.comments.reserve(updated)
        class UnknownMessage(Exception):
            """Discord 10008 identifies the removed original bot comment."""
            code = 10008
        async def absent(thread_id, message_id, content):
            """Return a known absence instead of a transient transport failure."""
            edge.edits.append((thread_id, message_id, content))
            raise UnknownMessage('Unknown Message')
        edge.edit = absent
        asyncio.run(worker.drain_once())
        asyncio.run(worker.drain_once())
        self.assertEqual(len(edge.edits), 2)
        with self.db.session() as session:
            self.assertEqual({row.state for row in session.scalars(
                select(RemotePersonCommentPlacement))}, {'failed'})


if __name__ == '__main__':
    unittest.main()
