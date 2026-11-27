"""Cross-guild Follow assignment and global Personal Feed binding contracts.

These scenarios use the production SQLite admission/services with no Discord SDK
or network access, including the last-link Undo reservation boundary.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sqlalchemy import select

from src.db import Database
from src.models import (
    User, RemotePersonFollow, RemotePersonChannelAssignment, PersonalFeedBinding,
)
from src.personal_feeds import PersonalFeedService
from src.remote_person_feeds import RemotePersonChannelService, REQUIRED_BOT_PERMISSIONS
from types import SimpleNamespace

REMOTE = 'https://remote.example/users/bob'


class FeedGuildAssignmentContracts(unittest.TestCase):
    """Observe the actual serialized channel and ownership transitions."""

    def setUp(self) -> None:
        """Create registered users and one accepted Follow in a fresh v2 DB."""
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.db = Database(f"sqlite:///{Path(folder.name) / 'bridge.db'}")
        self.addCleanup(self.db.close)
        self.db.migrate()
        with self.db.session() as session:
            for who in ('alice', 'other'):
                actor = f'https://bridge.example/actors/users/{who}'
                user = User(
                    discord_user_id=who, activitypub_username=who, actor_url=actor,
                    inbox_url=f'{actor}/inbox', outbox_url=f'{actor}/outbox',
                    followers_url=f'{actor}/followers', public_key_pem='pub', private_key_pem='key',
                )
                session.add(user)
                session.flush()
                session.add(RemotePersonFollow(
                    user_id=user.id, remote_actor_id=REMOTE,
                    remote_inbox_url=f'{REMOTE}/inbox',
                    follow_activity_id=f'{actor}/follows/1', generation=1,
                    relationship_status='accepted', follow_dispatch_state='sent',
                ))
        self.personal = PersonalFeedService(self.db)
        self.channels = RemotePersonChannelService(self.db)
        self.perms = SimpleNamespace(**{flag: True for flag in REQUIRED_BOT_PERMISSIONS})

    def assign(self, user: str, guild: int, channel: int):
        """Use the real authorization and serialized assignment service."""
        return self.channels.add_channel(
            discord_user_id=user, remote_actor_id=REMOTE,
            discord_guild_id=guild, discord_channel_id=channel,
            target_channel_guild_id=guild, channel_type='text',
            caller_can_manage_guild=True, bot_permissions=self.perms,
        )

    def test_personal_binding_is_global_and_explicit_move_is_atomic(self) -> None:
        """A move changes one binding without stealing a foreign channel."""
        first = self.personal.bind_or_move('alice', 1, 101)
        self.assertEqual(first.status, 'bound')
        # A Remote Person feed may share a guild with other Remote Person
        # feeds, but never claim the exclusive Personal Feed channel.
        self.assertEqual(self.assign('other', 1, 101).status, 'channel_conflict')
        moved = self.personal.bind_or_move(
            'alice', 2, 201, expected_binding_id=first.binding.id)
        self.assertEqual(moved.status, 'moved')
        self.assertEqual(moved.binding.id, first.binding.id)
        self.assertIsNone(self.personal.resolve_root_binding(1, 101))
        self.assertEqual(self.personal.resolve_root_binding(2, 201).user_id, first.binding.user_id)
        # A stale concurrent move cannot mutate the surviving binding.
        self.assertEqual(self.personal.bind_or_move(
            'alice', 3, 301, expected_binding_id=None).status, 'binding_changed')
        self.assertEqual(self.personal.bind_or_move('other', 2, 202).status, 'guild_conflict')
        with self.db.session() as session:
            self.assertEqual(session.query(PersonalFeedBinding).count(), 1)

    def test_competing_first_personal_binds_have_one_winner(self) -> None:
        """A stale concurrent first-bind cannot silently move into another guild."""
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        gate = Barrier(2)
        def bind(guild: int, channel: int):
            """Serialize two independently authorized commands at the SQLite writer."""
            gate.wait()
            return self.personal.bind_or_move(
                'alice', guild, channel, expected_binding_id=None).status
        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(bind, 1, 101)
            b = pool.submit(bind, 2, 201)
            self.assertEqual({a.result(), b.result()}, {'bound', 'binding_changed'})
        with self.db.session() as session:
            rows = session.scalars(select(PersonalFeedBinding)).all()
            self.assertEqual(len(rows), 1)
            self.assertIn((rows[0].discord_guild_id, rows[0].discord_channel_id),
                          {(1, 101), (2, 201)})

    def test_cross_guild_remove_does_not_undo_until_last_link(self) -> None:
        """Only the final assignment requests a single Undo for this Follow."""
        first = self.assign('alice', 1, 101)
        self.assertEqual(first.status, 'assigned')
        self.assertEqual(self.assign('alice', 2, 201).status, 'assigned')
        # Another person's independent Follow may share destination channel.
        self.assertEqual(self.assign('other', 1, 101).status, 'assigned')
        result = self.channels.remove_channel(
            discord_user_id='alice', remote_actor_id=REMOTE,
            discord_guild_id=1, discord_channel_id=101)
        self.assertEqual(result.status, 'removed')
        with self.db.session() as session:
            f = session.get(RemotePersonFollow, first.follow_id)
            self.assertEqual((f.relationship_status, f.undo_dispatch_state),
                             ('accepted', 'not_requested'))
            self.assertEqual(len(session.scalars(select(RemotePersonChannelAssignment).where(
                RemotePersonChannelAssignment.follow_id == first.follow_id)).all()), 1)
        self.assertEqual(self.channels.remove_channel(
            discord_user_id='alice', remote_actor_id=REMOTE,
            discord_guild_id=2, discord_channel_id=201).status, 'removed')
        self.assertEqual(self.channels.remove_channel(
            discord_user_id='alice', remote_actor_id=REMOTE,
            discord_guild_id=2, discord_channel_id=201).status, 'not_assigned')
        with self.db.session() as session:
            own = session.get(RemotePersonFollow, first.follow_id)
            other = session.scalar(select(RemotePersonFollow).where(
                RemotePersonFollow.id != first.follow_id))
            self.assertEqual((own.relationship_status, own.undo_dispatch_state),
                             ('cancel_requested', 'prepared'))
            self.assertEqual((other.relationship_status, other.undo_dispatch_state),
                             ('accepted', 'not_requested'))
            self.assertEqual(session.query(RemotePersonChannelAssignment).count(), 1)


if __name__ == '__main__':
    unittest.main()
