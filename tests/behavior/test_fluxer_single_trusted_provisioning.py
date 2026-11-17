"""One operator-approved Fluxer identity survives SQLite admission without replacing prior state."""

from __future__ import annotations

import json
import runpy
import shutil
import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.config import Settings
from src.db import Database
from src.db.repositories.platform_surfaces import PlatformSurfaceRepository, preflight_instance_mode
from src.actor_key_service import BridgeActorKeyBootstrap
from src.models import PlatformConnector, PublishedActivityObject


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
            'BRIDGE_PLATFORM': 'fluxer',
            'PUBLIC_BASE_URL': 'https://bridge.example',
            'FEDIFY_SHARED_SECRET': 'fedify-secret',
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


class FluxerPreMigrationOwnership(unittest.TestCase):
    """Compare physical SQLite state across mode admission and refusal."""

    def setUp(self) -> None:
        """Allocate one unique file per test; preflight must never create it."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.path = self.root / 'bridge.sqlite'
        self.url = f'sqlite:///{self.path}'
        self.config = FluxerSingleTrustedProvisioning._config()

    def _preflight(self, *, mode: str = 'fluxer', key: str = 'fx-one',
                   origin: str = 'https://one.test') -> None:
        """Exercise the actual pre-migration boundary, not a second classifier."""
        preflight_instance_mode(
            self.url, platform=mode,
            connector_key=key if mode == 'fluxer' else None,
            instance_origin=origin if mode == 'fluxer' else None,
        )

    def _new_v1(self) -> Database:
        """Open real current-schema SQLite with its inert Discord metadata."""
        db = Database(self.url)
        db.migrate()
        return db

    def test_new_database_and_inert_v1_bootstrap_can_be_admitted(self) -> None:
        """No file is created by preflight; an unused Discord row has no identity."""
        self._preflight()
        self.assertFalse(self.path.exists())
        db = self._new_v1()
        db.close()
        before = hashlib.sha256(self.path.read_bytes()).digest()
        self._preflight()
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).digest(), before)

    def test_existing_service_actor_key_blocks_fluxer_without_content(self) -> None:
        """A key-only Discord startup cannot leak its AP identity to Fluxer."""
        db = self._new_v1()
        try:
            BridgeActorKeyBootstrap(database=db, settings=self.config).ensure()
        finally:
            db.close()
        before = self.path.read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'unbound service actor key'):
            self._preflight()
        self.assertEqual(self.path.read_bytes(), before)

    def test_matching_binding_and_key_survive_restart_and_mismatch_is_read_only(self) -> None:
        """Saved Fluxer origin/key are immutable across exact repeat startup."""
        db = self._new_v1()
        try:
            with db.session() as session:
                PlatformSurfaceRepository.admit_single_trusted_fluxer(
                    session, connector_key='fx-one', instance_origin='https://one.test',
                )
            BridgeActorKeyBootstrap(database=db, settings=self.config).ensure()
        finally:
            db.close()
        before = self.path.read_bytes()
        self._preflight()
        with self.assertRaisesRegex(RuntimeError, 'differs from persisted'):
            self._preflight(key='fx-other')
        with self.assertRaisesRegex(RuntimeError, 'differs from persisted'):
            self._preflight(origin='https://two.test')
        with self.assertRaisesRegex(RuntimeError, 'Fluxer-bound'):
            self._preflight(mode='discord')
        self.assertEqual(self.path.read_bytes(), before)

    def test_bound_fluxer_refuses_surface_less_discord_mapping_before_migration(self) -> None:
        """A valid legacy Discord source FK cannot hide behind a NULL channel surface."""
        db = self._new_v1()
        try:
            with db.session() as session:
                PlatformSurfaceRepository.admit_single_trusted_fluxer(
                    session, connector_key='fx-one', instance_origin='https://one.test',
                )
                # This historically allowed shape passes real SQLite FK checks.
                session.execute(__import__('sqlalchemy').text(
                    "INSERT INTO message_mappings (source_connector_id,source_platform,source_id,"
                    "activity_id,object_id,actor_url,publication_context,community_actor_url,created_at) "
                    "VALUES ((SELECT id FROM platform_connectors WHERE connector_key='discord-primary'),"
                    "'discord','source-1','https://ap.test/activities/1',"
                    "'https://ap.test/objects/1','https://ap.test/actors/1','community',"
                    "'https://ap.test/actors/groups/1',CURRENT_TIMESTAMP)"
                ))
            with db.engine.connect() as connection:
                self.assertEqual(connection.exec_driver_sql('PRAGMA foreign_key_check').all(), [])
        finally:
            db.close()
        before = self.path.read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'Discord-owned'):
            self._preflight()
        self.assertEqual(self.path.read_bytes(), before)

    def test_bound_fluxer_allows_own_copy_but_preserves_actor_key_on_refusal(self) -> None:
        """Exact Fluxer-source state is valid; a later foreign source preserves the AP key."""
        db = self._new_v1()
        try:
            with db.session() as session:
                PlatformSurfaceRepository.admit_single_trusted_fluxer(
                    session, connector_key='fx-one', instance_origin='https://one.test',
                )
                session.execute(__import__('sqlalchemy').text(
                    "INSERT INTO message_mappings (source_connector_id,source_platform,source_id,"
                    "activity_id,object_id,actor_url,publication_context,community_actor_url,created_at) "
                    "VALUES ((SELECT id FROM platform_connectors WHERE connector_key='fx-one'),"
                    "'fluxer','source-1','https://ap.test/activities/1',"
                    "'https://ap.test/objects/1','https://ap.test/actors/1','community',"
                    "'https://ap.test/actors/groups/1',CURRENT_TIMESTAMP)"
                ))
            BridgeActorKeyBootstrap(database=db, settings=self.config).ensure()
        finally:
            db.close()
        self._preflight()
        db = Database(self.url)
        try:
            with db.session() as session:
                session.execute(__import__('sqlalchemy').text(
                    "INSERT INTO message_mappings (source_connector_id,source_platform,source_id,"
                    "activity_id,object_id,actor_url,publication_context,community_actor_url,created_at) "
                    "VALUES ((SELECT id FROM platform_connectors WHERE connector_key='discord-primary'),"
                    "'discord','source-2','https://ap.test/activities/2',"
                    "'https://ap.test/objects/2','https://ap.test/actors/1','community',"
                    "'https://ap.test/actors/groups/1',CURRENT_TIMESTAMP)"
                ))
        finally:
            db.close()
        before = self.path.read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'Discord-owned'):
            self._preflight()
        self.assertEqual(self.path.read_bytes(), before)

    def _assert_legacy_group_refused(self, table: str, legacy_ids: str) -> None:
        """Check a single canonical source row independently of other group types."""
        from sqlalchemy import text

        db = self._new_v1()
        try:
            with db.session() as session:
                PlatformSurfaceRepository.admit_single_trusted_fluxer(
                    session, connector_key='fx-one', instance_origin='https://one.test',
                )
                # Unscoped legacy fields need no channel surface or connector.
                # This valid current-schema shape must still be refused.
                fields = 'thread_group_id,' if table == 'community_message_groups' else ''
                values = '1,' if fields else ''
                session.execute(text(
                    f"INSERT INTO {table} "
                    f"(community_actor_id,{fields}source_channel_id,source_thread_id,"
                    f"{legacy_ids},created_at) "
                    f"VALUES ('https://ap.test/groups/1',{values}11,22,33,CURRENT_TIMESTAMP)"
                ))
            BridgeActorKeyBootstrap(database=db, settings=self.config).ensure()
            with db.engine.connect() as connection:
                self.assertEqual(connection.exec_driver_sql('PRAGMA foreign_key_check').all(), [])
        finally:
            db.close()
        before = self.path.read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'Discord-owned'):
            self._preflight()
        self.assertEqual(self.path.read_bytes(), before)
        # The actual composition root must refuse this mode before opening a
        # migrator or creating a new signing identity, not only at unit preflight.
        from src.app import build_runtime
        settings = self.config.model_copy(update={
            'database_url': self.url,
            'profile_media_root': str(self.root / 'media'),
            'profile_media_lock_path': str(self.root / 'media.lock'),
            'deployment_state_lock_path': str(self.root / 'deployment.lock'),
        })
        with self.assertRaisesRegex(RuntimeError, 'Discord-owned'):
            build_runtime(settings)
        self.assertEqual(self.path.read_bytes(), before)

    def test_bound_fluxer_refuses_legacy_discord_thread_group_ids(self) -> None:
        """Legacy starter ID without source connector blocks Fluxer admission."""
        self._assert_legacy_group_refused(
            'community_thread_groups', 'source_starter_message_id',
        )

    def test_bound_fluxer_refuses_legacy_discord_message_group_ids(self) -> None:
        """Legacy message ID without source connector blocks Fluxer admission."""
        self._assert_legacy_group_refused('community_message_groups', 'source_message_id')

    def test_bound_fluxer_allows_null_legacy_projections_and_scoped_source(self) -> None:
        """Fluxer and remote AP canonical groups are not mistaken for Discord."""
        from sqlalchemy import text

        db = self._new_v1()
        try:
            with db.session() as session:
                PlatformSurfaceRepository.admit_single_trusted_fluxer(
                    session, connector_key='fx-one', instance_origin='https://one.test',
                )
                session.execute(text(
                    "INSERT INTO community_thread_groups "
                    "(community_actor_id,source_connector_id,source_external_thread_id,created_at) "
                    "VALUES ('https://ap.test/groups/1',"
                    "(SELECT id FROM platform_connectors WHERE connector_key='fx-one'),"
                    "'thread-fx',CURRENT_TIMESTAMP)"
                ))
                session.execute(text(
                    "INSERT INTO community_message_groups "
                    "(community_actor_id,thread_group_id,source_connector_id,"
                    "source_external_container_id,source_external_message_id,created_at) "
                    "VALUES ('https://ap.test/groups/1',1,"
                    "(SELECT id FROM platform_connectors WHERE connector_key='fx-one'),"
                    "'thread-fx','message-fx',CURRENT_TIMESTAMP)"
                ))
            BridgeActorKeyBootstrap(database=db, settings=self.config).ensure()
        finally:
            db.close()
        before = self.path.read_bytes()
        self._preflight()
        self.assertEqual(self.path.read_bytes(), before)

    def test_bound_fluxer_copy_requires_no_discord_integer_projection(self) -> None:
        """A valid Fluxer physical target is allowed until it claims a Discord ID."""
        from sqlalchemy import text

        db = self._new_v1()
        try:
            with db.session() as session:
                PlatformSurfaceRepository.admit_single_trusted_fluxer(
                    session, connector_key='fx-one', instance_origin='https://one.test',
                )
                PlatformSurfaceRepository.ensure_channel(
                    session, connector_key='fx-one', external_channel_id='forum-a',
                    external_guild_id='guild-a', kind='forum',
                )
                session.execute(text(
                    "INSERT INTO post_links (connector_id,channel_surface_id,external_thread_id,"
                    "lemmy_post_id,direction,created_at) VALUES ("
                    "(SELECT id FROM platform_connectors WHERE connector_key='fx-one'),"
                    "(SELECT id FROM platform_channel_surfaces WHERE external_channel_id='forum-a'),"
                    "'thread-a',7,'inbound',CURRENT_TIMESTAMP)"
                ))
        finally:
            db.close()
        self._preflight()
        db = Database(self.url)
        try:
            with db.session() as session:
                session.execute(text("UPDATE post_links SET discord_forum_thread_id=123"))
        finally:
            db.close()
        before = self.path.read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'Discord-owned'):
            self._preflight()
        self.assertEqual(self.path.read_bytes(), before)

    def test_bound_fluxer_refuses_discord_authored_object_without_surface(self) -> None:
        """An authored object's direct source FK proves ownership without a surface."""
        db = self._new_v1()
        try:
            with db.session() as session:
                PlatformSurfaceRepository.admit_single_trusted_fluxer(
                    session, connector_key='fx-one', instance_origin='https://one.test',
                )
                discord = session.scalar(select(PlatformConnector).where(
                    PlatformConnector.connector_key == 'discord-primary',
                ))
                session.add(PublishedActivityObject(
                    source_connector_id=discord.id,
                    actor_username='author', actor_url='https://ap.test/actor/author',
                    publication_context='community',
                    community_actor_url='https://ap.test/groups/a',
                    activity_id='https://ap.test/activities/a',
                    object_id='https://ap.test/objects/a', kind='post',
                    body_markdown='content',
                ))
        finally:
            db.close()
        before = self.path.read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'Discord-owned'):
            self._preflight()
        self.assertEqual(self.path.read_bytes(), before)

    def test_bound_fluxer_rejects_discord_source_platform_even_with_fluxer_fk(self) -> None:
        """Conflicting canonical source-platform metadata cannot authorize Fluxer."""
        from sqlalchemy import text
        db = self._new_v1()
        try:
            with db.session() as session:
                PlatformSurfaceRepository.admit_single_trusted_fluxer(
                    session, connector_key='fx-one', instance_origin='https://one.test',
                )
                session.execute(text(
                    "INSERT INTO message_mappings (source_connector_id,source_platform,source_id,"
                    "activity_id,object_id,actor_url,publication_context,community_actor_url,created_at) "
                    "VALUES ((SELECT id FROM platform_connectors WHERE connector_key='fx-one'),"
                    "'discord','source-1','https://ap.test/a/1','https://ap.test/o/1',"
                    "'https://ap.test/users/1','community','https://ap.test/groups/1',CURRENT_TIMESTAMP)"
                ))
        finally:
            db.close()
        before = self.path.read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'Discord-owned'):
            self._preflight()
        self.assertEqual(self.path.read_bytes(), before)

    def test_released_v0_refused_before_its_committing_migration(self) -> None:
        """The published v0 source is classified without an upgrade or key write."""
        fixture = Path(__file__).resolve().parents[1] / 'fixtures/release_v0_1_0_original.sqlite'
        shutil.copy2(fixture, self.path)
        # Populate the released schema through its pinned historical ORM so the
        # refusal also protects real registered actor rows, not just empty DDL.
        old = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'fixtures/release_v0_1_0_models.py'))
        db = Database(self.url)
        try:
            with Session(db.engine) as session:
                session.add(old['User'](
                    id=7, discord_user_id='1', activitypub_username='alice',
                    actor_url='https://bridge.test/actors/users/alice',
                    inbox_url='https://bridge.test/actors/users/alice/inbox',
                    outbox_url='https://bridge.test/actors/users/alice/outbox',
                    followers_url='https://bridge.test/actors/users/alice/followers',
                    public_key_pem='public', private_key_pem='private',
                ))
                session.commit()
        finally:
            db.close()
        before = self.path.read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'released Discord v0'):
            self._preflight()
        self._preflight(mode='discord')
        self.assertEqual(self.path.read_bytes(), before)

    def test_unknown_partial_schema_and_discord_surface_are_rejected(self) -> None:
        """A fabricated v1 label or a Discord channel claim cannot select Fluxer."""
        with sqlite3.connect(self.path) as connection:
            connection.execute('CREATE TABLE unknown (id INTEGER)')
            connection.execute('PRAGMA user_version=1')
        before = self.path.read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'current v1|Unknown'):
            self._preflight()
        self.assertEqual(self.path.read_bytes(), before)
        self.path.unlink()
        db = self._new_v1()
        try:
            with db.session() as session:
                PlatformSurfaceRepository.admit_single_trusted_fluxer(
                    session, connector_key='fx-one', instance_origin='https://one.test',
                )
                PlatformSurfaceRepository.ensure_channel(
                    session, connector_key='discord-primary', external_channel_id='123',
                    external_guild_id='456', kind='forum',
                )
        finally:
            db.close()
        before = self.path.read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'Discord-owned channel surfaces'):
            self._preflight()
        self.assertEqual(self.path.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
