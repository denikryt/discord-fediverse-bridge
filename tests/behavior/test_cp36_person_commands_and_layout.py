"""Plan 146 fit-first feed layout regressions; Person commands follow in CP44."""
from __future__ import annotations

import unittest

from src.formatting import (DISCORD_MESSAGE_LIMIT, feed_comment_content,
                            render_feed_discord_message)


OBJECT = 'https://peer.example/notes/42'
ACTOR = 'https://peer.example/users/bob'


def _units(message: str) -> int:
    """Assert the same UTF-16 limit Discord applies to sent content."""
    return len(message.encode('utf-16-le')) // 2


class FeedFitFirstLayoutTests(unittest.TestCase):
    """A fitting source URL is kept whole even after the original body cutoff."""

    def _renderers(self):
        """Root footer and comment marker use the same optional body budget."""
        return (
            lambda body: render_feed_discord_message(header='', body=body, footer=OBJECT),
            lambda body: feed_comment_content(actor_name='bob', actor_id=ACTOR,
                                              object_id=OBJECT, body=body),
        )

    def test_crossing_url_is_retained_by_middle_omission(self) -> None:
        """Unimportant prefix text yields space to one complete late link."""
        formats = (
            'https://reference.example/a(b)c',
            '<https://reference.example/d>',
            '[full multi word label](https://reference.example/a(b)c)',
            r'[see \] this label](https://reference.example/e)',
            '[description](<https://reference.example/f>)',
        )
        for link in formats:
            for prefix in (1900, 1970, 2020):
                for render in self._renderers():
                    with self.subTest(link=link, prefix=prefix, renderer=render):
                        result = render('x' * prefix + ' ' + link + ' tail')
                        self.assertLessEqual(_units(result), DISCORD_MESSAGE_LIMIT)
                        self.assertIn(link, result)
                        if prefix >= 1970:
                            self.assertIn('…', result)
                        self.assertEqual(result.count(link), 1)
                        self.assertTrue(result.endswith(OBJECT) or
                                        result.startswith(f'`bob@peer.example` [↗](<{OBJECT}>)'))

    def test_multiple_links_preserve_relative_order(self) -> None:
        """Preserve early complete links and their order without URL shortening."""
        links = ['https://reference.example/first',
                 '[read more](https://reference.example/second)',
                 'https://reference.example/third']
        body = 'a' * 1900 + ' ' + ' words '.join(links) + ' z' * 120
        for render in self._renderers():
            text = render(body)
            self.assertLessEqual(_units(text), DISCORD_MESSAGE_LIMIT)
            positions = [text.index(link) for link in links]
            self.assertEqual(positions, sorted(positions))
            self.assertEqual(text.count('https://reference.example/'), 3)

    def test_unfit_atom_is_dropped_not_fragmented(self) -> None:
        """A URL larger than optional capacity never becomes a damaged prefix."""
        huge = 'https://reference.example/' + 'z' * 2050
        for render in self._renderers():
            result = render('hello ' + huge + ' after')
            self.assertLessEqual(_units(result), DISCORD_MESSAGE_LIMIT)
            self.assertNotIn('https://reference.example/', result)
            self.assertIn('hello', result)
            self.assertIn('…', result)

    def test_empty_unicode_limits_and_mandatory_metadata(self) -> None:
        """Boundary lengths include surrogate pairs, header and footer."""
        for render in self._renderers():
            empty = render('')
            self.assertIn(OBJECT, empty)
            for size in (1999, 2000, 2001, 3000):
                result = render('🦊' * size)
                self.assertLessEqual(_units(result), DISCORD_MESSAGE_LIMIT)
                self.assertIn(OBJECT, result)
        with self.assertRaises(ValueError):
            render_feed_discord_message(header='', body='content')
        with self.assertRaises(ValueError):
            render_feed_discord_message(header='', body='content', footer='https://example.org/' + 'a' * 3000)


class OwnedPersonCommandIdentityTests(unittest.TestCase):
    """Real SQLite ownership gates canonicalize command handles before writes."""

    def setUp(self):
        """Create two local owners and one fake verified Gateway identity."""
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace
        from src.db import Database
        from src.models import User
        from src.remote_person_follow_service import RemotePersonFollowService
        from src.remote_person_feeds import RemotePersonChannelService, REQUIRED_BOT_PERMISSIONS
        from src.fedify_gateway_client import ResolvedRemotePerson
        self.actor = 'https://remote.example/actors/bob'
        self.inbox = 'https://remote.example/users/bob/inbox'
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(f"sqlite:///{Path(self.tmp.name) / 'test.sqlite'}")
        self.addCleanup(self.db.close)
        self.db.migrate()
        with self.db.session() as session:
            for user_id in ('alice', 'charlie'):
                actor = f'https://bridge.test/actors/users/{user_id}'
                session.add(User(discord_user_id=user_id, activitypub_username=user_id,
                    actor_url=actor, inbox_url=f'{actor}/inbox',
                    outbox_url=f'{actor}/outbox', followers_url=f'{actor}/followers',
                    public_key_pem='key', private_key_pem='key'))
        class Gateway:
            """Track network use and return a verified actor and inbox pair."""
            def __init__(self, fixture):
                self.fixture = fixture
                self.calls = []
                self.offline = False
            async def resolve_remote_person(self, identifier):
                self.calls.append(identifier)
                if self.offline:
                    raise TimeoutError('network unavailable')
                return ResolvedRemotePerson(self.fixture.actor, self.fixture.inbox)
        self.gateway = Gateway(self)
        self.policy_denied = False
        self.policy = SimpleNamespace(
            is_discord_guild_allowed=lambda _guild: True,
            federation_decision=lambda _url: SimpleNamespace(allowed=not self.policy_denied),
        )
        self.service = RemotePersonFollowService(self.db, self.gateway, 'https://bridge.test', self.policy)
        self.channels = RemotePersonChannelService(self.db)
        self.flags = SimpleNamespace(**{name: True for name in REQUIRED_BOT_PERMISSIONS})

    def _follow(self, user='alice', guild=1, channel=100, actor='@bob@remote.example'):
        """Reserve a real Follow with existing admission and ownership checks."""
        import asyncio
        return asyncio.run(self.service.follow(discord_user_id=user, actor=actor,
            discord_guild_id=guild, discord_channel_id=channel,
            target_channel_guild_id=guild, channel_type='text',
            caller_can_manage_guild=True, bot_permissions=self.flags))

    def _resolve(self, owner, identifier, *, policy=False):
        """Apply the exact command-to-service identity conversion."""
        import asyncio
        return asyncio.run(self.service.canonical_owned_actor(
            discord_user_id=owner, identifier=identifier, require_admission_policy=policy))

    def test_handle_and_exact_url_use_one_follow_with_offline_cancellation(self):
        """A handle and canonical URL own the same generation across guilds."""
        first = self._follow()
        self.assertEqual(first.status, 'prepared')
        self.assertEqual(self._resolve('alice', '@bob@remote.example'), self.actor)
        self.assertEqual(self._resolve('alice', self.actor), self.actor)
        self.assertEqual(self._resolve('charlie', '@bob@remote.example'), None)
        self.gateway.offline = True
        previous_calls = len(self.gateway.calls)
        self.assertEqual(self._resolve('alice', self.actor), self.actor)
        self.assertEqual(len(self.gateway.calls), previous_calls)
        self.assertIsNone(self._resolve('alice', '@bob@remote.example'))
        assigned = self.channels.add_channel(discord_user_id='alice', remote_actor_id=self.actor,
            discord_guild_id=2, discord_channel_id=200, target_channel_guild_id=2,
            channel_type='text', caller_can_manage_guild=True, bot_permissions=self.flags)
        self.assertEqual(assigned.status, 'assigned')
        self.assertEqual(self.service.cancel_follow(
            discord_user_id='alice', remote_actor_id=self._resolve('alice', self.actor)), 'cancel_requested')
        from sqlalchemy import select
        from src.models import RemotePersonFollow, RemotePersonChannelAssignment
        with self.db.session() as session:
            follow = session.get(RemotePersonFollow, first.follow_id)
            self.assertEqual(follow.relationship_status, 'cancel_requested')
            self.assertEqual(session.scalars(select(RemotePersonChannelAssignment)).all(), [])

    def test_denied_host_actor_mismatch_and_bad_url_do_not_change_assignments(self):
        """Only a verified, owned canonical Actor may cross an admission write."""
        self._follow()
        self.assertIsNone(self._resolve('charlie', self.actor))
        self.assertIsNone(self._resolve('alice', 'http://remote.example/actors/bob'))
        self.assertIsNone(self._resolve('alice', 'https://remote.example/actors/bob#other'))
        self.gateway.offline = False
        self.gateway.fixture = type('RemoteFixture', (), {
            'actor': 'https://attacker.example/actors/other',
            'inbox': 'https://attacker.example/inbox',
        })()
        self.assertIsNone(self._resolve('alice', '@bob@remote.example'))
        self.gateway.fixture = self
        self.policy_denied = True
        calls = len(self.gateway.calls)
        self.assertIsNone(self._resolve('alice', '@bob@remote.example'))
        self.assertEqual(len(self.gateway.calls), calls)
        # An already-owned URL remains removable when federation is denied,
        # but cannot be used to add another destination under blocked policy.
        self.assertEqual(self._resolve('alice', self.actor), self.actor)
        self.assertIsNone(self._resolve('alice', self.actor, policy=True))
        self.policy_denied = False
        from sqlalchemy import select
        from src.models import RemotePersonFollow, RemotePersonChannelAssignment
        with self.db.session() as session:
            self.assertEqual(len(session.scalars(select(RemotePersonFollow)).all()), 1)
            self.assertEqual(len(session.scalars(select(RemotePersonChannelAssignment)).all()), 1)


if __name__ == '__main__':
    unittest.main()
