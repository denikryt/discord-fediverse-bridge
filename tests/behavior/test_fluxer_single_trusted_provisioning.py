"""One operator-approved Fluxer identity survives SQLite admission without replacing prior state."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import select

from src.config import Settings
from src.db import Database
from src.db.repositories.platform_surfaces import PlatformSurfaceRepository
from src.models import PlatformConnector


class FluxerSingleTrustedProvisioning(unittest.TestCase):
    """Exercise config admission through real SQLite transactions and saved rows."""

    def setUp(self) -> None:
        """Create an isolated current-schema deployment with no Fluxer registration."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Database(f"sqlite:///{Path(tmp.name) / 'bridge.sqlite'}")
        self.addCleanup(self.db.close)
        self.db.migrate()

    @staticmethod
    def _config(key: str = 'fx-one', origin: str = 'https://ONE.test:443') -> Settings:
        """Construct one token-free operator config through real settings validation."""
        return Settings.model_validate({
            'DISCORD_TOKEN': 'discord-test',
            'PUBLIC_BASE_URL': 'https://bridge.example',
            'FEDIFY_SHARED_SECRET': 'fedify-secret',
            'BRIDGE_SUPER_ADMIN_USER_IDS': '1',
            'FLUXER_CONNECTORS_JSON': json.dumps([{
                'connector_key': key, 'origin': origin,
                'rpc_url': 'http://127.0.0.1:8769', 'transport_secret': 'private-bearer',
            }]),
        })

    def _admit(self, settings: Settings) -> None:
        """Use the same database boundary that startup will call before SDK launch."""
        configured = settings.fluxer_connectors[0] if settings.fluxer_connectors else None
        with self.db.session() as session:
            PlatformSurfaceRepository.admit_single_trusted_fluxer(
                session,
                connector_key=configured.connector_key if configured else None,
                instance_origin=configured.origin if configured else None,
            )

    def test_first_registration_and_exact_reuse_do_not_rekey(self) -> None:
        """An enabled trusted deployment inserts once and reuses its immutable row."""
        self._admit(self._config())
        with self.db.session() as session:
            original = session.scalar(select(PlatformConnector).where(PlatformConnector.platform == 'fluxer'))
            self.assertIsNotNone(original)
            first_id = original.id
            self.assertEqual(original.instance_origin, 'https://one.test')
        self._admit(self._config(origin='https://one.test.'))
        with self.db.session() as session:
            rows = session.scalars(select(PlatformConnector).where(PlatformConnector.platform == 'fluxer')).all()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].id, first_id)

    def test_different_persisted_origin_or_key_refused_without_rebinding(self) -> None:
        """One old connector survives invalid replacement attempts unchanged."""
        self._admit(self._config())
        for key, origin in [('fx-two', 'https://two.test'), ('fx-two', 'https://one.test'),
                            ('fx-one', 'https://two.test')]:
            with self.subTest(key=key, origin=origin), self.assertRaisesRegex(ValueError, 'Fluxer'):
                self._admit(self._config(key=key, origin=origin))
        with self.db.session() as session:
            rows = session.scalars(select(PlatformConnector).where(PlatformConnector.platform == 'fluxer')).all()
            self.assertEqual([(r.connector_key, r.instance_origin) for r in rows],
                             [('fx-one', 'https://one.test')])

    def test_two_saved_rows_refused_even_if_fluxer_disabled(self) -> None:
        """An invalid preexisting DB is rejected, not silently cleaned or adopted."""
        with self.db.session() as session:
            PlatformSurfaceRepository.add_connector(session, connector_key='fx-one',
                platform='fluxer', instance_origin='https://one.test')
            PlatformSurfaceRepository.add_connector(session, connector_key='fx-two',
                platform='fluxer', instance_origin='https://two.test')
        with self.assertRaisesRegex(ValueError, 'Fluxer'):
            self._admit(Settings.model_validate({
                'DISCORD_TOKEN': 'discord-test', 'PUBLIC_BASE_URL': 'https://bridge.example',
                'FEDIFY_SHARED_SECRET': 'fedify-secret', 'BRIDGE_SUPER_ADMIN_USER_IDS': '1',
            }))
        with self.db.session() as session:
            self.assertEqual(session.scalar(select(PlatformConnector).where(
                PlatformConnector.connector_key == 'fx-two')).instance_origin, 'https://two.test')

    def test_disabled_start_does_not_add_row(self) -> None:
        """Opt-out preserves a clean DB and does not trigger client provisioning."""
        settings = Settings.model_validate({
            'DISCORD_TOKEN': 'discord-test', 'PUBLIC_BASE_URL': 'https://bridge.example',
            'FEDIFY_SHARED_SECRET': 'fedify-secret', 'BRIDGE_SUPER_ADMIN_USER_IDS': '1',
        })
        self._admit(settings)
        with self.db.session() as session:
            self.assertEqual(session.scalars(select(PlatformConnector).where(
                PlatformConnector.platform == 'fluxer')).all(), [])


if __name__ == '__main__':
    unittest.main()
