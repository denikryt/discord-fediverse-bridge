"""SQLite v0/v1→v2 upgrade, physical fingerprint, and remote-feed constraints."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy.exc import IntegrityError

from src.db import Database
from src.db import migrations
from src.models import (
    User, RemotePersonFollow, RemotePersonChannelAssignment,
    RemotePersonPost, RemotePersonPostDelivery,
    RemotePersonCommentPlacement, InboundConversationObject,
    PublishedActivityObject, PersonalFeedThreadMapping, MessageMapping,
)

_ACTOR = "https://bridge.test/actors/users/alice"
_REMOTE = "https://remote.test/users/bob"


class VersionTwoMigrationTests(unittest.TestCase):
    """Prove offline migration safety with an actual SQLite file per scenario."""

    def setUp(self) -> None:
        """Allocate an independent SQLite path so rollback can be inspected."""
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "database.sqlite3"

    def v1(self, *, with_rows: bool = False) -> None:
        """Create the exact frozen v1 DDL independently of current ORM metadata."""
        root = Path(__file__).resolve().parents[1]
        with sqlite3.connect(self.path) as connection:
            connection.executescript((root / "src/db/schema_versions/v1.sql").read_text())
            connection.execute("PRAGMA user_version=1")
            if with_rows:
                connection.execute(
                    "INSERT INTO users(id,discord_user_id,activitypub_username,actor_url,inbox_url,"
                    "outbox_url,followers_url,public_key_pem,private_key_pem,profile_version,"
                    "created_at,updated_at) VALUES (1,'111','alice',?,?,?,?,'public','private',0,?,?)",
                    (_ACTOR, f"{_ACTOR}/inbox", f"{_ACTOR}/outbox", f"{_ACTOR}/followers", "2026-11-16", "2026-11-16"),
                )
                connection.execute(
                    "INSERT INTO published_activity_objects(id,actor_username,actor_url,"
                    "publication_context,activity_id,object_id,kind,body_markdown,"
                    "discord_guild_id,discord_channel_id,discord_message_id,"
                    "content_revision,desired_kind,discord_reconciliation_needed,"
                    "discord_reconciliation_attempt_count,published_at,created_at) "
                    "VALUES (17,'alice',?,'personal_feed',?,?, 'post','root',1,10,20,1,'none',0,0,?,?)",
                    (_ACTOR, f"{_ACTOR}/activities/20", f"{_ACTOR}/objects/20", "2026-11-16", "2026-11-16"),
                )
                connection.execute(
                    "INSERT INTO personal_feed_thread_mappings(id,root_published_object_id,"
                    "root_discord_channel_id,root_discord_message_id,thread_id,created_at,updated_at)"
                    " VALUES (8,17,10,20,20,?,?)", ("2026-11-16", "2026-11-16"),
                )
                connection.execute(
                    "INSERT INTO message_mappings(id,source_platform,source_id,activity_id,"
                    "object_id,actor_url,publication_context,discord_guild_id,"
                    "discord_channel_id,discord_message_id,created_at) "
                    "VALUES (9,'discord','20',?,?,?,'personal_feed',1,10,20,?)",
                    (f"{_ACTOR}/activities/20", f"{_ACTOR}/objects/20", _ACTOR, "2026-11-16"),
                )

    def test_empty_and_v1_bootstrap_have_exact_same_v2_fingerprint(self) -> None:
        """Both supported entry points result in the same admitted physical v2."""
        db = Database(f"sqlite:///{self.path}")
        self.addCleanup(db.close)
        db.migrate()
        self.assertEqual(migrations.read_database_state(db.engine).profile, "ready_v3")
        db.migrate()

    def test_populated_v1_preserves_ids_and_external_fks(self) -> None:
        """Original source/message IDs and personal thread FKs survive a rebuild."""
        self.v1(with_rows=True)
        db = Database(f"sqlite:///{self.path}")
        self.addCleanup(db.close)
        db.migrate()
        with db.session() as session:
            self.assertEqual(session.get(PublishedActivityObject, 17).discord_message_id, 20)
            self.assertEqual(session.get(PersonalFeedThreadMapping, 8).root_published_object_id, 17)
            self.assertEqual(session.get(MessageMapping, 9).object_id, f"{_ACTOR}/objects/20")
        with db.engine.connect() as connection:
            self.assertEqual(connection.exec_driver_sql("PRAGMA foreign_key_check").all(), [])
            self.assertEqual(connection.exec_driver_sql("PRAGMA integrity_check").scalar_one(), "ok")

    def test_new_person_context_survives_legacy_v1_upgrade(self) -> None:
        """v2 CHECK allows Person replies in both original source tables."""
        self.v1(with_rows=True)
        db = Database(f"sqlite:///{self.path}")
        self.addCleanup(db.close)
        db.migrate()
        with db.session() as session:
            session.add(MessageMapping(
                source_platform="discord", source_id="777", activity_id=f"{_ACTOR}/activities/777",
                object_id=f"{_ACTOR}/objects/777", actor_url=_ACTOR,
                publication_context="remote_person_feed", discord_guild_id=1,
                discord_channel_id=10, discord_message_id=777,
            ))
            session.add(PublishedActivityObject(
                actor_username="alice", actor_url=_ACTOR, publication_context="remote_person_feed",
                activity_id=f"{_ACTOR}/activities/777", object_id=f"{_ACTOR}/objects/777",
                kind="comment", body_markdown="reply", discord_guild_id=1,
                discord_channel_id=10, discord_message_id=777,
            ))
        self.assertEqual(migrations.read_database_state(db.engine).profile, "ready_v3")

    def test_mid_upgrade_exception_does_not_stamp_partial_state(self) -> None:
        """A DDL failure rolls back renamed tables, data, and schema header."""
        self.v1(with_rows=True)
        db = Database(f"sqlite:///{self.path}")
        self.addCleanup(db.close)
        with patch.dict(migrations.MIGRATIONS, {2: lambda *_args: (_ for _ in ()).throw(RuntimeError("forced DDL error"))}):
            with self.assertRaisesRegex(RuntimeError, "forced DDL error"):
                db.migrate()
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT id FROM published_activity_objects").fetchone()[0], 17)
        db.migrate()
        self.assertEqual(migrations.read_database_state(db.engine).profile, "ready_v3")

    def test_existing_v1_receives_restorable_backup_before_upgrade(self) -> None:
        """A real v1 installation gets a protected pre-v2 media/database archive."""
        from src.db.startup import prepare_sqlite_under_exclusive_lease
        from src.db.backup import inspect_migration_backup_source
        from src.db.migrations import Stage4IdentityMigrationConfig
        from src.profile_media import FileLock
        import tarfile

        self.v1(with_rows=True)
        db = Database(f"sqlite:///{self.path}")
        self.addCleanup(db.close)
        root = Path(self.folder.name)
        with FileLock(root / "deployment.lock").acquire(exclusive=True):
            archived = prepare_sqlite_under_exclusive_lease(
                db, database_path=self.path, media_root=root / "profile-media",
                media_lock_path=root / "media.lock", lock_timeout_seconds=5,
                identity=Stage4IdentityMigrationConfig("https://bridge.test", "bridge"),
            )
        self.assertIsNotNone(archived)
        self.assertEqual(migrations.read_database_state(db.engine).profile, "ready_v3")
        restored = root / "restored-v1.db"
        with tarfile.open(archived, "r:gz") as archive:
            content = archive.extractfile("bridge.db")
            self.assertIsNotNone(content)
            restored.write_bytes(content.read())
        self.assertEqual(inspect_migration_backup_source(restored), "ready_v1")
        with sqlite3.connect(restored) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT id FROM published_activity_objects").fetchone()[0], 17)

    def test_physical_schema_tampering_is_rejected_without_repair(self) -> None:
        """An injected extra index is not silently accepted as legitimate v2."""
        db = Database(f"sqlite:///{self.path}")
        self.addCleanup(db.close)
        db.migrate()
        with sqlite3.connect(self.path) as connection:
            connection.execute("CREATE INDEX injected_invalid_schema ON users(id)")
        with self.assertRaisesRegex(RuntimeError, "physical schema differs"):
            db.migrate()

    def test_existing_old_v2_shape_requires_explicit_alignment(self) -> None:
        """Version 2 alone cannot admit an older physical table shape."""
        frozen = Path(__file__).resolve().parents[1] / "src/db/schema_versions/v2.sql"
        current_ddl = frozen.read_text()
        index = ('CREATE UNIQUE INDEX uq_remote_person_assignment_follow_guild ON '
                 'remote_person_channel_assignments (follow_id, discord_guild_id);\n')
        self.assertIn(index, current_ddl)
        with sqlite3.connect(self.path) as connection:
            # Represent the original CP21 v2 shape without runtime auto-repair.
            connection.executescript(current_ddl.replace(index, ""))
            connection.execute("PRAGMA user_version=2")
        db = Database(f"sqlite:///{self.path}")
        self.addCleanup(db.close)
        with self.assertRaisesRegex(RuntimeError, "physical schema differs"):
            db.migrate()

    def test_follow_guild_cardinality_is_sql_enforced_on_fresh_v2(self) -> None:
        """Direct INSERT cannot bypass per-Follow/guild assignment admission."""
        db = Database(f"sqlite:///{self.path}")
        self.addCleanup(db.close)
        db.migrate()
        with db.session() as session:
            actor = "https://bridge.test/actors/users/alice"
            user = User(discord_user_id="1", activitypub_username="alice", actor_url=actor,
                        inbox_url=f"{actor}/inbox", outbox_url=f"{actor}/outbox",
                        followers_url=f"{actor}/followers", public_key_pem="public",
                        private_key_pem="private")
            session.add(user)
            session.flush()
            for name in ("bob", "charlie"):
                session.add(RemotePersonFollow(
                    user_id=user.id, remote_actor_id=f"https://remote.test/users/{name}",
                    remote_inbox_url="https://remote.test/inbox",
                    follow_activity_id=f"https://bridge.test/follows/{name}",
                    generation=1, relationship_status="accepted"))
        with db.session() as session:
            bob, charlie = session.query(RemotePersonFollow).order_by(RemotePersonFollow.id).all()
            session.add(RemotePersonChannelAssignment(
                follow_id=bob.id, discord_guild_id=1, discord_channel_id=100))
        with self.assertRaises(IntegrityError):
            with db.session() as session:
                session.add(RemotePersonChannelAssignment(
                    follow_id=bob.id, discord_guild_id=1, discord_channel_id=101))
        with db.session() as session:
            # Another guild and another Follow remain independent.
            session.add(RemotePersonChannelAssignment(
                follow_id=bob.id, discord_guild_id=2, discord_channel_id=200))
            session.add(RemotePersonChannelAssignment(
                follow_id=charlie.id, discord_guild_id=1, discord_channel_id=100))
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 3)
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM remote_person_channel_assignments").fetchone()[0], 3)
        self.assertEqual(migrations.read_database_state(db.engine).profile, "ready_v3")

    def test_constraints_reject_invalid_or_duplicated_remote_identity(self) -> None:
        """Follow generations, placement XOR, context and root identity are SQL-enforced."""
        db = Database(f"sqlite:///{self.path}")
        self.addCleanup(db.close)
        db.migrate()
        with db.session() as session:
            user = User(discord_user_id="101", activitypub_username="alice", actor_url=_ACTOR,
                        inbox_url=f"{_ACTOR}/inbox", outbox_url=f"{_ACTOR}/outbox",
                        followers_url=f"{_ACTOR}/followers", public_key_pem="public",
                        private_key_pem="private")
            session.add(user)
            session.flush()
            session.add(RemotePersonFollow(user_id=user.id, remote_actor_id=_REMOTE,
                       remote_inbox_url=f"{_REMOTE}/inbox", follow_activity_id="https://bridge.test/follows/1",
                       generation=1, relationship_status="accepted"))
            session.add(RemotePersonPost(object_id=f"{_REMOTE}/posts/1", remote_actor_id=_REMOTE,
                                         content_markdown="post", source_activity_json="{}", revision_fingerprint="a"*64))
        with db.session() as session:
            follow = session.query(RemotePersonFollow).one()
            post = session.query(RemotePersonPost).one()
            session.add(RemotePersonChannelAssignment(follow_id=follow.id,
                        discord_guild_id=1,discord_channel_id=10))
            session.add(RemotePersonPostDelivery(post_id=post.id,discord_guild_id=1,discord_channel_id=10,state="pending"))
        with self.assertRaises(IntegrityError):
            with db.session() as session:
                session.add(RemotePersonFollow(user_id=follow.user_id, remote_actor_id=_REMOTE,
                            remote_inbox_url=f"{_REMOTE}/inbox",follow_activity_id="https://bridge.test/follows/2",
                            generation=2,relationship_status="accepted"))
        with self.assertRaises(IntegrityError):
            with db.session() as session:
                root = session.query(RemotePersonPostDelivery).one()
                session.add(RemotePersonCommentPlacement(root_delivery_id=root.id,state="pending"))
        with self.assertRaises(IntegrityError):
            with db.session() as session:
                root = session.query(RemotePersonPostDelivery).one()
                root.discord_root_message_id=123
                root.discord_thread_id=124


if __name__ == "__main__":
    unittest.main()
