"""Registered Person command lifecycle scenarios with a real SQLite writer.

Discord SDK policy adapters are exercised in the full suite when discord.py is
installed. These offline tests cover the command-facing ownership boundary.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from sqlalchemy import select

from src.db import Database
from src.models import User, RemotePersonFollow, RemotePersonChannelAssignment
from src.remote_person_follow_service import RemotePersonFollowService
from src.remote_person_feeds import RemotePersonChannelService, REQUIRED_BOT_PERMISSIONS
from src.fedify_gateway_client import ResolvedRemotePerson


class _Gateway:
    """Resolve a verified actor without network access."""

    async def resolve_remote_person(self, actor):
        """Return one stable trusted remote Person identity."""
        return ResolvedRemotePerson('https://remote.example/users/bob', 'https://remote.example/inbox')


class PersonCommandOwnershipScenarios(unittest.TestCase):
    """Exercise multiple owners and all-destination cancellation under one writer."""

    def setUp(self):
        """Prepare independent registered users with a v2 schema."""
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db = Database(f'sqlite:///{Path(temp.name) / "db.sqlite"}')
        self.addCleanup(self.db.close)
        self.db.migrate()
        with self.db.session() as session:
            for username in ('alice', 'bob'):
                actor = f'https://bridge.test/actors/users/{username}'
                session.add(User(discord_user_id=username, activitypub_username=username,
                    actor_url=actor, inbox_url=f'{actor}/inbox', outbox_url=f'{actor}/outbox',
                    followers_url=f'{actor}/followers', public_key_pem='pub', private_key_pem='priv'))
        self.follow = RemotePersonFollowService(self.db, _Gateway(), 'https://bridge.test',
            SimpleNamespace(is_discord_guild_allowed=lambda _guild: True,
                federation_decision=lambda _url: SimpleNamespace(allowed=True)))
        self.channel = RemotePersonChannelService(self.db)
        self.flags = SimpleNamespace(**{name: True for name in REQUIRED_BOT_PERMISSIONS})

    def subscribe(self, user, channel):
        """Perform the validated service path corresponding to /person-feed follow."""
        return asyncio.run(self.follow.follow(discord_user_id=user, actor='@bob@remote.example',
            discord_guild_id=1, discord_channel_id=channel, target_channel_guild_id=1,
            channel_type='text', caller_can_manage_guild=True, bot_permissions=self.flags))

    def test_unfollow_cancels_only_caller_across_multiple_channels(self):
        """A cancels their multi-channel Follow without cancelling B's Follow."""
        first = self.subscribe('alice', 100)
        self.assertEqual(self.subscribe('alice', 101).status, 'assigned_existing')
        other = self.subscribe('bob', 100)
        self.assertEqual(self.follow.cancel_follow(discord_user_id='alice',
            remote_actor_id='https://remote.example/users/bob'), 'cancel_requested')
        with self.db.session() as session:
            self.assertEqual(session.get(RemotePersonFollow, first.follow_id).relationship_status, 'cancel_requested')
            self.assertEqual(session.get(RemotePersonFollow, other.follow_id).relationship_status, 'pending')
            self.assertEqual(session.scalars(select(RemotePersonChannelAssignment).where(
                RemotePersonChannelAssignment.follow_id == first.follow_id)).all(), [])
            self.assertEqual(len(session.scalars(select(RemotePersonChannelAssignment).where(
                RemotePersonChannelAssignment.follow_id == other.follow_id)).all()), 1)

    def test_unfollow_wrong_owner_or_actor_is_no_op(self):
        """Neither unrelated users nor foreign actor IDs can remove a Follow."""
        original = self.subscribe('alice', 100)
        self.assertEqual(self.follow.cancel_follow(discord_user_id='bob',
            remote_actor_id='https://remote.example/users/bob'), 'not_following')
        self.assertEqual(self.follow.cancel_follow(discord_user_id='alice',
            remote_actor_id='https://remote.example/users/other'), 'not_following')
        with self.db.session() as session:
            self.assertEqual(session.get(RemotePersonFollow, original.follow_id).relationship_status, 'pending')

    def test_follow_without_manager_or_history_never_reserves(self):
        """Discord validation must occur before any remote lookup or write."""
        self.assertEqual(asyncio.run(self.follow.follow(discord_user_id='alice', actor='x',
            discord_guild_id=1, discord_channel_id=100, target_channel_guild_id=1,
            channel_type='text', caller_can_manage_guild=False, bot_permissions=self.flags)).status, 'forbidden')
        flags = SimpleNamespace(**{name: name != 'read_message_history' for name in REQUIRED_BOT_PERMISSIONS})
        result = asyncio.run(self.follow.follow(discord_user_id='alice', actor='x',
            discord_guild_id=1, discord_channel_id=100, target_channel_guild_id=1,
            channel_type='text', caller_can_manage_guild=True, bot_permissions=flags))
        self.assertEqual(result.missing_permissions, ('read_message_history',))
        with self.db.session() as session:
            self.assertIsNone(session.scalar(select(RemotePersonFollow.id)))

    def test_late_cancel_then_new_follow_uses_new_generation(self):
        """Re-enrollment never resurrects a cancelled old Follow identity."""
        first = self.subscribe('alice', 100)
        self.follow.cancel_follow(discord_user_id='alice', remote_actor_id='https://remote.example/users/bob')
        self.assertEqual(asyncio.run(self.follow.dispatch_cancellations()), 0)
        second = self.subscribe('alice', 100)
        self.assertNotEqual(first.follow_activity_id, second.follow_activity_id)
        self.assertNotEqual(first.follow_id, second.follow_id)


if __name__ == '__main__':
    unittest.main()
