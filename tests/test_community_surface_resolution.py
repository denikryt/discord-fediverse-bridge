"""Checkpoint 57 typed forum ownership regressions using actual migrated SQLite.

May run directly with PYTHONPATH=.:tests when optional pytest/Discord packages
are unavailable; pytest later collects identical unittest cases.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from src.db.database import Database
from src.platform_types import ChannelRef, MessageRef
from src.content_sync.surface_resolution import resolve_forum_source


FIXTURE = Path(__file__).parent / 'fixtures/release_v0_1_0_original.sqlite'


class ForumSourceOwnershipScenarios(unittest.TestCase):
    """A physical channel has one current authoritative source owner."""

    def setUp(self) -> None:
        """Seed one real v0 database with host/subscriber/remote forum claims."""
        from test_legacy_v0_to_v1_migration import _seed

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        file = Path(self.temp.name) / 'db.sqlite'
        shutil.copyfile(FIXTURE, file)
        self.db = Database(f'sqlite:///{file}')
        self.addCleanup(self.db.close)
        _seed(self.db)
        self.db.migrate()

    def _resolve(self, channel_id: str, *, kind='forum', guild='1'):
        """Use the same caller-owned SQL session as runtime admission."""
        with self.db.session() as session:
            return resolve_forum_source(session, ChannelRef('discord-primary', channel_id, kind=kind, external_guild_id=guild))

    def test_valid_host_subscriber_and_remote_sources(self) -> None:
        """Host, active subscriber and accepted remote all resolve to one mode."""
        host = self._resolve('900')
        self.assertEqual((host.owner_domain,host.owner_role,host.owner_id), ('local','local_host',8))
        sub = self._resolve('901')
        self.assertEqual((sub.owner_domain,sub.owner_role,sub.owner_id), ('local','local_subscriber',9))
        remote = self._resolve('902')
        self.assertEqual((remote.owner_domain,remote.owner_role,remote.owner_id), ('remote','remote_subscription',10))
        self.assertEqual(remote.community_key,'https://remote.test/c/news')

    def test_pending_and_disabled_claims_are_not_valid_sources(self) -> None:
        """A durable channel claim alone cannot authorize publication."""
        self.assertIsNone(self._resolve('903'))
        self.assertIsNone(self._resolve('904'))
        with self.db.immediate_session() as session:
            session.execute(__import__('sqlalchemy').text("UPDATE local_subscribers SET status='inactive' WHERE id=9"))
            session.execute(__import__('sqlalchemy').text("UPDATE local_communities SET status='disabled' WHERE id=8"))
        self.assertIsNone(self._resolve('900'))
        self.assertIsNone(self._resolve('901'))

    def test_wrong_connector_kind_and_guild_fail_closed(self) -> None:
        """No other connector, stale kind or unknown guild can claim a source."""
        self.assertIsNone(self._resolve('900',kind='text'))
        self.assertIsNone(self._resolve('900',guild='2'))
        self.assertIsNone(self._resolve('900',guild=None))
        with self.db.session() as session:
            self.assertIsNone(resolve_forum_source(session, ChannelRef('fake-connector','900',kind='forum',external_guild_id='1')))
        self.assertIsNone(self._resolve('not-a-channel'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
