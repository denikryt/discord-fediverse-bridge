"""Positive/negative historical v0 data-transfer and one-time owner evidence tests.

This CP04 suite uses the tagged release schema, not a schema inferred from
current ORM. Physical v1 constraints and the transactional runner arrive CP05.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import text

from src.db import Database
from src.db.backup import classify_legacy_sqlite_schema, schema_entries_fingerprint
from src.db.main_v0_admission import inspect_known_main_identities
from src.db.migration_steps.v0_to_v1 import transfer_released_v0_tables
from src.db.migrations import (
    Stage4IdentityMigrationConfig,
    _migrate_stage4_typed_activitypub_identity,
)


_FIXTURES = Path(__file__).parent / "fixtures" / "database"
_CONFIG = Stage4IdentityMigrationConfig(
    public_origin="https://bridge.example",
    service_actor_identifier="bridge",
)
_NOW = "2026-11-11 00:00:00"


def _fixture(tmp_path: Path, name: str) -> tuple[Database, Path]:
    """Load one immutable historical SQL snapshot into a fresh SQLite file."""
    path = tmp_path / f"{name}.db"
    with sqlite3.connect(path) as conn:
        conn.executescript((_FIXTURES / f"{name}_schema.sql").read_text())
    return Database(f"sqlite:///{path}"), path


def _snapshot(conn: object) -> str:
    """Compute the same physical DDL digest used for source admission."""
    return schema_entries_fingerprint(conn.exec_driver_sql("""
        SELECT type,name,tbl_name,sql FROM sqlite_master
        WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name,tbl_name
    """).all())


def _seed_local_actors(conn: object, *, legacy: bool) -> None:
    """Persist actual release-style or typed actor addresses and valid relationships."""
    root = "https://bridge.example"
    user = f"{root}/actors/{'alice' if legacy else 'users/alice'}"
    community = f"{root}/{'communities/public' if legacy else 'actors/communities/public'}"
    service = f"{root}/actors/{'bridge' if legacy else 'services/bridge'}"
    is_main = bool(conn.exec_driver_sql("SELECT COUNT(*) FROM pragma_table_info('users') WHERE name='profile_version'").scalar())
    conn.execute(text(f"""
        INSERT INTO users(id,discord_user_id,activitypub_username,actor_url,inbox_url,
            outbox_url,followers_url,public_key_pem,private_key_pem,created_at,updated_at
            {',profile_version' if is_main else ''})
        VALUES(2,'discord-2','alice',:user,:inbox,:outbox,:followers,'p','s',:now,:now
            {',0' if is_main else ''})
    """), dict(user=user,inbox=f"{user}/inbox",outbox=f"{user}/outbox",
               followers=f"{user}/followers",now=_NOW))
    conn.execute(text("""
        INSERT INTO local_communities(id,discord_guild_id,discord_forum_channel_id,
            slug,display_name,actor_url,inbox_url,outbox_url,followers_url,
            public_key_pem,private_key_pem,status,created_at,updated_at)
        VALUES(3,44,55,'public','Public',:actor,:inbox,:outbox,:followers,
            'pub','priv','active',:now,:now)
    """), dict(actor=community,inbox=f"{community}/inbox",outbox=f"{community}/outbox",
               followers=f"{community}/followers",now=_NOW))
    conn.execute(text("""
        INSERT INTO bridge_actor_keys(id,actor_url,key_id,key_format,algorithm,
            public_key_data,private_key_data,created_at,updated_at)
        VALUES(1,:actor,:key,'pem','rsa','pub','priv',:now,:now)
    """), dict(actor=service,key=f"{service}#main-key",now=_NOW))
    conn.execute(text("""
        INSERT INTO remote_subscribers(id,local_community_id,remote_actor_id,
            remote_inbox_url,follow_activity_id,status,created_at,updated_at)
        VALUES(5,3,'https://remote.example/actors/service','https://remote.example/inbox',
            'https://remote.example/follow/4','accepted',:now,:now)
    """),dict(now=_NOW))
    conn.execute(text("""
        INSERT INTO bridge_actor_follows(id,community_actor_id,follow_activity_id,
            community_inbox_url,status,created_at,updated_at)
        VALUES(6,'https://forum.example/c/1','https://bridge.example/follows/1',
            'https://forum.example/c/1/inbox','accepted',:now,:now)
    """),dict(now=_NOW))
    conn.execute(text("""
        INSERT INTO channel_community_subscriptions(id,discord_channel_id,
            lemmy_community_actor_id,lemmy_community_id,community_inbox_url,
            follow_activity_id,status,created_at,updated_at)
        VALUES(7,555,'https://forum.example/c/1',1,
            'https://forum.example/c/1/inbox','https://bridge.example/follows/1',
            'accepted',:now,:now)
    """),dict(now=_NOW))


def test_tagged_release_data_transfer_preserves_all_old_rows_and_main_ddl(tmp_path: Path) -> None:
    """Copy real release v0 data to the known-main physical schema exactly."""
    database, _path = _fixture(tmp_path, "released_v0")
    try:
        with database.engine.connect() as conn:
            conn.rollback()
            conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
            conn.rollback()
            conn.exec_driver_sql("BEGIN IMMEDIATE")
            _seed_local_actors(conn, legacy=True)
            conn.execute(text("""
                INSERT INTO message_mappings(id,source_platform,source_id,
                    activity_id,object_id,actor_url,community_actor_url,created_at)
                VALUES(8,'discord','msg-8','https://remote.example/activity/8',
                    'https://remote.example/object/8',
                    'https://remote.example/person', 'https://forum.example/c/1', :now)
            """),dict(now=_NOW))
            conn.execute(text("""
                INSERT INTO published_activity_objects(
                    id,actor_username,actor_url,community_actor_url,activity_id,
                    object_id,kind,title,body_markdown,published_at,created_at)
                VALUES(9,'alice','https://bridge.example/actors/alice',
                    'https://forum.example/c/1', 'https://remote.example/a/9',
                    'https://remote.example/o/9','post','Subject','body',:now,:now)
            """),dict(now=_NOW))
            transfer_released_v0_tables(conn)
            profile = classify_legacy_sqlite_schema(conn.exec_driver_sql("""
                SELECT type,name,tbl_name,sql FROM sqlite_master
                WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name,tbl_name
            """).all())
            assert profile == "known_main_v0"
            assert conn.execute(text("SELECT COUNT(*) FROM users")).scalar_one() == 1
            assert conn.execute(text("SELECT id,profile_version FROM users")).one() == (2,0)
            assert conn.execute(text("""
                SELECT id,publication_context,conversation_root_object_id
                FROM published_activity_objects
            """)).one() == (9,"community","https://remote.example/o/9")
            assert conn.execute(text("""
                SELECT id,publication_context FROM message_mappings
            """)).one() == (8,"community")
            assert conn.execute(text("SELECT id,status FROM remote_subscribers")).one() == (5,"accepted")
            assert conn.execute(text("SELECT id,follow_activity_id,status FROM bridge_actor_follows")).one() == (
                6,"https://bridge.example/follows/1","accepted")
            assert conn.exec_driver_sql("PRAGMA foreign_key_check").all() == []
            conn.rollback()
    finally:
        database.close()


def test_typed_main_cutover_preserves_unaffected_accepted_relations(tmp_path: Path) -> None:
    """A verified typed source must never purge already accepted subscription rows."""
    database, _path = _fixture(tmp_path, "known_main_v0")
    try:
        with database.engine.begin() as conn:
            _seed_local_actors(conn, legacy=False)
            conn.execute(text("""
                INSERT INTO user_actor_followers(id,user_id,remote_actor_id,
                    remote_inbox_url,follow_activity_id,status,
                    owner_notification_state,created_at,updated_at)
                VALUES(8,2,'https://follower.example/person',
                    'https://follower.example/inbox','https://follower.example/follow',
                    'accepted','pending',:now,:now)
            """),dict(now=_NOW))
        with database.engine.begin() as conn:
            source_subscriber = conn.execute(text("SELECT * FROM remote_subscribers")).one()
            source_follow = conn.execute(text("SELECT * FROM bridge_actor_follows")).one()
            source_user_follower = conn.execute(text("SELECT * FROM user_actor_followers")).one()
            evidence = inspect_known_main_identities(conn, _CONFIG)
            assert evidence.state == "typed"
            assert not evidence.changed_local_community_ids
            assert not evidence.service_actor_changed
            _migrate_stage4_typed_activitypub_identity(conn, _CONFIG)
            assert conn.execute(text("SELECT * FROM remote_subscribers")).one() == source_subscriber
            assert conn.execute(text("SELECT * FROM bridge_actor_follows")).one() == source_follow
            assert conn.execute(text("SELECT * FROM user_actor_followers")).one() == source_user_follower
            assert conn.execute(text("SELECT id,status FROM remote_subscribers")).one() == (5,"accepted")
            assert conn.execute(text("SELECT id,status,follow_activity_id FROM bridge_actor_follows")).one() == (
                6,"accepted","https://bridge.example/follows/1")
            assert conn.execute(text("""
                SELECT status,follow_activity_id FROM channel_community_subscriptions
            """)).one() == ("accepted","https://bridge.example/follows/1")
    finally:
        database.close()


def test_legacy_main_cutover_invalidates_only_changed_owner_relations(tmp_path: Path) -> None:
    """Source-owned follows cannot survive actor ID changes, but rows stay retriable."""
    database, _path = _fixture(tmp_path, "known_main_v0")
    try:
        with database.engine.begin() as conn:
            _seed_local_actors(conn, legacy=True)
            conn.execute(text("""
                INSERT INTO user_actor_followers(id,user_id,remote_actor_id,
                    remote_inbox_url,follow_activity_id,status,
                    owner_notification_state,created_at,updated_at)
                VALUES(8,2,'https://follower.example/person',
                    'https://follower.example/inbox','https://follower.example/follow',
                    'accepted','pending',:now,:now)
            """),dict(now=_NOW))
        with database.engine.begin() as conn:
            evidence = inspect_known_main_identities(conn, _CONFIG)
            assert evidence.state == "legacy"
            assert evidence.changed_local_community_ids == (3,)
            assert evidence.service_actor_changed
            _migrate_stage4_typed_activitypub_identity(conn, _CONFIG)
            assert conn.execute(text("SELECT COUNT(*) FROM remote_subscribers")).scalar_one() == 0
            assert conn.execute(text("SELECT COUNT(*) FROM user_actor_followers")).scalar_one() == 0
            assert conn.execute(text("SELECT id,status,follow_activity_id FROM bridge_actor_follows")).one() == (
                6,"failed",None)
            assert conn.execute(text("""
                SELECT id,status,follow_activity_id FROM channel_community_subscriptions
            """)).one() == (7,"failed",None)
            assert conn.execute(text("SELECT actor_url FROM users")).scalar_one() == (
                "https://bridge.example/actors/users/alice")
            assert conn.execute(text("SELECT actor_url FROM local_communities")).scalar_one() == (
                "https://bridge.example/actors/communities/public")
            assert conn.execute(text("SELECT actor_url FROM bridge_actor_keys")).scalar_one() == (
                "https://bridge.example/actors/services/bridge")
    finally:
        database.close()


@pytest.mark.parametrize("attack", ["mixed_namespaces","foreign_actor","changed_endpoint","wrong_key"])
def test_known_main_rejects_unverifiable_legacy_identity(
    tmp_path: Path,attack: str
) -> None:
    """No unsupported or partial identity evidence is adopted into v1."""
    database, path = _fixture(tmp_path, "known_main_v0")
    try:
        with database.engine.begin() as conn:
            _seed_local_actors(conn, legacy=True)
            if attack == "mixed_namespaces":
                conn.execute(text("""
                    UPDATE bridge_actor_keys SET actor_url=:actor,key_id=:key
                """),dict(actor="https://bridge.example/actors/services/bridge",
                          key="https://bridge.example/actors/services/bridge#main-key"))
            elif attack == "foreign_actor":
                conn.execute(text("UPDATE users SET actor_url='https://evil.example/actors/alice'"))
            elif attack == "changed_endpoint":
                conn.execute(text("UPDATE local_communities SET inbox_url='https://evil.example/inbox'"))
            else:
                conn.execute(text("UPDATE bridge_actor_keys SET key_id='https://other.example/key'"))
        before = path.read_bytes()
        with database.engine.connect() as conn:
            with pytest.raises(RuntimeError):
                inspect_known_main_identities(conn, _CONFIG)
        assert path.read_bytes() == before
    finally:
        database.close()



@pytest.mark.parametrize("legacy", [True, False])
def test_stored_object_identity_must_match_owner_cutover_state(
    tmp_path: Path, legacy: bool
) -> None:
    """A typed main owner cannot retain old AP objects, nor vice versa."""
    database, path = _fixture(tmp_path, "known_main_v0")
    try:
        with database.engine.begin() as conn:
            _seed_local_actors(conn, legacy=legacy)
            actor = (
                "https://bridge.example/actors/users/alice" if legacy
                else "https://bridge.example/actors/alice"
            )
            conn.execute(text("""
                INSERT INTO message_mappings(id,source_platform,source_id,
                    activity_id,object_id,actor_url,publication_context,
                    community_actor_url,created_at)
                VALUES(40,'activitypub','remote-40','https://remote.example/a/40',
                    'https://remote.example/o/40',:actor,'community',
                    'https://forum.example/c/1',:now)
            """), dict(actor=actor,now=_NOW))
        before = path.read_bytes()
        with database.engine.connect() as conn:
            with pytest.raises(RuntimeError, match="object identity"):
                inspect_known_main_identities(conn, _CONFIG)
        assert path.read_bytes() == before
    finally:
        database.close()


def test_outgoing_follow_without_verified_service_key_is_not_adopted(tmp_path: Path) -> None:
    """A remote Follow row cannot prove the sending Service's AP key owner."""
    database, _path = _fixture(tmp_path, "known_main_v0")
    try:
        with database.engine.begin() as conn:
            _seed_local_actors(conn, legacy=True)
            conn.execute(text("DELETE FROM bridge_actor_keys"))
        with database.engine.connect() as conn:
            with pytest.raises(RuntimeError, match="lacks original Service key"):
                inspect_known_main_identities(conn, _CONFIG)
    finally:
        database.close()


def test_no_local_actors_and_no_relationships_remains_identity_neutral(tmp_path: Path) -> None:
    """A known physical schema with no local identity has no cutover to invent."""
    database, _path = _fixture(tmp_path, "known_main_v0")
    try:
        with database.engine.connect() as conn:
            assert inspect_known_main_identities(conn, None).state == "empty"
            assert inspect_known_main_identities(conn, _CONFIG).state == "empty"
    finally:
        database.close()

def test_release_transfer_errors_roll_back_all_schema_and_data(tmp_path: Path) -> None:
    """Invalid source leaves a physical version-0 source after rollback."""
    database, path = _fixture(tmp_path, "released_v0")
    try:
        old = path.read_bytes()
        with database.engine.connect() as conn:
            conn.rollback()
            conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
            conn.rollback()
            conn.exec_driver_sql("BEGIN IMMEDIATE")
            transfer_released_v0_tables(conn)
            assert conn.exec_driver_sql("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").scalar() == 43
            conn.rollback()
        with sqlite3.connect(path) as conn:
            assert conn.execute("PRAGMA user_version").fetchone() == (0,)
            assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchone() == (30,)
        assert path.read_bytes() == old
    finally:
        database.close()
