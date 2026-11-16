"""Behavioral SQLite tests for root webhook delivery, cancellation, and recovery."""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import select

from src.activitypub_models import RemotePersonContentEvent
from src.db import Database
from src.models import (RemotePersonChannelAssignment, RemotePersonFollow,
                        RemotePersonPost, RemotePersonPostDelivery, User)
from src.remote_person_discord_worker import RemotePersonRootDeliveryWorker
from src.remote_person_inbound import RemotePersonInboundService, PUBLIC
from src.remote_person_webhooks import RemotePersonWebhookTransport, PreparedRemoteWebhook, remote_root_content
from src.remote_person_feeds import RemotePersonChannelService

ACTOR = 'https://remote.example/users/bob'
OBJECT = 'https://remote.example/users/bob/statuses/7'


class Policy:
    """An allow-all policy with controllable safety decisions."""
    def __init__(self):
        self.allowed = True

    def snapshot(self):
        return self

    def is_discord_guild_allowed(self, guild_id):
        return self.allowed

    def federation_decision(self, actor_id):
        return SimpleNamespace(allowed=self.allowed)


class WebhookEdge:
    """Record Discord effects without pretending that a live API was exercised."""
    def __init__(self):
        self.creates = []
        self.threads = []
        self.edits = []
        self.deletes = []
        self.reconcile_to = {}
        self.timeout = False
        self.fail_channel = None
        self.fail_thread = False
        self.before_authorization = None

    async def prepare(self, channel_id, *, original_webhook_id=None):
        if channel_id == self.fail_channel:
            raise PermissionError('effective channel permissions revoked')
        if self.before_authorization is not None:
            callback = self.before_authorization
            self.before_authorization = None
            callback()
        return SimpleNamespace(webhook_id=original_webhook_id or 41 + channel_id)

    async def create(self, prepared, post):
        self.creates.append((post.channel_id, post.object_id, prepared.webhook_id))
        if self.timeout:
            raise TimeoutError('Discord POST outcome unknown')
        return 100000 + post.channel_id

    async def create_thread(self, channel_id, root_message_id, post):
        self.threads.append((channel_id, root_message_id))
        if self.fail_thread:
            raise PermissionError('thread creation denied')
        return root_message_id

    async def find_created_root(self, channel_id, webhook_id, object_id):
        return self.reconcile_to.get(channel_id)

    async def edit(self, prepared, message_id, post):
        self.edits.append((prepared.webhook_id, message_id, post.content_markdown))

    async def delete(self, prepared, message_id):
        self.deletes.append((prepared.webhook_id, message_id))


class RemotePersonDiscordDeliveryScenarios(unittest.TestCase):
    """Exercise durable worker state transitions using real SQLite constraints."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db = Database(f'sqlite:///{Path(temp.name) / "feed.sqlite"}')
        self.addCleanup(self.db.close)
        self.db.migrate()
        self.policy = Policy()
        self.edge = WebhookEdge()
        self.worker = RemotePersonRootDeliveryWorker(database=self.db, bot=None,
                                                     policy_service=self.policy,
                                                     transport=self.edge)
        self.inbound = RemotePersonInboundService(self.db)
        self.when = datetime(2026, 11, 16, 6, 0, tzinfo=timezone.utc)
        with self.db.session() as session:
            for user_ix in (1, 2):
                uri = f'https://bridge.example/actors/users/local{user_ix}'
                user = User(discord_user_id=str(user_ix), activitypub_username=f'local{user_ix}',
                            actor_url=uri, inbox_url=f'{uri}/inbox', outbox_url=f'{uri}/outbox',
                            followers_url=f'{uri}/followers', public_key_pem='pub', private_key_pem='priv')
                session.add(user)
                session.flush()
                follow = RemotePersonFollow(user_id=user.id, remote_actor_id=ACTOR,
                    remote_inbox_url=f'{ACTOR}/inbox', follow_activity_id=f'{uri}/follow/{user_ix}',
                    generation=1, relationship_status='accepted', follow_dispatch_state='sent')
                session.add(follow)
                session.flush()
                session.add(RemotePersonChannelAssignment(follow_id=follow.id,
                    discord_channel_id=100, discord_guild_id=1))
                if user_ix == 1:
                    session.add(RemotePersonChannelAssignment(follow_id=follow.id,
                        discord_channel_id=101, discord_guild_id=1))

    def event(self, operation='created', *, when=None):
        """Build the signed-event shape the gateway passes to Python."""
        when = when or self.when
        kind = {'created': 'Create', 'updated': 'Update', 'deleted': 'Delete'}[operation]
        note = {'type': 'Note', 'id': OBJECT, 'attributedTo': ACTOR,
                'content': 'Edited text' if operation == 'updated' else 'Hello from Bob',
                'to': [PUBLIC], 'published': self.when.isoformat()}
        source = {'type': kind, 'id': f'https://remote.example/activities/{operation}-{int(when.timestamp())}',
                  'actor': ACTOR, 'to': [PUBLIC],
                  'object': OBJECT if operation == 'deleted' else note}
        return RemotePersonContentEvent(event_type=f'remote.person.{operation}',
            delivery_id=source['id'], actor_id=ACTOR, verified_sender_actor_id=ACTOR,
            source_activity_id=source['id'], source_activity_json=source,
            occurred_at=when, object={'ap_id': OBJECT, 'body_markdown': note['content'],
                'author_name': 'bob', 'published_at': self.when})

    def root_rows(self):
        with self.db.session() as session:
            return [(x.discord_channel_id, x.state, x.discord_root_message_id,
                     x.discord_thread_id, x.last_applied_revision, x.last_error)
                    for x in session.scalars(select(RemotePersonPostDelivery).order_by(
                        RemotePersonPostDelivery.discord_channel_id))]

    def run_drain(self):
        asyncio.run(self.worker.drain_once())

    def test_root_fanout_is_once_per_channel_and_thread(self):
        self.assertEqual(self.inbound.reserve(self.event()).target_count, 2)
        self.run_drain()
        self.assertEqual(len(self.edge.creates), 2)
        self.assertEqual({c[0] for c in self.edge.creates}, {100, 101})
        self.assertEqual(len(self.edge.threads), 2)
        self.assertTrue(all(x[1] == 'delivered' and x[2] == x[3] for x in self.root_rows()))
        self.run_drain()
        self.assertEqual(len(self.edge.creates), 2)

    def test_unsubscribe_queued_post_and_resubscribe_does_not_replay(self):
        self.inbound.reserve(self.event())
        with self.db.immediate_session() as session:
            for follow in session.scalars(select(RemotePersonFollow)):
                follow.relationship_status = 'cancel_requested'
        self.run_drain()
        self.assertEqual(self.edge.creates, [])
        self.assertEqual({x[1] for x in self.root_rows()}, {'suppressed'})
        with self.db.immediate_session() as session:
            for follow in session.scalars(select(RemotePersonFollow)):
                follow.relationship_status = 'accepted'
        self.run_drain()
        self.assertEqual(self.edge.creates, [])

    def test_revoked_policy_prevents_new_root_only(self):
        self.inbound.reserve(self.event())
        self.policy.allowed = False
        self.run_drain()
        self.assertEqual(self.edge.creates, [])
        self.assertEqual({x[1] for x in self.root_rows()}, {'suppressed'})

    def test_preparation_failure_is_isolated_and_does_not_send(self):
        self.inbound.reserve(self.event())
        self.edge.fail_channel = 100
        self.run_drain()
        self.assertEqual([x[0] for x in self.edge.creates], [101])
        self.assertEqual({x[0]: x[1] for x in self.root_rows()}, {100: 'pending', 101: 'delivered'})

    def test_unknown_webhook_post_never_blindly_repeats(self):
        self.inbound.reserve(self.event())
        self.edge.timeout = True
        self.run_drain()
        self.assertEqual(len(self.edge.creates), 2)
        self.assertEqual({x[1] for x in self.root_rows()}, {'uncertain'})
        self.edge.timeout = False
        self.run_drain()
        self.assertEqual(len(self.edge.creates), 2)
        self.edge.reconcile_to = {100: 100100, 101: 100101}
        self.run_drain()
        self.assertEqual({x[1] for x in self.root_rows()}, {'delivered'})

    def test_lost_binding_after_claim_before_send_suppresses_without_post(self):
        self.inbound.reserve(self.event())
        def revoke():
            with self.db.immediate_session() as session:
                for follow in session.scalars(select(RemotePersonFollow)):
                    follow.relationship_status = 'cancel_requested'
        self.edge.before_authorization = revoke
        self.run_drain()
        self.assertEqual(self.edge.creates, [])
        self.assertEqual({x[1] for x in self.root_rows()}, {'suppressed'})

    def test_thread_failure_retains_root_without_duplicate_post(self):
        self.inbound.reserve(self.event())
        self.edge.fail_thread = True
        self.run_drain()
        self.assertEqual(len(self.edge.creates), 2)
        self.assertTrue(all(x[1] == 'delivered' and x[2] is not None
                            and x[3] is None and 'thread creation failed' in x[5]
                            for x in self.root_rows()))
        self.run_drain()
        self.assertEqual(len(self.edge.creates), 2)

    def test_retained_post_mutations_continue_after_unsubscribe(self):
        self.inbound.reserve(self.event())
        self.run_drain()
        original = {(x[0], x[2]) for x in self.root_rows()}
        with self.db.immediate_session() as session:
            for follow in session.scalars(select(RemotePersonFollow)):
                follow.relationship_status = 'cancel_requested'
        self.inbound.reserve(self.event('updated', when=self.when + timedelta(minutes=1)))
        self.run_drain()
        self.assertEqual(len(self.edge.edits), 2)
        self.assertEqual({(id - 41, msg) for id, msg, _ in self.edge.edits}, original)
        self.inbound.reserve(self.event('deleted', when=self.when + timedelta(minutes=2)))
        self.run_drain()
        self.assertEqual(len(self.edge.deletes), 2)
        self.assertEqual(len(self.edge.creates), 2)

    def test_restart_recovers_claimed_and_marks_sending_uncertain(self):
        """A restart only retries work that never crossed the send boundary."""
        self.inbound.reserve(self.event())
        with self.db.immediate_session() as session:
            rows = list(session.scalars(select(RemotePersonPostDelivery).order_by(
                RemotePersonPostDelivery.discord_channel_id)))
            rows[0].state = 'claimed'
            rows[1].state = 'sending'
        async def restart():
            """Exercise real task lifecycle rather than altering an internal helper."""
            self.worker.start()
            await self.worker.stop()
        asyncio.run(restart())
        self.assertEqual({x[0]: x[1] for x in self.root_rows()},
                         {100: 'pending', 101: 'uncertain'})

    def test_adapter_posts_wait_true_without_mentions_and_creates_thread(self):
        """A webhook root preserves remote identity and bot-created thread scope."""
        sent = []
        class Hook:
            """Capture the Discord SDK edge's public webhook request."""
            id = 40
            async def send(self, **kwargs):
                """Return the confirmed message ID from a wait=true webhook call."""
                sent.append(kwargs)
                return SimpleNamespace(id=100100)
        class Message:
            """Simulate the exact root message returned to the bot."""
            async def create_thread(self, **kwargs):
                """Match Discord's documented text-channel root-thread identity."""
                return SimpleNamespace(id=100100)
        class Channel:
            """Expose bot permissions and root fetch without remote network."""
            type = 0
            guild = SimpleNamespace(me=object())
            def permissions_for(self, _member):
                """Permit all required root and thread operations."""
                from src.remote_person_feeds import REQUIRED_BOT_PERMISSIONS
                return SimpleNamespace(**{permission: True for permission in REQUIRED_BOT_PERMISSIONS})
            async def fetch_message(self, _message_id):
                """Resolve the root under its own text channel."""
                return Message()
        channel = Channel()
        bot = SimpleNamespace(get_channel=lambda _id: channel)
        transport = RemotePersonWebhookTransport(bot)
        self.inbound.reserve(self.event())
        with self.db.session() as session:
            root = session.scalar(select(RemotePersonPost))
            post = SimpleNamespace(remote_actor_id=root.remote_actor_id,
                remote_handle='bob', remote_avatar_url='https://remote.example/avatar.png',
                source_activity_json=root.source_activity_json, content_markdown=root.content_markdown,
                object_id=root.object_id)
        fake_discord = SimpleNamespace(AllowedMentions=SimpleNamespace(none=lambda: 'none'))
        async def exercise():
            """Pass through the real adapter with a fake outbound SDK boundary."""
            with patch.dict('sys.modules', {'discord': fake_discord}):
                message_id = await transport.create(PreparedRemoteWebhook(channel, Hook()), post)
                thread_id = await transport.create_thread(100, message_id, post)
                return thread_id
        self.assertEqual(asyncio.run(exercise()), 100100)
        self.assertEqual(sent[0]['username'], 'bob@remote.example')
        self.assertEqual(sent[0]['avatar_url'], 'https://remote.example/avatar.png')
        self.assertIs(sent[0]['wait'], True)
        self.assertEqual(sent[0]['allowed_mentions'], 'none')

    def test_content_render_bounds_source_and_media(self):
        self.inbound.reserve(self.event())
        with self.db.session() as session:
            post = session.scalar(select(RemotePersonPost))
            text = remote_root_content(post)
        self.assertTrue(text.endswith(OBJECT))
        self.assertIn('Hello from Bob', text)
        self.assertLessEqual(len(text), 2000)


    def test_last_owner_removal_suppresses_old_pending_even_after_new_owner_accepted(self):
        """A zero-subscriber interval terminates queued work before B can authorize it."""
        with self.db.immediate_session() as session:
            other = session.scalar(select(RemotePersonFollow).where(RemotePersonFollow.user_id == 2))
            other.relationship_status = 'pending'
        self.assertEqual(self.inbound.reserve(self.event()).target_count, 2)
        result = RemotePersonChannelService(self.db).remove_channel(
            discord_user_id='1', remote_actor_id=ACTOR,
            discord_guild_id=1, discord_channel_id=100)
        self.assertEqual(result.status, 'removed')
        with self.db.session() as session:
            original = session.scalar(select(RemotePersonPostDelivery).where(
                RemotePersonPostDelivery.discord_channel_id == 100))
            self.assertEqual(original.state, 'suppressed')
        with self.db.immediate_session() as session:
            other = session.scalar(select(RemotePersonFollow).where(RemotePersonFollow.user_id == 2))
            other.relationship_status = 'accepted'
        self.run_drain()
        self.assertEqual([x[0] for x in self.edge.creates], [101])
        # The fake edge's default ID only varies by channel. Distinct notes
        # must receive different Discord snowflakes within that channel.
        async def next_message(prepared, post):
            """Simulate the second root with a new Discord message identity."""
            self.edge.creates.append((post.channel_id, post.object_id, prepared.webhook_id))
            return 200000 + post.channel_id
        self.edge.create = next_message
        next_event = self.event(when=self.when + timedelta(minutes=1))
        next_event.source_activity_json['object']['id'] = 'https://remote.example/users/bob/statuses/8'
        next_event.object.ap_id = 'https://remote.example/users/bob/statuses/8'
        self.assertEqual(self.inbound.reserve(next_event).target_count, 2)
        self.run_drain()
        self.assertEqual({x[0] for x in self.edge.creates}, {100, 101})
        self.assertEqual(sum(x[0] == 100 for x in self.edge.creates), 1)

    def test_missing_webhook_delete_message_is_idempotent_and_does_not_retry(self):
        """Discord Unknown Message means the remote deletion's effect already holds."""
        self.inbound.reserve(self.event())
        self.run_drain()
        self.inbound.reserve(self.event('deleted', when=self.when + timedelta(minutes=2)))
        class UnknownMessage(Exception):
            """Discord's known 10008 result, not a generic HTTP or permission error."""
            code = 10008
        async def absent(_prepared, message_id):
            """Simulate a message removed by a Discord moderator before Delete."""
            self.edge.deletes.append(message_id)
            raise UnknownMessage('Unknown Message')
        self.edge.delete = absent
        self.run_drain()
        self.run_drain()
        self.assertEqual(len(self.edge.deletes), 2)
        self.assertTrue(all(x[4] == 'deleted' for x in self.root_rows()))

    def test_missing_webhook_credentials_preserve_other_channel_mutation(self):
        """One revoked original hook does not block mutations in other channels."""
        self.inbound.reserve(self.event())
        self.run_drain()
        self.inbound.reserve(self.event('updated', when=self.when + timedelta(minutes=2)))
        self.edge.fail_channel = 100
        self.run_drain()
        self.assertEqual({x[1] for x in self.edge.edits}, {100101})
        rows = {x[0]: x for x in self.root_rows()}
        self.assertIn('failed', rows[100][5])
        self.assertNotIn('failed', rows[101][5] or '')


    def test_missing_webhook_edit_message_has_terminal_diagnostic(self):
        """A deleted root cannot be edited, so a definite 10008 must stop retries."""
        self.inbound.reserve(self.event())
        self.run_drain()
        self.inbound.reserve(self.event('updated', when=self.when + timedelta(minutes=2)))
        class UnknownMessage(Exception):
            """Discord 10008 reports the exact original message is absent."""
            code = 10008
        async def absent(_prepared, message_id, _post):
            """Simulate an administrator removing the original root."""
            self.edge.edits.append(message_id)
            raise UnknownMessage('Unknown Message')
        self.edge.edit = absent
        self.run_drain()
        self.run_drain()
        self.assertEqual(len(self.edge.edits), 2)
        self.assertTrue(all(x[1] == 'failed' and 'removed' in x[5]
                            for x in self.root_rows()))

    def test_unrelated_webhook_404_is_not_treated_as_successful_delete(self):
        """Only Discord Unknown Message (10008) proves a deletion is complete."""
        self.inbound.reserve(self.event())
        self.run_drain()
        self.inbound.reserve(self.event('deleted', when=self.when + timedelta(minutes=2)))
        class UnknownChannel(Exception):
            """Discord 10003 is not proof that the specific root was deleted."""
            code = 10003
        async def missing_channel(_prepared, message_id):
            """Simulate an unavailable original destination."""
            self.edge.deletes.append(message_id)
            raise UnknownChannel('Unknown Channel')
        self.edge.delete = missing_channel
        self.run_drain()
        self.assertTrue(all(x[4] != 'deleted' and 'failed' in x[5]
                            for x in self.root_rows()))


if __name__ == '__main__':
    unittest.main()
