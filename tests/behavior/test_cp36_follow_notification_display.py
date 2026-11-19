"""Actor-verified Follow display is optional; Follow and Accept identity are not."""
from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from src.db import Database
from src.activitypub_models import LocalFollowRequestObject
from src.user_actor_followers import UserActorFollowService

ACTOR = 'https://space.nachitima.com/uid/2'
TARGET = 'https://bridge.example/actors/users/alice'


class FollowNotificationDisplayTests(unittest.TestCase):
    """Use one persistent relationship and a fake external DM/Accept transport."""

    def setUp(self):
        """Initialize a real v3 database with one registered Person owner."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Database(f"sqlite:///{Path(tmp.name) / 'db.sqlite'}")
        self.addCleanup(self.db.close)
        self.db.migrate()
        self.user = self.db.users.create_user(
            discord_user_id='42', activitypub_username='alice',
            actor_url=TARGET, inbox_url=f'{TARGET}/inbox',
            outbox_url=f'{TARGET}/outbox', followers_url=f'{TARGET}/followers',
            public_key_pem='public', private_key_pem='private',
        )
        self.messages: list[str] = []
        self.accepts: list[str] = []
        messages = self.messages
        class User:
            """Simulate the user-owned Discord DM target."""
            async def send(self, text):
                """Capture only one actual notification attempt."""
                messages.append(text)
        self.discord_user = User()
        self.bot = SimpleNamespace(fetch_user=self._fetch_user)
        self.service = UserActorFollowService(
            database=self.db, fedify_gateway=SimpleNamespace(
                accept_user_actor_follow=self._accept), bot=self.bot,
        )

    async def _fetch_user(self, _user_id):
        """Return the one user whose relationship belongs to this DB."""
        return self.discord_user

    async def _accept(self, *, follow_activity_id, **_kwargs):
        """Capture Accept without changing the signed Follow id."""
        self.accepts.append(follow_activity_id)

    def _follow(self, index, name=None):
        """Invoke the normal follower service with a minimal HandlerResult port."""
        fake = ModuleType('src.activitypub_handlers')
        fake.HandlerResult = lambda status, outcome, detail: SimpleNamespace(
            status=status, outcome=outcome, detail=detail)
        with patch.dict(sys.modules, {'src.activitypub_handlers': fake}):
            return asyncio.run(self.service.handle_follow(
                target_actor_id=TARGET, remote_actor_id=ACTOR,
                remote_inbox_url='https://space.nachitima.com/inbox',
                remote_shared_inbox_url=None,
                follow_activity_id=f'https://space.nachitima.com/follows/{index}',
                remote_actor_display_name=name,
            ))

    def test_verified_label_and_duplicate_follow_have_one_dm(self):
        """Same Actor owns both Follow attempts; label affects only first DM."""
        first = self._follow(1, 'nachitima')
        second = self._follow(2, 'Some Later Name')
        self.assertEqual((first.status, second.status), ('processed', 'processed'))
        self.assertEqual(len(self.accepts), 2)
        self.assertEqual(len(self.messages), 1)
        self.assertIn('@nachitima@space.nachitima.com', self.messages[0])
        row = self.db.user_actor_followers.get_relationship(
            user_id=self.user.id, remote_actor_id=ACTOR)
        self.assertIsNotNone(row)
        self.assertEqual(row.follow_activity_id, self.accepts[-1])

    def test_absent_or_hostile_label_falls_back_safely(self):
        """Unavailable profile and malicious presentation cannot spoof a DM."""
        label = self.service._remote_label
        self.assertEqual(label(ACTOR), '@2@space.nachitima.com')
        self.assertEqual(label(ACTOR, 'nachitima'), '@nachitima@space.nachitima.com')
        self.assertEqual(label(ACTOR, '@everyone\n<@123>'), '@2@space.nachitima.com')
        self.assertLessEqual(len(label(ACTOR, '🦊' * 200 + 'hello')), 90)
        self.assertEqual(label(ACTOR, 'everyone'), '@2@space.nachitima.com')
        legacy = LocalFollowRequestObject(
            follow_activity_id='https://peer.example/follow/1',
            remote_inbox_url='https://peer.example/inbox',
        )
        self.assertIsNone(legacy.remote_actor_display_name)
        self._follow(1, None)
        self.assertIn('@2@space.nachitima.com', self.messages[0])


if __name__ == '__main__':
    unittest.main()
