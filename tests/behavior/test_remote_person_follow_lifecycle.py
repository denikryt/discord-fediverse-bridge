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
        self.service = RemotePersonFollowService(self.db, self.gateway, 'https://bridge.test',
            SimpleNamespace(is_discord_guild_allowed=lambda _guild: True,
                federation_decision=lambda _url: SimpleNamespace(allowed=True)))
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

    def test_repeated_prepared_follow_targets_its_own_activity_after_key_recovery(self):
        """Repeating B cannot dispatch older prepared A through a global limit=1 scan."""
        a = self.subscribe()
        b = self.subscribe('charlie', 101)
        same_b = self.subscribe('charlie', 101)
        self.assertEqual((same_b.status, same_b.follow_id, same_b.follow_activity_id),
                         ('already_assigned', b.follow_id, b.follow_activity_id))
        with self.db.session() as session:
            generation = session.get(RemotePersonFollow, b.follow_id).generation
        def missing_key(request):
            """The gateway rejects signing before any network submission."""
            if request.follow_activity_id == b.follow_activity_id:
                raise FollowNotDispatchedError('signing key unavailable')
        self.gateway.on_send = missing_key
        self.assertEqual(asyncio.run(self.service.dispatch_prepared_for(
            follow_id=same_b.follow_id, follow_activity_id=same_b.follow_activity_id)), 0)
        self.assertEqual(self.status(b.follow_id)[1], 'prepared')
        self.gateway.on_send = None
        self.assertEqual(asyncio.run(self.service.dispatch_prepared_for(
            follow_id=same_b.follow_id, follow_activity_id=same_b.follow_activity_id)), 1)
        self.assertEqual([x.follow_activity_id for x in self.gateway.sends],
                         [b.follow_activity_id, b.follow_activity_id])
        self.assertEqual(self.status(a.follow_id)[1], 'prepared')
        self.assertEqual(self.status(b.follow_id)[1], 'sent')
        with self.db.session() as session:
            row = session.get(RemotePersonFollow, b.follow_id)
            self.assertEqual((row.follow_activity_id, row.generation), (b.follow_activity_id, generation))
        self.assertEqual(asyncio.run(self.service.dispatch_prepared_for(
            follow_id=b.follow_id, follow_activity_id=b.follow_activity_id)), 0)
        self.assertEqual(len(self.gateway.sends), 2)

    def test_targeted_follow_rejects_old_generation_and_concurrent_repeats(self):
        """Only one guarded prepared generation can enter the outbound network."""
        old = self.subscribe()
        self.assertEqual(self.service.cancel_follow(
            discord_user_id='alice', remote_actor_id=ACTOR), 'cancel_requested')
        self.assertEqual(asyncio.run(self.service.dispatch_prepared_for(
            follow_id=old.follow_id, follow_activity_id=old.follow_activity_id)), 0)
        # The existing Undo path settles a never-sent Follow locally before
        # permitting the next generation to reserve the same Person.
        self.assertEqual(asyncio.run(self.service.dispatch_cancellations()), 0)
        fresh = self.subscribe()
        self.assertNotEqual(old.follow_activity_id, fresh.follow_activity_id)
        self.assertEqual(asyncio.run(self.service.dispatch_prepared_for(
            follow_id=old.follow_id, follow_activity_id=old.follow_activity_id)), 0)
        self.assertEqual(asyncio.run(self.service.dispatch_prepared_for(
            follow_id=fresh.follow_id, follow_activity_id=old.follow_activity_id)), 0)
        async def repeat_twice():
            """Hold A at the real network await while B checks durable sending."""
            started, release = asyncio.Event(), asyncio.Event()
            async def slow_gateway(request):
                """Simulate an in-flight Follow POST after the SQLite claim commits."""
                self.gateway.sends.append(request)
                started.set()
                await release.wait()
            self.gateway.send_prepared_person_follow = slow_gateway
            first = asyncio.create_task(self.service.dispatch_prepared_for(
                follow_id=fresh.follow_id, follow_activity_id=fresh.follow_activity_id))
            await started.wait()
            self.assertEqual(self.status(fresh.follow_id)[1], 'sending')
            second = await self.service.dispatch_prepared_for(
                follow_id=fresh.follow_id, follow_activity_id=fresh.follow_activity_id)
            release.set()
            return [await first, second]
        self.assertEqual(sorted(asyncio.run(repeat_twice())), [0, 1])
        self.assertEqual([x.follow_activity_id for x in self.gateway.sends], [fresh.follow_activity_id])

    def test_targeted_ambiguous_follow_never_sends_again(self):
        """A gateway timeout after network start forbids immediate same-ID retry."""
        pending = self.subscribe()
        def timeout(_request):
            """Model the lost response after a possibly accepted Follow."""
            raise TimeoutError('response lost after network transmission')
        self.gateway.on_send = timeout
        self.assertEqual(asyncio.run(self.service.dispatch_prepared_for(
            follow_id=pending.follow_id, follow_activity_id=pending.follow_activity_id)), 0)
        self.assertEqual(self.status(pending.follow_id)[1], 'uncertain')
        self.gateway.on_send = None
        self.assertEqual(asyncio.run(self.service.dispatch_prepared_for(
            follow_id=pending.follow_id, follow_activity_id=pending.follow_activity_id)), 0)
        self.assertEqual(len(self.gateway.sends), 1)

    def test_blocked_old_follow_does_not_starve_new_authorized_follow(self):
        """A denied prepared Follow must not stop an unrelated queued sender."""
        blocked = self.subscribe()
        other_actor = 'https://other.example/actors/carol'
        other_inbox = 'https://other.example/actors/carol/inbox'

        async def resolve(identifier):
            """Resolve a second actual remote Person for this guild."""
            if identifier == '@carol@other.example':
                return ResolvedRemotePerson(other_actor, other_inbox)
            return ResolvedRemotePerson(ACTOR, INBOX)

        self.gateway.resolve_remote_person = resolve
        other = asyncio.run(self.service.follow(
            discord_user_id='charlie', actor='@carol@other.example',
            discord_guild_id=1, discord_channel_id=101,
            target_channel_guild_id=1, channel_type='text',
            caller_can_manage_guild=True, bot_permissions=self.flags,
        ))
        self.assertEqual(other.status, 'prepared')
        deny_old = True

        def decide(url):
            """Apply a domain prohibition after both identities are reserved."""
            return SimpleNamespace(allowed=not (deny_old and url in (ACTOR, INBOX)))

        self.service.policy_service = SimpleNamespace(
            is_discord_guild_allowed=lambda _guild: True,
            federation_decision=decide,
        )
        # Even a one-send budget must progress past a denied, older row.
        self.assertEqual(asyncio.run(self.service.dispatch_prepared(limit=1)), 1)
        self.assertEqual([item.follow_activity_id for item in self.gateway.sends],
                         [other.follow_activity_id])
        self.assertEqual(self.status(blocked.follow_id)[1], 'prepared')
        with self.db.session() as session:
            original = session.get(RemotePersonFollow, blocked.follow_id)
            self.assertEqual(original.follow_activity_id, blocked.follow_activity_id)
            self.assertIn('policy denied', original.last_error)
        self.assertEqual(asyncio.run(self.service.dispatch_prepared()), 0)
        deny_old = False
        self.assertEqual(asyncio.run(self.service.dispatch_prepared()), 1)
        self.assertEqual([item.follow_activity_id for item in self.gateway.sends],
                         [other.follow_activity_id, blocked.follow_activity_id])
        self.assertEqual(self.status(blocked.follow_id)[1], 'sent')
        self.assertEqual(asyncio.run(self.service.dispatch_prepared()), 0)

    def test_proved_unsent_follow_does_not_block_another_sender(self):
        """A gateway preflight failure remains retryable without starving peers."""
        first = self.subscribe()
        second = self.subscribe('charlie', 101)

        def before_send(request):
            """Fail only the first Person before gateway network submission."""
            if request.follow_activity_id == first.follow_activity_id:
                raise FollowNotDispatchedError('gateway rejected before sending')

        self.gateway.on_send = before_send
        self.assertEqual(asyncio.run(self.service.dispatch_prepared(limit=2)), 1)
        self.assertEqual(self.status(first.follow_id)[1], 'prepared')
        self.assertEqual(self.status(second.follow_id)[1], 'sent')
        self.assertEqual([x.follow_activity_id for x in self.gateway.sends],
                         [first.follow_activity_id, second.follow_activity_id])
        self.gateway.on_send = None
        self.assertEqual(asyncio.run(self.service.dispatch_prepared(limit=1)), 1)
        self.assertEqual(self.gateway.sends[-1].follow_activity_id, first.follow_activity_id)

    def test_proved_unsent_follow_does_not_exhaust_single_send_budget(self):
        """Three command-sized drains must reach B despite A's persistent pre-send error."""
        first = self.subscribe()
        second = self.subscribe('charlie', 101)
        blocked = True

        def before_send(request):
            """Simulate gateway rejecting A before any outbound Follow request."""
            if blocked and request.follow_activity_id == first.follow_activity_id:
                raise FollowNotDispatchedError('gateway rejected before network')

        self.gateway.on_send = before_send
        # /person-feed follow uses limit=1; A may not consume that network
        # budget because the gateway proves the network was never contacted.
        self.assertEqual([
            asyncio.run(self.service.dispatch_prepared(limit=1)) for _ in range(3)
        ], [1, 0, 0])
        self.assertEqual(self.status(first.follow_id)[1], 'prepared')
        self.assertEqual(self.status(second.follow_id)[1], 'sent')
        self.assertEqual([
            item.follow_activity_id for item in self.gateway.sends
        ], [first.follow_activity_id, second.follow_activity_id,
            first.follow_activity_id, first.follow_activity_id])
        with self.db.session() as session:
            row = session.get(RemotePersonFollow, first.follow_id)
            self.assertEqual(row.follow_activity_id, first.follow_activity_id)
            self.assertIn('before network', row.last_error)
            self.assertEqual(len(session.scalars(select(RemotePersonFollow)).all()), 2)

        # Removing the gateway preflight failure permits the original A
        # generation to send, without replaying B or minting a second ID.
        blocked = False
        self.assertEqual(asyncio.run(self.service.dispatch_prepared(limit=1)), 1)
        self.assertEqual(self.gateway.sends[-1].follow_activity_id, first.follow_activity_id)
        self.assertEqual(self.status(first.follow_id)[1], 'sent')
        self.assertEqual(asyncio.run(self.service.dispatch_prepared(limit=1)), 0)

    def test_ambiguous_follow_consumes_one_send_budget_and_is_not_retried(self):
        """An unknown network outcome counts against limit=1, unlike pre-send failure."""
        first = self.subscribe()
        second = self.subscribe('charlie', 101)

        def before_return(request):
            """Simulate an HTTP timeout after A may have reached its inbox."""
            if request.follow_activity_id == first.follow_activity_id:
                raise TimeoutError('possibly transmitted')

        self.gateway.on_send = before_return
        self.assertEqual(asyncio.run(self.service.dispatch_prepared(limit=1)), 0)
        self.assertEqual(self.status(first.follow_id)[1], 'uncertain')
        self.assertEqual(self.status(second.follow_id)[1], 'prepared')
        self.assertEqual([item.follow_activity_id for item in self.gateway.sends],
                         [first.follow_activity_id])
        self.assertEqual(asyncio.run(self.service.dispatch_prepared(limit=1)), 1)
        self.assertEqual(self.status(second.follow_id)[1], 'sent')
        self.assertEqual([item.follow_activity_id for item in self.gateway.sends],
                         [first.follow_activity_id, second.follow_activity_id])
        # The ambiguous original Follow is not repeated on later drains.
        self.assertEqual(asyncio.run(self.service.dispatch_prepared(limit=1)), 0)
        self.assertEqual(len(self.gateway.sends), 2)

    def test_fast_accept_during_sending_survives_sender_completion(self):
        """A legitimate Accept wins over the subsequent HTTP completion write."""
        pending = self.subscribe()
        self.gateway.on_send = lambda p: self.assertEqual(
            self.service.apply_remote_decision(p.follow_activity_id, ACTOR, 'accepted'), 'applied')
        self.assertEqual(asyncio.run(self.service.dispatch_prepared()), 1)
        self.assertEqual(self.status(pending.follow_id), ('accepted', 'sent', 'not_requested'))
        self.assertEqual(self.service.apply_remote_decision(pending.follow_activity_id, ACTOR, 'accepted'), 'duplicate')

    def test_follow_response_to_other_users_inbox_does_not_accept(self):
        """A signed remote response cannot take effect through a foreign local inbox."""
        alice = self.subscribe()
        asyncio.run(self.service.dispatch_prepared())
        wrong_inbox = 'https://bridge.test/actors/users/charlie'
        self.assertEqual(self.service.apply_remote_decision(
            alice.follow_activity_id, ACTOR, 'accepted',
            local_recipient_actor_id=wrong_inbox), 'unmatched')
        self.assertEqual(self.status(alice.follow_id)[0], 'pending')
        self.assertEqual(self.service.apply_remote_decision(
            alice.follow_activity_id, ACTOR, 'accepted',
            local_recipient_actor_id='https://bridge.test/actors/users/alice'), 'applied')

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
