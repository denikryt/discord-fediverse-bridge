"""Real SQLite Follow reply, cancellation and signed Undo lifecycle scenarios."""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import select

from src.db import Database
from src.models import RemotePersonFollow, RemotePersonChannelAssignment, User
from src.remote_person_follow_service import RemotePersonFollowService
from src.remote_person_feeds import RemotePersonChannelService, REQUIRED_BOT_PERMISSIONS
from src.fedify_gateway_client import ResolvedRemotePerson, FollowNotDispatchedError

ACTOR = 'https://remote.example/actors/bob'
INBOX = 'https://remote.example/users/bob/inbox'


class FakeRemoteGateway:
    """Control the await point so an Accept can race with Follow completion."""

    def __init__(self):
        """Track typed sender and original immutable follow/undo identifiers."""
        self.sends = []
        self.undos = []
        self.on_send = None
        self.undo_error = None

    async def resolve_remote_person(self, _identifier):
        """Return an already verified remote Person from the network edge."""
        return ResolvedRemotePerson(ACTOR, INBOX)

    async def send_prepared_person_follow(self, request):
        """Allow tests to inject a signed Accept before the send returns."""
        self.sends.append(request)
        if self.on_send is not None:
            self.on_send(request)

    async def send_prepared_person_undo(self, request):
        """Record a signed user-owned Undo request and optionally time out."""
        self.undos.append(request)
        if self.undo_error is not None:
            raise self.undo_error


class FollowLifecycleScenarios(unittest.TestCase):
    """Verify per-user, generation-scoped Follow/Undo state transitions."""

    def setUp(self):
        """Migrate one real SQLite DB with independent local Persons."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Database(f"sqlite:///{Path(tmp.name) / 'db.sqlite'}")
        self.addCleanup(self.db.close)
        self.db.migrate()
        with self.db.session() as session:
            for identity in ('alice', 'charlie'):
                actor = f'https://bridge.test/actors/users/{identity}'
                session.add(User(discord_user_id=identity, activitypub_username=identity,
                    actor_url=actor, inbox_url=f'{actor}/inbox', outbox_url=f'{actor}/outbox',
                    followers_url=f'{actor}/followers', public_key_pem='public', private_key_pem='private'))
        self.gateway = FakeRemoteGateway()
        self.service = RemotePersonFollowService(self.db, self.gateway, 'https://bridge.test')
        self.channels = RemotePersonChannelService(self.db)
        self.flags = SimpleNamespace(**{name: True for name in REQUIRED_BOT_PERMISSIONS})

    def subscribe(self, user='alice', channel=100):
        """Reserve one local Person Follow with a concrete guild destination."""
        return asyncio.run(self.service.follow(discord_user_id=user, actor='@bob@remote.example',
            discord_guild_id=1, discord_channel_id=channel, target_channel_guild_id=1,
            channel_type='text', caller_can_manage_guild=True, bot_permissions=self.flags))

    def remove(self, user='alice', channel=100):
        """Remove only one user's assignment with a serialized DB writer."""
        return self.channels.remove_channel(discord_user_id=user, remote_actor_id=ACTOR,
            discord_guild_id=1, discord_channel_id=channel)

    def status(self, follow_id):
        """Inspect orthogonal relationship, Follow and Undo transport states."""
        with self.db.session() as session:
            f = session.get(RemotePersonFollow, follow_id)
            return f.relationship_status, f.follow_dispatch_state, f.undo_dispatch_state

    def test_signed_inbox_event_handler_routes_user_follow_before_group(self):
        """The existing inbound handler must admit this local user's Follow ID."""
        from datetime import datetime, timezone
        from src.activitypub_models import FollowLifecycleEvent
        try:
            from src.activitypub_handlers import handle_follow_lifecycle
        except ModuleNotFoundError as error:
            if error.name == "discord":
                self.skipTest("discord.py integration dependency unavailable")
            raise
        pending = self.subscribe()
        asyncio.run(self.service.dispatch_prepared())
        event = FollowLifecycleEvent(
            event_type='follow.accepted', delivery_id=f'{ACTOR}/accept/1',
            occurred_at=datetime.now(timezone.utc), actor_id=ACTOR,
            community_actor_id=ACTOR,
            object={'follow_activity_id': pending.follow_activity_id},
        )
        runtime = SimpleNamespace(database=self.db, remote_person_follow_service=self.service)
        handled = asyncio.run(handle_follow_lifecycle(event, runtime))
        self.assertEqual(handled.status, 'processed')
        self.assertEqual(self.status(pending.follow_id)[0], 'accepted')
        forged = event.model_copy(update={'actor_id': 'https://attacker.example/user'})
        self.assertEqual(asyncio.run(handle_follow_lifecycle(forged, runtime)).status, 'skipped')

    def test_fast_accept_during_sending_survives_sender_completion(self):
        """A legitimate Accept wins over the subsequent HTTP completion write."""
        pending = self.subscribe()
        self.gateway.on_send = lambda p: self.assertEqual(
            self.service.apply_remote_decision(p.follow_activity_id, ACTOR, 'accepted'), 'applied')
        self.assertEqual(asyncio.run(self.service.dispatch_prepared()), 1)
        self.assertEqual(self.status(pending.follow_id), ('accepted', 'sent', 'not_requested'))
        self.assertEqual(self.service.apply_remote_decision(pending.follow_activity_id, ACTOR, 'accepted'), 'duplicate')

    def test_fast_reject_and_wrong_sender_are_isolated(self):
        """Only signed sender matching the committed original Follow can decide."""
        pending = self.subscribe()
        self.assertEqual(self.service.apply_remote_decision(pending.follow_activity_id,
            'https://other.example/users/mallory', 'accepted'), 'unmatched')
        self.gateway.on_send = lambda p: self.assertEqual(
            self.service.apply_remote_decision(p.follow_activity_id, ACTOR, 'rejected'), 'applied')
        asyncio.run(self.service.dispatch_prepared())
        self.assertEqual(self.status(pending.follow_id), ('rejected', 'sent', 'not_requested'))
        self.assertEqual(self.service.apply_remote_decision(pending.follow_activity_id, ACTOR, 'accepted'), 'unmatched')

    def test_remove_unsent_follow_requires_no_undo(self):
        """Cancel a prepared Follow without contacting the remote Person."""
        pending = self.subscribe()
        self.assertEqual(self.remove().status, 'removed')
        self.assertEqual(asyncio.run(self.service.dispatch_cancellations()), 0)
        self.assertEqual(self.status(pending.follow_id), ('cancelled', 'prepared', 'not_requested'))
        self.assertEqual(self.gateway.undos, [])
        self.assertEqual(asyncio.run(self.service.dispatch_prepared()), 0)

    def test_sent_follow_undo_uses_immutable_user_identity(self):
        """Removing final channel sends one signed Undo for this user's Follow."""
        pending = self.subscribe()
        self.assertEqual(asyncio.run(self.service.dispatch_prepared()), 1)
        self.remove()
        self.assertEqual(asyncio.run(self.service.dispatch_cancellations()), 1)
        self.assertEqual(self.status(pending.follow_id), ('cancelled', 'sent', 'sent'))
        undo = self.gateway.undos[0]
        self.assertEqual(undo.follow_activity_id, pending.follow_activity_id)
        self.assertEqual(undo.actor_username, 'alice')
        self.assertEqual(asyncio.run(self.service.dispatch_cancellations()), 0)
        self.assertEqual(self.service.apply_remote_decision(pending.follow_activity_id, ACTOR, 'accepted'), 'unmatched')

    def test_two_owner_follows_cannot_cancel_each_other(self):
        """Undo targets only the departing Person's Follow generation."""
        alice = self.subscribe()
        charlie = self.subscribe('charlie')
        asyncio.run(self.service.dispatch_prepared())
        self.service.apply_remote_decision(alice.follow_activity_id, ACTOR, 'accepted')
        self.service.apply_remote_decision(charlie.follow_activity_id, ACTOR, 'accepted')
        self.remove()
        asyncio.run(self.service.dispatch_cancellations())
        self.assertEqual(self.status(alice.follow_id)[0], 'cancelled')
        self.assertEqual(self.status(charlie.follow_id)[0], 'accepted')
        self.assertEqual([x.actor_username for x in self.gateway.undos], ['alice'])

    def test_undo_timeout_remains_uncertain_and_does_not_duplicate(self):
        """After a potentially delivered Undo, do not resend on restart."""
        f = self.subscribe()
        asyncio.run(self.service.dispatch_prepared())
        self.remove()
        self.gateway.undo_error = TimeoutError('HTTP unknown')
        self.assertEqual(asyncio.run(self.service.dispatch_cancellations()), 0)
        self.assertEqual(self.status(f.follow_id), ('cancel_requested', 'sent', 'uncertain'))
        self.gateway.undo_error = None
        self.assertEqual(asyncio.run(self.service.dispatch_cancellations()), 0)
        self.assertEqual(len(self.gateway.undos), 1)

    def test_accept_from_old_generation_cannot_claim_resubscribe(self):
        """Late replies have no effect after the original Follow was undone."""
        old = self.subscribe()
        asyncio.run(self.service.dispatch_prepared())
        self.remove()
        asyncio.run(self.service.dispatch_cancellations())
        newer = self.subscribe()
        self.assertNotEqual(old.follow_activity_id, newer.follow_activity_id)
        self.assertEqual(self.service.apply_remote_decision(old.follow_activity_id, ACTOR, 'accepted'), 'unmatched')
        self.assertEqual(self.status(newer.follow_id)[0], 'pending')

    def test_retain_follow_until_last_of_two_channels_removed(self):
        """Removing one of two channels never cancels a still active Follow."""
        one = self.subscribe()
        self.subscribe(channel=101)
        asyncio.run(self.service.dispatch_prepared())
        self.remove(channel=100)
        self.assertEqual(asyncio.run(self.service.dispatch_cancellations()), 0)
        self.assertEqual(self.status(one.follow_id)[0], 'pending')
        self.remove(channel=101)
        self.assertEqual(asyncio.run(self.service.dispatch_cancellations()), 1)


if __name__ == '__main__':
    unittest.main()
