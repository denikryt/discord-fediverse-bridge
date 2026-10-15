"""File-backed SQLite startup preservation and database version contracts."""

from __future__ import annotations

import sqlite3
import runpy
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import Column, Integer, Table, event, text

from src.db import Database, migrations
from test_legacy_v0_to_v1_migration import legacy, _seed
from support.historical_platform_probe import upgrade_historical_probe

BASE_REGISTRY = dict(migrations.MIGRATIONS)

from src.models import (
    Base,
    BridgeActorFollow,
    ChannelCommunitySubscription,
    MessageMapping,
    PersonalFeedThreadMapping,
    PublishedActivityObject,
    UserActorFollowActivityId,
    UserActorOutboundSource,
    UserActorOutboundDelivery,
    UserActorOutboundDeliveryFollower,
)


def test_new_follower_survives_repeated_migrate(tmp_path):
    """A Follow accepted after bootstrap remains durable across another startup."""
    database = Database(f"sqlite:///{tmp_path / 'followers.db'}")
    database.migrate()
    actor = "https://bridge.example/actors/users/alice"
    user = database.users.create_user(
        discord_user_id="1",
        activitypub_username="alice",
        actor_url=actor,
        inbox_url=f"{actor}/inbox",
        outbox_url=f"{actor}/outbox",
        followers_url=f"{actor}/followers",
        public_key_pem="public",
        private_key_pem="private",
    )
    with database.session() as session:
        database.user_actor_followers.create_or_refresh(
            session,
            user_id=user.id,
            remote_actor_id="https://remote.example/u/bob",
            remote_inbox_url="https://remote.example/inbox",
            remote_shared_inbox_url=None,
            follow_activity_id="https://remote.example/follows/1",
        )
    # The relationship and its global Follow-ID ledger must survive together.
    database.migrate()
    with database.engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT count(*) FROM user_actor_followers")
            ).scalar_one()
            == 1
        )
        assert (
            connection.execute(
                text("SELECT count(*) FROM user_actor_follow_activity_ids")
            ).scalar_one()
            == 1
        )
    database.close()


def _version(database):
    """Read the actual SQLite header through a real connection."""
    with database.engine.connect() as connection:
        return connection.exec_driver_sql("PRAGMA user_version").scalar_one()


def _snapshot(database):
    """Capture schema and all user rows, ignoring only the database version."""
    with database.engine.connect() as connection:
        schema = connection.exec_driver_sql(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ).all()
        rows = {
            name: connection.exec_driver_sql(
                f'SELECT * FROM "{name}" ORDER BY rowid'
            ).all()
            for kind, name, _, _ in schema
            if kind == "table"
        }
        return schema, rows


def test_fresh_database_bootstraps_once(tmp_path):
    """Direct migrate creates the complete fresh schema and stamps the baseline."""
    database = Database(f"sqlite:///{tmp_path / 'fresh.db'}")
    database.migrate()
    assert _version(database) == 1
    before = _snapshot(database)
    database.migrate()
    assert _version(database) == 1
    assert _snapshot(database) == before
    database.close()


@pytest.mark.parametrize("version", [3, -1])
def test_unsupported_version_rejected_before_schema_writes(tmp_path, version):
    """Invalid headers are rejected while preserving all existing schema and data."""
    database = Database(f"sqlite:///{tmp_path / 'unsupported.db'}")
    with database.engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE sentinel (value INTEGER)")
        connection.exec_driver_sql("INSERT INTO sentinel VALUES (7)")
        connection.exec_driver_sql(f"PRAGMA user_version = {version}")
    before = _snapshot(database)
    with pytest.raises(RuntimeError):
        database.migrate()
    assert _version(database) == version
    assert _snapshot(database) == before
    database.close()


def test_unversioned_nonempty_database_is_not_guessed(tmp_path):
    """Unpublished historical input is rejected with either header; stamping cannot adopt it."""
    path = tmp_path / "unknown.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            (Path(__file__).parent / "fixtures/platform_v1_schema.sql").read_text()
        )
        connection.execute("PRAGMA user_version=0")
    database = Database(f"sqlite:///{path}")
    before = _snapshot(database)
    with pytest.raises(RuntimeError, match="Unsupported"):
        database.migrate()
    assert _version(database) == 0 and _snapshot(database) == before
    with database.engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA user_version=1")
    with pytest.raises(RuntimeError, match="current v1"):
        database.migrate()
    assert _version(database) == 1 and _snapshot(database) == before
    database.close()


def test_failed_upgrade_rolls_back_ddl_dml_and_header_then_retries(legacy, monkeypatch):
    """A registered single-step fault undoes real DDL and user changes before retry."""
    _seed(legacy)
    before = _snapshot(legacy)
    real = migrations.MIGRATIONS[1]

    def fail(connection, identity=None):
        """Write under the real reserved transaction, then abort before final checks."""
        connection.exec_driver_sql("CREATE TABLE migration_probe(id INTEGER)")
        connection.exec_driver_sql("UPDATE users SET activitypub_username='changed'")
        raise RuntimeError("injected failure")

    monkeypatch.setattr(migrations, "MIGRATIONS", {1: fail})
    with pytest.raises(RuntimeError, match="injected failure"):
        legacy.migrate()
    assert _version(legacy) == 0 and _snapshot(legacy) == before
    monkeypatch.setattr(migrations, "MIGRATIONS", {1: real})
    legacy.migrate()
    assert _version(legacy) == 1


@pytest.mark.parametrize(
    "registry", [{}, {3: lambda c: None}, {2: None}, {True: lambda c: None}]
)
def test_invalid_registry_fails_before_any_writes(tmp_path, monkeypatch, registry):
    """Missing, out-of-order, non-callable and baseline registrations are errors."""
    database = Database(f"sqlite:///{tmp_path / 'registry.db'}")
    database.migrate()
    before = _snapshot(database)
    monkeypatch.setattr(migrations, "DATABASE_VERSION", 1)
    monkeypatch.setattr(migrations, "MIGRATIONS", registry)
    with pytest.raises(RuntimeError, match="registry"):
        database.migrate()
    assert _version(database) == 1
    assert _snapshot(database) == before
    database.close()


def test_both_internal_phases_share_unstamped_transaction(legacy):
    """A reader sees released v0 while normalization/backfill share the writer lock."""
    _seed(legacy)
    seen = []

    def inspect(connection, cursor, statement, parameters, context, many):
        """Observe from an independent connection at the first platform DDL."""
        if statement.strip().startswith("CREATE TABLE platform_connectors"):
            assert connection.connection.driver_connection.in_transaction
            assert connection.exec_driver_sql("PRAGMA user_version").scalar_one() == 0
            with sqlite3.connect(legacy.engine.url.database) as outside:
                assert outside.execute("PRAGMA user_version").fetchone()[0] == 0
                assert (
                    outside.execute(
                        "SELECT count(*) FROM sqlite_master WHERE type='table'"
                    ).fetchone()[0]
                    == 30
                )
            seen.append(True)

    event.listen(legacy.engine, "before_cursor_execute", inspect)
    legacy.migrate()
    assert seen == [True] and _version(legacy) == 1


def test_current_runner_allows_an_existing_writer(tmp_path):
    """Current-version reads succeed while another real SQLite writer is reserved."""
    path = tmp_path / "read-only.db"
    database = Database(f"sqlite:///{path}")
    database.migrate()
    writer = sqlite3.connect(path)
    try:
        writer.execute("BEGIN IMMEDIATE")
        migrations.run_pending_migrations(database.engine)
        assert _version(database) == 1
    finally:
        writer.rollback()
        writer.close()
        database.close()


def _seed_federation(database):
    """Persist accepted relationships, typed identities, keys and mapped deliveries."""
    old = runpy.run_path(str(Path(__file__).parent / "fixtures/platform_v1_models.py"))
    BridgeActorFollow = old["BridgeActorFollow"]
    ChannelCommunitySubscription = old["ChannelCommunitySubscription"]
    MessageMapping = old["MessageMapping"]
    PublishedActivityObject = old["PublishedActivityObject"]
    PersonalFeedThreadMapping = old["PersonalFeedThreadMapping"]
    UserActorFollowActivityId = old["UserActorFollowActivityId"]
    UserActorOutboundSource = old["UserActorOutboundSource"]
    UserActorOutboundDelivery = old["UserActorOutboundDelivery"]
    UserActorOutboundDeliveryFollower = old["UserActorOutboundDeliveryFollower"]
    now = "2026-06-21T12:00:00Z"
    with database.engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (id, discord_user_id, activitypub_username, actor_url, inbox_url, outbox_url, followers_url, public_key_pem, private_key_pem, profile_version, created_at, updated_at) VALUES (1, '1', 'alice', 'https://bridge.example/actors/users/alice', 'https://bridge.example/actors/users/alice/inbox', 'https://bridge.example/actors/users/alice/outbox', 'https://bridge.example/actors/users/alice/followers', 'public', 'private', 3, :now, :now)"
            ),
            {"now": now},
        )
        connection.execute(
            text(
                "INSERT INTO local_communities (id, discord_guild_id, discord_forum_channel_id, slug, display_name, actor_url, inbox_url, outbox_url, followers_url, public_key_pem, private_key_pem, status, created_at, updated_at) VALUES (2, 10, 20, 'python', 'Python', 'https://bridge.example/actors/communities/python', 'https://bridge.example/actors/communities/python/inbox', 'https://bridge.example/actors/communities/python/outbox', 'https://bridge.example/actors/communities/python/followers', 'public', 'private', 'active', :now, :now)"
            ),
            {"now": now},
        )
        connection.execute(
            text(
                "INSERT INTO bridge_actor_keys (id, actor_url, key_id, key_format, algorithm, public_key_data, private_key_data, created_at, updated_at) VALUES (1, 'https://bridge.example/actors/services/bridge', 'https://bridge.example/actors/services/bridge#main-key', 'jwk', 'RSASSA-PKCS1-v1_5', '{}', '{}', :now, :now)"
            ),
            {"now": now},
        )
        connection.execute(
            text(
                "INSERT INTO local_community_threads (id, local_community_id, ap_activity_id, ap_object_id, direction, origin_kind, created_at) VALUES (5, 2, 'https://bridge.example/actors/communities/python/activities/create/posts/thread-1', 'https://bridge.example/actors/communities/python/objects/posts/thread-1', 'outbound', 'discord', :now)"
            ),
            {"now": now},
        )
        connection.execute(
            text(
                "INSERT INTO local_community_thread_surfaces (id, local_community_thread_id, discord_forum_channel_id, discord_thread_id, discord_starter_message_id, local_subscriber_id, role, created_at) VALUES (6, 5, 20, 30, 40, NULL, 'host', :now)"
            ),
            {"now": now},
        )
        connection.execute(
            text(
                "INSERT INTO local_community_messages (id, local_community_thread_id, ap_activity_id, ap_object_id, parent_ap_object_id, direction, created_at) VALUES (7, 5, 'https://bridge.example/actors/communities/python/activities/create/comments/msg-1', 'https://bridge.example/actors/communities/python/objects/comments/msg-1', 'https://bridge.example/actors/communities/python/objects/posts/thread-1', 'outbound', :now)"
            ),
            {"now": now},
        )
        connection.execute(
            text(
                "INSERT INTO local_community_message_surfaces (id, local_community_message_id, local_community_thread_surface_id, discord_forum_channel_id, discord_message_id, parent_discord_message_id, local_subscriber_id, role, created_at) VALUES (8, 7, 6, 20, 50, 40, NULL, 'host', :now)"
            ),
            {"now": now},
        )
        connection.execute(
            text(
                "INSERT INTO user_actor_followers (id, user_id, remote_actor_id, remote_inbox_url, follow_activity_id, status, owner_notification_state, created_at, updated_at) VALUES (9, 1, 'https://remote.example/actors/users/bob', 'https://remote.example/inbox', 'https://remote.example/follows/1', 'accepted', 'pending', :now, :now)"
            ),
            {"now": now},
        )
        connection.execute(
            text(
                "INSERT INTO remote_subscribers (id, local_community_id, remote_actor_id, remote_inbox_url, follow_activity_id, status, created_at, updated_at) VALUES (10, 2, 'https://remote.example/u/bob', 'https://remote.example/inbox', 'https://remote.example/follows/2', 'accepted', :now, :now)"
            ),
            {"now": now},
        )

        # Core inserts apply model defaults, while the raw snapshot preserves
        # every column rather than only the originally reported follower count.
        connection.execute(
            UserActorFollowActivityId.__table__.insert().values(
                follow_activity_id="https://remote.example/follows/1",
                user_id=1,
                remote_actor_id="https://remote.example/actors/users/bob",
            )
        )
        connection.execute(
            BridgeActorFollow.__table__.insert().values(
                community_actor_id="https://remote.example/c/python",
                follow_activity_id="https://bridge.example/actors/services/bridge/follows/1",
                community_inbox_url="https://remote.example/inbox",
                status="accepted",
            )
        )
        connection.execute(
            ChannelCommunitySubscription.__table__.insert().values(
                discord_channel_id=99,
                discord_guild_id=10,
                lemmy_community_actor_id="https://remote.example/c/python",
                follow_activity_id="https://bridge.example/actors/services/bridge/follows/1",
                status="accepted",
            )
        )
        actor = "https://bridge.example/actors/users/alice"
        activity = f"{actor}/activities/create/posts/content"
        object_id = f"{actor}/objects/posts/content"
        connection.execute(
            PublishedActivityObject.__table__.insert().values(
                id=1,
                actor_username="alice",
                actor_url=actor,
                publication_context="personal_feed",
                community_actor_url=None,
                activity_id=activity,
                object_id=object_id,
                kind="post",
                body_markdown="durable body",
                conversation_root_object_id=object_id,
                discord_guild_id=10,
                discord_channel_id=20,
                discord_message_id=100,
            )
        )
        connection.execute(
            MessageMapping.__table__.insert().values(
                source_platform="discord",
                source_id="100",
                activity_id=activity,
                object_id=object_id,
                actor_url=actor,
                publication_context="personal_feed",
                community_actor_url=None,
                discord_guild_id=10,
                discord_channel_id=20,
                discord_message_id=100,
            )
        )
        connection.execute(
            PersonalFeedThreadMapping.__table__.insert().values(
                root_published_object_id=1,
                root_discord_channel_id=20,
                root_discord_message_id=100,
                thread_id=100,
            )
        )
        connection.execute(
            UserActorOutboundSource.__table__.insert().values(
                id=1,
                user_id=1,
                source_kind="content_create",
                ordering_key="content:1",
                logical_version=1,
                activity_id=activity,
                published_object_id=1,
                snapshot_json='{"body":"durable body"}',
                activity_json='{"type":"Create"}',
                state="ready",
            )
        )
        connection.execute(
            UserActorOutboundDelivery.__table__.insert().values(
                id=1,
                source_id=1,
                target_endpoint_url="https://remote.example/inbox",
                status="failed",
                attempt_count=2,
                last_error="retry retained",
            )
        )
        connection.execute(
            UserActorOutboundDeliveryFollower.__table__.insert().values(
                delivery_id=1,
                relationship_id_snapshot=9,
                remote_actor_id="https://remote.example/actors/users/bob",
                snapshotted_endpoint_url="https://remote.example/inbox",
            )
        )


@pytest.mark.parametrize("initial_version", [0, 1])
def test_baseline_and_restart_preserve_complete_federation_snapshot(
    tmp_path, initial_version
):
    """Isolated internal phase preserves identity/state; public unversioned adoption is rejected."""
    path = tmp_path / "federation.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            (Path(__file__).parent / "fixtures/platform_v1_schema.sql").read_text()
        )
    database = Database(f"sqlite:///{path}")
    _seed_federation(database)
    with database.engine.begin() as connection:
        connection.exec_driver_sql(f"PRAGMA user_version={initial_version}")
    before = _snapshot(database)
    if initial_version == 0:
        with pytest.raises(RuntimeError, match="Unsupported"):
            database.migrate()
        assert _snapshot(database) == before and _version(database) == 0
        database.close()
        return
    with database.engine.connect() as connection:
        columns = {
            name: [
                r[1]
                for r in connection.exec_driver_sql(f'PRAGMA table_info("{name}")')
                if name != "local_communities"
                or r[1] not in {"discord_guild_id", "discord_forum_channel_id"}
            ]
            for name in before[1]
        }
        preserved = {
            name: connection.exec_driver_sql(
                f'SELECT {",".join(columns[name])} FROM "{name}" ORDER BY rowid'
            ).all()
            for name in columns
        }
    upgrade_historical_probe(database)
    assert _version(database) == 1
    after = _snapshot(database)
    database.close()
    reopened = Database(f"sqlite:///{path}")
    reopened.migrate()
    assert _version(reopened) == 1 and _snapshot(reopened) == after
    with reopened.engine.connect() as connection:
        for name in columns:
            assert (
                connection.exec_driver_sql(
                    f'SELECT {",".join(columns[name])} FROM "{name}" ORDER BY rowid'
                ).all()
                == preserved[name]
            )
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    reopened.close()


def test_bootstrap_failure_leaves_database_unversioned(tmp_path, monkeypatch):
    """Fresh schema-object failure must roll back bootstrap and its header."""
    database = Database(f"sqlite:///{tmp_path / 'bootstrap-error.db'}")
    original = migrations._install_current_schema_objects

    def fail_helpers(engine):
        """Inject a helper boundary failure after metadata created the tables."""
        raise RuntimeError("helper failure")

    monkeypatch.setattr(migrations, "_install_current_schema_objects", fail_helpers)
    with pytest.raises(RuntimeError, match="helper failure"):
        database.migrate()
    assert _version(database) == 0
    monkeypatch.setattr(migrations, "_install_current_schema_objects", original)
    database.migrate()
    assert _version(database) == 1
    database.close()


def test_version_becoming_newer_before_lock_is_rejected(legacy):
    """Locked reread rejects a future winner without normalization or header downgrade."""
    before = _snapshot(legacy)

    def change_header(connection, cursor, statement, parameters, context, many):
        """Change only the external header between preflight and lock acquisition."""
        if statement == "BEGIN IMMEDIATE":
            with sqlite3.connect(legacy.engine.url.database) as outside:
                outside.execute("PRAGMA user_version=2")

    event.listen(legacy.engine, "before_cursor_execute", change_header)
    with pytest.raises(RuntimeError, match="Unsupported"):
        legacy.migrate()
    assert _version(legacy) == 2 and _snapshot(legacy) == before


def test_runner_bootstraps_empty_source(tmp_path):
    """Direct runner and Database.migrate share the same atomic empty-file contract."""
    db = Database(f"sqlite:///{tmp_path / 'empty.db'}")
    migrations.run_pending_migrations(db.engine)
    assert _version(db) == 1
    db.close()


def test_failure_after_header_assignment_rolls_back_the_version(legacy):
    """Header assignment stays transactional even after both schema transformations."""
    before = _snapshot(legacy)

    def fail(connection, cursor, statement, parameters, context, many):
        """Abort after the only target stamp and before its transaction commits."""
        if statement == "PRAGMA user_version = 1":
            raise RuntimeError("header failure")

    event.listen(legacy.engine, "after_cursor_execute", fail)
    with pytest.raises(RuntimeError, match="header failure"):
        legacy.migrate()
    assert _version(legacy) == 0 and _snapshot(legacy) == before
    event.remove(legacy.engine, "after_cursor_execute", fail)
    legacy.migrate()
    assert _version(legacy) == 1


@pytest.mark.parametrize("target", [0, -1, True, 1.5, 2147483648])
def test_invalid_target_version_is_rejected_before_bootstrap(
    tmp_path, monkeypatch, target
):
    """Invalid registry targets cannot create schema or stamp SQLite's header."""
    database = Database(f"sqlite:///{tmp_path / 'invalid-target.db'}")
    monkeypatch.setattr(migrations, "DATABASE_VERSION", target)
    with pytest.raises(RuntimeError, match="registry"):
        database.migrate()
    assert _version(database) == 0
    assert _snapshot(database) == ([], {})
    database.close()
