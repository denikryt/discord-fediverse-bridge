"""V1 schema constraints and irreversible upgrade failure scenarios."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from src.db import Database
from src.db.migrations import Stage4IdentityMigrationConfig, read_database_state


HISTORICAL = Path(__file__).parent / "fixtures" / "database"
CONFIG = Stage4IdentityMigrationConfig("https://bridge.example", "bridge")


def _legacy(tmp_path: Path, source: str) -> tuple[Database, Path]:
    """Create exact historical source independently of the current ORM."""
    path = tmp_path / f"{source}.db"
    with sqlite3.connect(path) as connection:
        connection.executescript((HISTORICAL / f"{source}_schema.sql").read_text())
    return Database(f"sqlite:///{path}"), path


@pytest.mark.parametrize("source", ["known_main_v0", "released_v0"])
def test_supported_source_to_v1_preserves_rows_and_noop_restart(tmp_path: Path, source: str) -> None:
    """Valid v0 rows and identities survive exactly one writer transaction."""
    database, path = _legacy(tmp_path, source)
    try:
        database.migrate(CONFIG)
        assert read_database_state(database.engine).profile == "ready_v1"
        with database.engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA user_version").scalar_one() == 1
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        prior = path.read_bytes()
        database.migrate(CONFIG)
        assert path.read_bytes() == prior
    finally:
        database.close()


def test_legacy_group_collision_aborts_without_writing(tmp_path: Path) -> None:
    """Duplicate remote root ownership must not be merged heuristically."""
    database, path = _legacy(tmp_path, "known_main_v0")
    try:
        with database.engine.begin() as connection:
            for rowid in (1, 2):
                connection.execute(text("""
                    INSERT INTO community_thread_groups(id,community_actor_id,ap_object_id,created_at)
                    VALUES(:id, 'https://remote.example/category/1',
                           'https://remote.example/post/1', '2026-01-01 00:00:00')
                """), {"id": rowid})
        prior = path.read_bytes()
        with pytest.raises(RuntimeError, match="duplicate remote thread group"):
            database.migrate(CONFIG)
        assert path.read_bytes() == prior
    finally:
        database.close()


def test_v1_group_owner_tenant_and_per_target_claims(tmp_path: Path) -> None:
    """Only one per-Group canonical remote root, but different Groups may use same AP ID."""
    database = Database(f"sqlite:///{tmp_path / 'empty.db'}")
    try:
        database.migrate()
        with database.engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO community_thread_groups(id,community_actor_id,ap_object_id,
                    source_owner_actor_id,created_at) VALUES
                  (1,'https://remote.example/g/1','https://remote.example/p/1','https://remote.example/a','2026-01-01'),
                  (2,'https://remote.example/g/2','https://remote.example/p/1','https://remote.example/a','2026-01-01')
            """))
            conn.execute(text("""
                INSERT INTO community_message_groups(id,community_actor_id,thread_group_id,ap_object_id,created_at)
                VALUES (1,'https://remote.example/g/1',1,'https://remote.example/c/1','2026-01-01')
            """))
            conn.execute(text("""
                INSERT INTO community_discord_target_claims(thread_group_id,discord_channel_id,kind,status)
                VALUES (1,10,'post','claimed')
            """))
            conn.execute(text("""
                INSERT INTO community_discord_target_claims(message_group_id,discord_channel_id,kind,status)
                VALUES (1,10,'comment','claimed')
            """))
        with pytest.raises(IntegrityError):
            with database.engine.begin() as conn:
                conn.execute(text("""
                    INSERT INTO community_thread_groups(community_actor_id,ap_object_id,created_at)
                    VALUES ('https://remote.example/g/1','https://remote.example/p/1','2026-01-01')
                """))
        with pytest.raises(IntegrityError):
            with database.engine.begin() as conn:
                conn.execute(text("""
                    INSERT INTO community_discord_target_claims(message_group_id,discord_channel_id,kind,status)
                    VALUES (1,11,'post','claimed')
                """))
        with pytest.raises(IntegrityError):
            with database.engine.begin() as conn:
                conn.execute(text("""
                    INSERT INTO community_discord_target_claims(message_group_id,discord_channel_id,kind,status)
                    VALUES (999,11,'comment','claimed')
                """))
    finally:
        database.close()


def test_v1_local_tenant_and_scoped_receipt_independence(tmp_path: Path) -> None:
    """Same AP ID in independent communities is legal, but cross-tenant parent is not."""
    database = Database(f"sqlite:///{tmp_path / 'empty.db'}")
    try:
        database.migrate()
        with database.engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO local_community_threads(id,local_community_id,ap_activity_id,ap_object_id,
                    direction,origin_kind,created_at) VALUES
                (1,1,'https://remote/a','https://remote/o','ap_to_discord','remote','2026-01-01'),
                (2,2,'https://remote/a','https://remote/o','ap_to_discord','remote','2026-01-01')
            """))
            conn.execute(text("""
                INSERT INTO local_community_messages(local_community_id,local_community_thread_id,
                    ap_activity_id,ap_object_id,direction,created_at)
                VALUES (1,1,'https://remote/am','https://remote/om','ap_to_discord','2026-01-01')
            """))
            for group in ('https://remote/g/1','https://remote/g/2'):
                conn.execute(text("""
                    INSERT INTO activitypub_event_receipts(delivery_id,event_type,
                        verified_destination_group_actor_id,object_ap_id,status,created_at,updated_at)
                    VALUES ('https://remote/activity','post.created',:grp,'https://remote/o',
                        'applied','2026-01-01','2026-01-01')
                """), {'grp': group})
            conn.execute(text("""
                INSERT INTO activitypub_event_receipts(delivery_id,event_type,
                    verified_destination_group_actor_id,object_ap_id,status,created_at,updated_at)
                VALUES ('https://remote/activity','post.created',NULL,'https://remote/o',
                    'applied','2026-01-01','2026-01-01')
            """))
        with pytest.raises(IntegrityError):
            with database.engine.begin() as conn:
                conn.execute(text("""
                    INSERT INTO local_community_messages(local_community_id,local_community_thread_id,
                        ap_activity_id,ap_object_id,direction,created_at)
                    VALUES (2,1,'https://remote/evil','https://remote/evil','ap_to_discord','2026-01-01')
                """))
        with pytest.raises(IntegrityError):
            with database.engine.begin() as conn:
                conn.execute(text("""
                    INSERT INTO activitypub_event_receipts(delivery_id,event_type,
                        verified_destination_group_actor_id,object_ap_id,status,created_at,updated_at)
                    VALUES ('https://remote/activity','post.created','https://remote/g/1',
                        'https://remote/o','applied','2026-01-01','2026-01-01')
                """))
    finally:
        database.close()


def test_populated_typed_main_retains_follow_and_original_message_tenant(tmp_path: Path) -> None:
    """One accepted legacy follow and one reply retain original identity after upgrade.

    Historical encoded `lemmy:` selection permits forged numeric provenance, so
    even a matching numeric ID is not independent evidence for `threadiverse`.
    """
    database, path = _legacy(tmp_path, "known_main_v0")
    try:
        with database.engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO users(id,discord_user_id,activitypub_username,actor_url,
                    inbox_url,outbox_url,followers_url,public_key_pem,private_key_pem,
                    profile_version,created_at,updated_at)
                VALUES(2,'discord-2','alice','https://bridge.example/actors/users/alice',
                    'https://bridge.example/actors/users/alice/inbox',
                    'https://bridge.example/actors/users/alice/outbox',
                    'https://bridge.example/actors/users/alice/followers','p','s',0,'2026-01-01','2026-01-01')
            """))
            conn.execute(text("""
                INSERT INTO bridge_actor_keys(id,actor_url,key_id,key_format,algorithm,
                    public_key_data,private_key_data,created_at,updated_at)
                VALUES(1,'https://bridge.example/actors/services/bridge',
                    'https://bridge.example/actors/services/bridge#main-key','pem',
                    'rsa','p','s','2026-01-01','2026-01-01')
            """))
            conn.execute(text("""
                INSERT INTO local_communities(id,discord_guild_id,discord_forum_channel_id,
                    slug,display_name,actor_url,inbox_url,outbox_url,followers_url,
                    public_key_pem,private_key_pem,status,created_at,updated_at)
                VALUES(3,4,5,'public','Public','https://bridge.example/actors/communities/public',
                    'https://bridge.example/actors/communities/public/inbox',
                    'https://bridge.example/actors/communities/public/outbox',
                    'https://bridge.example/actors/communities/public/followers',
                    'p','s','active','2026-01-01','2026-01-01')
            """))
            conn.execute(text("""
                INSERT INTO bridge_actor_follows(id,community_actor_id,follow_activity_id,
                    community_inbox_url,status,created_at,updated_at)
                VALUES(6,'https://forum.example/c/1','https://bridge.example/follows/1',
                    'https://forum.example/c/1/inbox','accepted','2026-01-01','2026-01-01')
            """))
            conn.execute(text("""
                INSERT INTO channel_community_subscriptions(id,discord_channel_id,
                    lemmy_community_actor_id,lemmy_community_id,community_inbox_url,
                    follow_activity_id,status,created_at,updated_at)
                VALUES(7,55,'https://forum.example/c/1',1,'https://forum.example/c/1/inbox',
                    'https://bridge.example/follows/1','accepted','2026-01-01','2026-01-01')
            """))
            conn.execute(text("""
                INSERT INTO local_community_threads(id,local_community_id,
                    ap_activity_id,ap_object_id,direction,origin_kind,created_at)
                VALUES(40,3,'https://other.example/a/root','https://other.example/o/root',
                    'ap_to_discord','remote','2026-01-01')
            """))
            conn.execute(text("""
                INSERT INTO local_community_messages(id,local_community_thread_id,
                    ap_activity_id,ap_object_id,parent_ap_object_id,direction,created_at)
                VALUES(41,40,'https://other.example/a/comment',
                    'https://other.example/o/comment','https://other.example/o/root',
                    'ap_to_discord','2026-01-01')
            """))
        database.migrate(CONFIG)
        with database.engine.connect() as conn:
            assert conn.execute(text("""
                SELECT status,follow_activity_id,remote_software_profile,follow_dispatch_state
                FROM bridge_actor_follows WHERE id=6
            """)).one() == ("accepted", "https://bridge.example/follows/1", "unknown", "sent")
            assert conn.execute(text("""
                SELECT status,follow_activity_id FROM channel_community_subscriptions
                WHERE id=7
            """)).one() == ("accepted", "https://bridge.example/follows/1")
            assert conn.execute(text("""
                SELECT id,local_community_id,local_community_thread_id,ap_object_id,
                    source_owner_actor_id,origin_subscriber_actor_id
                FROM local_community_messages
            """)).one() == (41,3,40,'https://other.example/o/comment',None,None)
        before = path.read_bytes()
        database.migrate(CONFIG)
        assert path.read_bytes() == before
    finally:
        database.close()


def test_legacy_remote_reply_cross_group_parent_rejected_before_upgrade(tmp_path: Path) -> None:
    """An old parent ID cannot silently attach a reply to another remote Group."""
    database, path = _legacy(tmp_path, "known_main_v0")
    try:
        with database.engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO community_thread_groups(id,community_actor_id,ap_object_id,created_at)
                VALUES (10,'https://remote/g/A','https://remote/o/A','2026-01-01')
            """))
            conn.execute(text("""
                INSERT INTO community_message_groups(id,community_actor_id,
                    thread_group_id,ap_object_id,created_at)
                VALUES (12,'https://remote/g/B',10,'https://remote/o/comment','2026-01-01')
            """))
        before = path.read_bytes()
        with pytest.raises(RuntimeError, match="different Group"):
            database.migrate(CONFIG)
        assert path.read_bytes() == before
    finally:
        database.close()
