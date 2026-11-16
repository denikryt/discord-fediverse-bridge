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
from datetime import datetime, timezone
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
                    discord_guild_id=1, discord_channel_id=channel))
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
              operation='created'):
        """Construct one normalized independently signed ActivityPub comment."""
        kind = {'created': 'Create', 'updated': 'Update', 'deleted': 'Delete'}[operation]
        note = {'id': object_id, 'type': 'Note', 'attributedTo': author,
            'inReplyTo': parent, 'to': [PUBLIC] if public else [], 'content': 'Hello'}
        source = {'id': f'https://other.example/activities/{operation}/{object_id.rsplit("/", 1)[-1]}',
            'type': kind, 'actor': author, 'to': [PUBLIC] if public else [],
            'object': object_id if operation == 'deleted' else note}
        return RemotePersonCommentEvent(event_type=f'remote.person.comment.{operation}',
            delivery_id=source['id'], occurred_at=self.when, actor_id=author,
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
                return guild == 1
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
        self.assertTrue(all(call.parent_message_id is None for call in transport.calls))
        child = self.event(parent=COMMENT, object_id='https://other.example/notes/3')
        self.comments.reserve(child)
        asyncio.run(worker.drain_once())
        self.assertEqual(len(transport.calls), 4)
        parents = {call.channel_id: call.parent_message_id for call in transport.calls[2:]}
        self.assertEqual(parents, {100: 1000001, 101: 1010002})

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
                return guild_id == 1
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
            self.assertEqual(mirror.destination_thread_id, 80101)
        worker, edge = self.worker()
        asyncio.run(worker.drain_once())
        self.assertEqual(len(edge.calls), 1)
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
        reply_in_b = SimpleNamespace(id=90223, guild=SimpleNamespace(id=1),
            channel=SimpleNamespace(id=80101, parent_id=101),
            author=SimpleNamespace(id=42), content='Reply to mirror',
            reference=SimpleNamespace(message_id=1010001), created_at=self.when)
        reply = asyncio.run(runtime.publish_local_reply(message=reply_in_b))
        self.assertEqual(reply.status, 'published')
        self.assertEqual(gateway.renders[-1].in_reply_to_object_id, first.object_id)
        self.assertEqual(len(gateway.renders), 2)

    def test_two_destinations_and_duplicate(self):
        """One signed Note has one canonical row and one copy per destination."""
        self.assertEqual(self.comments.reserve(self.event()).targets, 2)
        self.assertEqual(self.comments.reserve(self.event()).status, 'duplicate')
        with self.db.session() as session:
            self.assertEqual(len(session.scalars(select(InboundConversationObject)).all()), 1)
            self.assertEqual(len(session.scalars(select(RemotePersonCommentPlacement)).all()), 2)

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
        """Unknown parents and improperly addressed or authenticated Notes are inert."""
        bad = self.event(parent='https://other.example/unknown')
        self.assertEqual(self.comments.reserve(bad).status, 'unknown_parent')
        self.assertEqual(self.comments.reserve(self.event(public=False)).status,
                         'invalid_identity_or_audience')
        spoof = self.event()
        spoof.source_activity_json['object']['attributedTo'] = BOB
        self.assertEqual(self.comments.reserve(spoof).status, 'invalid_identity_or_audience')
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


if __name__ == '__main__':
    unittest.main()
