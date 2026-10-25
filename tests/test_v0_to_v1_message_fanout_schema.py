"""Stage 04 checkpoint 56 schema/upgrade regressions, runnable without Discord SDK.

These tests exercise SQLite rather than a mocked database. They also run as
`python tests/test_v0_to_v1_message_fanout_schema.py` when pytest dependencies
are unavailable; pytest discovers the same unittest cases when restored.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from sqlalchemy.exc import IntegrityError

from src.db.database import Database
from src.models import (
    Base,
)


FIXTURE = Path(__file__).parent / "fixtures" / "release_v0_1_0_original.sqlite"
CANONICAL = (
    "community_thread_groups",
    "community_message_groups",
    "local_community_threads",
    "local_community_messages",
)
COPIES = (
    "community_thread_group_deliveries",
    "community_message_group_deliveries",
    "local_community_thread_surfaces",
    "local_community_message_surfaces",
)


class Stage04V1SchemaTests(unittest.TestCase):
    """Prove exact current-v1 DDL and no fabricated release-v0 feature state."""

    def setUp(self) -> None:
        """Use one independent database for each positive or adversarial case."""
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.file = Path(self.tmp.name) / "bridge.db"

    def database(self, legacy: bool = False) -> Database:
        """Open a bootstrapped or tagged-v0 database with a close cleanup."""
        if legacy:
            shutil.copyfile(FIXTURE, self.file)
        db = Database(f"sqlite:///{self.file}")
        self.addCleanup(db.close)
        db.migrate()
        return db

    def test_fresh_v1_has_owner_revision_and_copy_state(self) -> None:
        """Both canonical domains and all historical-copy types own mutations."""
        db = self.database()
        with db.engine.connect() as conn:
            for table in CANONICAL:
                with self.subTest(table=table):
                    columns = {r[1] for r in conn.exec_driver_sql(f'PRAGMA table_xinfo("{table}")')}
                    self.assertTrue({"deleted_at", "content_revision", "current_content_json", "last_source_update_order_key", "last_source_update_identity"} <= columns)
            for table in COPIES:
                with self.subTest(table=table):
                    columns = {r[1] for r in conn.exec_driver_sql(f'PRAGMA table_xinfo("{table}")')}
                    self.assertTrue({"applied_revision", "mutation_state", "mutation_target_revision", "mutation_claim_token", "mutation_lease_until"} <= columns)
            self.assertIn("platform_delivery_intents", Base.metadata.tables)
            self.assertIn("local_community_authored_announce_deliveries", Base.metadata.tables)
            self.assertIn("direct_authorizations_json", {r[1] for r in conn.exec_driver_sql('PRAGMA table_xinfo("user_actor_outbound_deliveries")')})

    def test_released_v0_migrates_to_v1_without_fabricated_attempts(self) -> None:
        """Old rows retain physical identity and never gain fictitious intents."""
        db = self.database(legacy=True)
        with db.engine.connect() as conn:
            self.assertEqual(conn.exec_driver_sql('PRAGMA user_version').scalar_one(), 1)
            self.assertEqual(conn.exec_driver_sql('SELECT COUNT(*) FROM platform_delivery_intents').scalar_one(), 0)
            self.assertEqual(conn.exec_driver_sql('SELECT COUNT(*) FROM local_community_authored_announce_deliveries').scalar_one(), 0)
            self.assertEqual(conn.exec_driver_sql('PRAGMA foreign_key_check').all(), [])
            self.assertEqual(conn.exec_driver_sql('PRAGMA integrity_check').scalar_one(), 'ok')
            for table in COPIES:
                self.assertEqual(conn.exec_driver_sql(f'SELECT COUNT(*) FROM "{table}" WHERE applied_revision IS NOT NULL').scalar_one(), 0)
            for table in CANONICAL:
                self.assertEqual(conn.exec_driver_sql(f'SELECT COUNT(*) FROM "{table}" WHERE deleted_at IS NOT NULL OR content_revision != 0').scalar_one(), 0)
        # A second boot must validate, not repair, the exact provisional v1.
        db.migrate()

    def test_populated_released_v0_preserves_rows_and_unknown_revisions(self) -> None:
        """Real released model rows upgrade without invented revision or receipts."""
        from test_legacy_v0_to_v1_migration import _seed

        shutil.copyfile(FIXTURE, self.file)
        db = Database(f"sqlite:///{self.file}")
        self.addCleanup(db.close)
        _seed(db)
        with db.engine.connect() as conn:
            before = {
                table: conn.exec_driver_sql(f'SELECT COUNT(*) FROM "{table}"').scalar_one()
                for table in CANONICAL + COPIES
            }
        self.assertGreater(sum(before.values()), 0)
        db.migrate()
        with db.engine.connect() as conn:
            after = {
                table: conn.exec_driver_sql(f'SELECT COUNT(*) FROM "{table}"').scalar_one()
                for table in before
            }
            self.assertEqual(after, before)
            for table in COPIES:
                self.assertEqual(conn.exec_driver_sql(f'SELECT COUNT(*) FROM "{table}" WHERE applied_revision IS NOT NULL').scalar_one(), 0)
            for table in CANONICAL:
                self.assertEqual(conn.exec_driver_sql(f'SELECT COUNT(*) FROM "{table}" WHERE deleted_at IS NOT NULL OR content_revision != 0').scalar_one(), 0)
            self.assertEqual(conn.exec_driver_sql('SELECT COUNT(*) FROM platform_delivery_intents').scalar_one(),0)
            self.assertEqual(conn.exec_driver_sql('SELECT COUNT(*) FROM local_community_authored_announce_deliveries').scalar_one(),0)
            self.assertEqual(conn.exec_driver_sql('PRAGMA foreign_key_check').all(),[])
        db.migrate()

    def test_group_announce_snapshot_survives_follow_deletion_and_rejects_conflict(self) -> None:
        """Immutable F1 evidence survives physical Undo and is never an FK to F1."""
        from test_legacy_v0_to_v1_migration import _seed

        shutil.copyfile(FIXTURE, self.file)
        db = Database(f"sqlite:///{self.file}")
        self.addCleanup(db.close)
        _seed(db)
        db.migrate()
        with db.engine.begin() as conn:
            sql = """INSERT INTO local_community_authored_announce_deliveries
                (published_object_id, local_community_id, target_remote_actor_id,
                 target_follow_activity_id, target_remote_inbox_url, delivery_profile,
                 group_actor_id, activity_kind, content_revision, announce_activity_id,
                 activity_json, created_at, updated_at)
                VALUES(20,8,'https://remote.test/u/bob','https://remote.test/follow/30',
                  'https://remote.test/u/bob/inbox','threadiverse_group',
                  'https://bridge.test/actors/communities/news','create',0,
                  'https://bridge.test/announce/20', '{"type":"Announce"}',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"""
            conn.exec_driver_sql(sql)
            conn.exec_driver_sql('DELETE FROM remote_subscribers WHERE id=30')
            self.assertEqual(conn.exec_driver_sql('PRAGMA foreign_key_check').all(),[])
            self.assertEqual(conn.exec_driver_sql('SELECT target_follow_activity_id FROM local_community_authored_announce_deliveries').scalar_one(),'https://remote.test/follow/30')
            with self.assertRaises(IntegrityError):
                with conn.begin_nested():
                    conn.exec_driver_sql(sql)
            with self.assertRaises(IntegrityError):
                with conn.begin_nested():
                    conn.exec_driver_sql(sql.replace("'create',0,", "'update',0,").replace("announce/20'", "announce/21'"))
        db.migrate()

    def test_direct_recipient_evidence_is_bounded_and_not_raw_url(self) -> None:
        """The schema stores typed evidence, rejecting malformed/oversize JSON."""
        db = self.database()
        with db.engine.connect() as conn:
            ddl = conn.exec_driver_sql("SELECT sql FROM sqlite_master WHERE name='user_actor_outbound_deliveries'").scalar_one()
            self.assertIn('direct_authorizations_json',ddl)
            self.assertIn('json_valid',ddl)
            self.assertIn('16384',ddl)

    def test_direct_evidence_rejects_empty_malformed_and_forged_format(self) -> None:
        """A raw URL or empty list is not a valid direct-recipient audience."""
        from test_legacy_v0_to_v1_migration import _seed

        shutil.copyfile(FIXTURE, self.file)
        db = Database(f"sqlite:///{self.file}")
        self.addCleanup(db.close)
        _seed(db)
        db.migrate()
        with db.engine.begin() as conn:
            conn.exec_driver_sql("""INSERT INTO user_actor_outbound_sources
            (user_id,source_kind,ordering_key,logical_version,activity_id,published_object_id,
             snapshot_json,state,render_attempt_count,created_at,updated_at)
             VALUES (7,'content_create','object-test',1,'https://bridge.test/a/example',20,
             '{}','render_pending',0,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)""")
            source_id = conn.exec_driver_sql('SELECT max(id) FROM user_actor_outbound_sources').scalar_one()
            statement = """INSERT INTO user_actor_outbound_deliveries
              (source_id,target_endpoint_url,direct_authorizations_json,status,attempt_count,created_at,updated_at)
              VALUES (:source,'https://remote.test/inbox',:evidence,'pending',0,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"""
            # A nonempty typed, versioned array is structurally accepted; actor
            # and context authorization is checked by the checkpoint 58 writer.
            conn.exec_driver_sql(statement,dict(source=source_id,evidence='[{"version":1,"context_kind":"remote_community_publication","actor_id":"https://remote.test/c/group"}]'))
            conn.exec_driver_sql('DELETE FROM user_actor_outbound_deliveries WHERE source_id=?',(source_id,))
            for bad in ('[]','"https://remote.test/inbox"','not json','[{"version":1},]'):
                with self.subTest(evidence=bad), self.assertRaises(IntegrityError):
                    with conn.begin_nested():
                        conn.exec_driver_sql(statement,dict(source=source_id,evidence=bad))

    def test_state_constraints_reject_missing_orphan_proof(self) -> None:
        """An orphan outcome without a checked physical receipt is forbidden."""
        db = self.database()
        with db.engine.begin() as conn:
            # A connector exists after the normal fresh-v1 bootstrap; no community
            # foreign key is needed for the create-only polymorphic intent.
            conn.exec_driver_sql("INSERT INTO platform_channel_surfaces (connector_id,external_channel_id,kind,created_at,updated_at) VALUES (1,'forum-x','forum',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)")
            surface = conn.exec_driver_sql("SELECT id FROM platform_channel_surfaces WHERE external_channel_id='forum-x'").scalar_one()
            params = dict(surface=surface)
            sql = """INSERT INTO platform_delivery_intents
                 (domain,object_kind,canonical_row_id,connector_id,channel_surface_id,operation,attempt_key,initial_revision,request_revision,
                  target_owner_kind,target_owner_id,state,request_json,request_fingerprint,desired_revision,applied_revision,desired_deleted,dispatch_token,created_at,updated_at)
                 VALUES ('local','post',1,1,:surface,'create','attempt-a',0,0,'local_community',1,'cancelled_after_send','{}','fingerprint',0,0,0,'claim-A',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"""
            with self.assertRaises(IntegrityError):
                with conn.begin_nested():
                    conn.exec_driver_sql(sql,params)
            # Unknown physical outcome with its immutable request remains valid.
            conn.exec_driver_sql(sql.replace("'cancelled_after_send'", "'unresolved'"),params)

    def test_source_uniqueness_scopes_immediate_container(self) -> None:
        """A single connector may reuse message IDs in distinct containers."""
        db = self.database()
        with db.engine.begin() as conn:
            for idx, container in enumerate(('thread-a','thread-b'), start=1):
                conn.exec_driver_sql("""INSERT INTO message_mappings
                (source_connector_id,source_external_container_id,source_platform,source_id,activity_id,object_id,actor_url,publication_context,community_actor_url,created_at)
                VALUES (1,:container,'discord','777',:activity,:object,'https://b.test/users/x','community','https://remote.test/c/g',CURRENT_TIMESTAMP)""",
                {'container':container,'activity':f'https://b.test/a/{idx}','object':f'https://b.test/o/{idx}'})
            with self.assertRaises(IntegrityError):
                with conn.begin_nested():
                    conn.exec_driver_sql("""INSERT INTO message_mappings
                    (source_connector_id,source_external_container_id,source_platform,source_id,activity_id,object_id,actor_url,publication_context,community_actor_url,created_at)
                    VALUES (1,'thread-a','discord','777','https://b.test/a/3','https://b.test/o/3','https://b.test/users/x','community','https://remote.test/c/g',CURRENT_TIMESTAMP)""")


if __name__ == "__main__":
    unittest.main(verbosity=2)
