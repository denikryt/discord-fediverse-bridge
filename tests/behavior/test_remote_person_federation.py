"""Runtime-scoped user Follow generation and dispatch behavior on migrated SQLite."""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import select

from src.db import Database
from src.models import User, RemotePersonFollow, RemotePersonChannelAssignment
from src.remote_person_follow_service import RemotePersonFollowService
from src.remote_person_feeds import REQUIRED_BOT_PERMISSIONS
from src.fedify_gateway_client import ResolvedRemotePerson, FollowNotDispatchedError

REMOTE = "https://fediverse.example/actors/bob"
INBOX = "https://fediverse.example/inbox/bob"


class GatewayFake:
    """Prove actor lookup and outbound gateway traffic are ordered after admission."""

    def __init__(self) -> None:
        """Retain deterministic transport observations rather than network mocks."""
        self.lookups = []
        self.sends = []
        self.failure = None

    async def resolve_remote_person(self, identifier):
        """Accept the prescribed handle and return verified canonical actor."""
        self.lookups.append(identifier)
        return ResolvedRemotePerson(REMOTE, INBOX)

    async def send_prepared_person_follow(self, prepared):
        """Capture signed-dispatch request; optionally emulate uncertain HTTP."""
        self.sends.append(prepared)
        if self.failure is not None:
            raise self.failure


class UserFollowScenarios(unittest.TestCase):
    """Exercise durable per-owner Follow identity and denied side effects."""

    def setUp(self):
        """Start real v2 schema with two distinct registered user identities."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Database(f"sqlite:///{Path(tmp.name) / 'bridge.sqlite'}")
        self.addCleanup(self.db.close)
        self.db.migrate()
        with self.db.session() as session:
            for id_, username in ((101, 'alice'), (102, 'charlie')):
                actor = f'https://bridge.test/actors/users/{username}'
                session.add(User(discord_user_id=str(id_), activitypub_username=username,
                    actor_url=actor, inbox_url=f'{actor}/inbox', outbox_url=f'{actor}/outbox',
                    followers_url=f'{actor}/followers', public_key_pem='public', private_key_pem='private'))
        self.gateway = GatewayFake()
        self.service = RemotePersonFollowService(self.db, self.gateway, 'https://bridge.test',
            SimpleNamespace(is_discord_guild_allowed=lambda _guild: True,
                federation_decision=lambda _url: SimpleNamespace(allowed=True)))
        self.flags = SimpleNamespace(**{name: True for name in REQUIRED_BOT_PERMISSIONS})

    async def start(self, user=101, actor='@bob@fediverse.example', channel=10, **params):
        """Request Follow and first channel through the real service."""
        return await self.service.follow(discord_user_id=str(user), actor=actor,
            discord_guild_id=params.get('guild', 1), discord_channel_id=channel,
            target_channel_guild_id=params.get('guild', 1),
            channel_type='text', caller_can_manage_guild=params.get('manager', True),
            bot_permissions=params.get('permissions', self.flags))

    def test_federation_blocklist_rejects_before_lookup_and_follow(self):
        """Denied remote actors cannot reserve or dispatch a user-owned Follow."""
        self.service.policy_service.federation_decision = lambda _url: SimpleNamespace(allowed=False)
        result = asyncio.run(self.start())
        self.assertEqual(result.status, 'federation_denied')
        self.assertEqual(self.gateway.lookups, [])
        self.assertEqual(asyncio.run(self.service.dispatch_prepared()), 0)
        self.assertEqual(self.gateway.sends, [])
        with self.db.session() as session:
            self.assertIsNone(session.scalar(select(RemotePersonFollow.id)))

    def test_policy_revocation_blocks_queued_follow_but_retains_its_identity(self):
        """A queued Follow cannot bypass a later blocklist at the send gate."""
        prepared = asyncio.run(self.start())
        self.assertEqual(prepared.status, 'prepared')
        self.service.policy_service.federation_decision = lambda _url: SimpleNamespace(allowed=False)
        self.assertEqual(asyncio.run(self.service.dispatch_prepared()), 0)
        self.assertEqual(self.gateway.sends, [])
        with self.db.session() as session:
            row = session.get(RemotePersonFollow, prepared.follow_id)
            self.assertEqual(row.follow_dispatch_state, 'prepared')
            self.assertEqual(row.follow_activity_id, prepared.follow_activity_id)
        self.service.policy_service.federation_decision = lambda _url: SimpleNamespace(allowed=True)
        self.assertEqual(asyncio.run(self.service.dispatch_prepared()), 1)
        self.assertEqual(len(self.gateway.sends), 1)

    def test_two_local_actors_get_distinct_immutable_follow_ids(self):
        """Follow identity belongs to the registered sender, not the shared service."""
        one = asyncio.run(self.start())
        two = asyncio.run(self.start(user=102))
        self.assertEqual((one.status, two.status), ('prepared', 'prepared'))
        self.assertNotEqual(one.follow_activity_id, two.follow_activity_id)
        self.assertIn('/actors/users/alice/activities/follow/', one.follow_activity_id)
        self.assertIn('/actors/users/charlie/activities/follow/', two.follow_activity_id)
        with self.db.session() as session:
            self.assertEqual(session.query(RemotePersonFollow).count(), 2)
            self.assertEqual(session.query(RemotePersonChannelAssignment).count(), 2)
        self.assertEqual(len(self.gateway.sends), 0)
        self.assertEqual(asyncio.run(self.service.dispatch_prepared()), 2)
        self.assertEqual({send.follow_activity_id for send in self.gateway.sends},
            {one.follow_activity_id, two.follow_activity_id})
        self.assertEqual({send.actor_username for send in self.gateway.sends}, {'alice', 'charlie'})
        self.assertEqual(asyncio.run(self.service.dispatch_prepared()), 0)

    def test_denied_channel_has_no_lookup_or_follow_or_assignment(self):
        """Discord admission guards run before WebFinger and before SQLite writes."""
        denied = asyncio.run(self.start(manager=False))
        self.assertEqual(denied.status, 'forbidden')
        self.assertEqual(self.gateway.lookups, [])
        flags = SimpleNamespace(**{name: name != 'read_message_history' for name in REQUIRED_BOT_PERMISSIONS})
        denied = asyncio.run(self.start(permissions=flags))
        self.assertEqual(denied.status, 'missing_bot_permissions')
        self.assertEqual(self.gateway.lookups, [])
        with self.db.session() as session:
            self.assertEqual(session.query(RemotePersonFollow).count(), 0)

    def test_duplicate_owner_reuses_original_follow_without_network_replay(self):
        """A different guild reuses the same Follow and its ActivityPub identity."""
        one = asyncio.run(self.start())
        second = asyncio.run(self.start(channel=11, guild=2))
        self.assertEqual(second.follow_activity_id, one.follow_activity_id)
        self.assertEqual(second.status, 'assigned_existing')
        self.assertEqual(len(self.gateway.lookups), 2)
        self.assertEqual(asyncio.run(self.service.dispatch_prepared()), 1)
        with self.db.session() as session:
            assignments = session.query(RemotePersonChannelAssignment).all()
            self.assertEqual({(row.discord_guild_id, row.discord_channel_id)
                              for row in assignments}, {(1, 10), (2, 11)})

    def test_unknown_network_send_is_not_blindly_repeated(self):
        """Ambiguous POST outcome keeps the exact Follow ID in uncertain state."""
        one = asyncio.run(self.start())
        self.gateway.failure = TimeoutError('maybe delivered')
        self.assertEqual(asyncio.run(self.service.dispatch_prepared()), 0)
        self.assertEqual(len(self.gateway.sends), 1)
        with self.db.session() as session:
            follow = session.scalar(select(RemotePersonFollow))
            self.assertEqual(follow.follow_dispatch_state, 'uncertain')
            self.assertEqual(follow.follow_activity_id, one.follow_activity_id)
            self.assertEqual(follow.relationship_status, 'pending')
        self.gateway.failure = None
        self.assertEqual(asyncio.run(self.service.dispatch_prepared()), 0)
        self.assertEqual(len(self.gateway.sends), 1)

    def test_remote_lookup_must_return_safe_canonical_https_identity(self):
        """Reject local/private actor or inbox before writing a Follow generation."""
        for actor, inbox in (
            ('https://127.0.0.1/users/a', INBOX),
            (REMOTE, 'http://fediverse.example/inbox/bob'),
            (REMOTE, 'https://192.168.0.10/inbox'),
            ('https://bridge.test/actors/users/alice', INBOX),
        ):
            self.gateway.resolve_remote_person = self._resolver(actor, inbox)
            result = asyncio.run(self.start())
            self.assertEqual(result.status, 'invalid_actor')
        with self.db.session() as session:
            self.assertEqual(session.query(RemotePersonFollow).count(), 0)

    @staticmethod
    def _resolver(actor, inbox):
        """Emulate an untrusted gateway result for boundary rejection tests."""
        async def resolve(_identifier):
            """Return a mismatched or unsafe test actor."""
            return ResolvedRemotePerson(actor, inbox)
        return resolve

    def test_local_key_unavailable_releases_only_same_id_prepared_follow(self):
        """Proven non-dispatch may retry with the existing committed activity."""
        one = asyncio.run(self.start())
        self.gateway.failure = FollowNotDispatchedError('missing key')
        self.assertEqual(asyncio.run(self.service.dispatch_prepared()), 0)
        with self.db.session() as session:
            self.assertEqual(session.scalar(select(RemotePersonFollow)).follow_dispatch_state, 'prepared')
        self.gateway.failure = None
        self.assertEqual(asyncio.run(self.service.dispatch_prepared()), 1)
        self.assertEqual(self.gateway.sends[0].follow_activity_id, one.follow_activity_id)
        self.assertEqual(self.gateway.sends[1].follow_activity_id, one.follow_activity_id)


if __name__ == '__main__':
    unittest.main()
