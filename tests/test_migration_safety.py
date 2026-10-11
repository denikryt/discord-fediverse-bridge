"""File-backed SQLite startup preservation and database version contracts."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import Column, Integer, Table, event, text

from src.db import Database, migrations
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
    database.create_all()
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


@pytest.mark.parametrize("version", [2, -1])
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


@pytest.fixture
def upgraded_metadata(monkeypatch):
    """Represent a future current model and its concrete registered table upgrade."""
    table = Table(
        "migration_probe", Base.metadata, Column("id", Integer, primary_key=True)
    )
    calls = []

    def add_probe(connection):
        """Create the new current-model table with transactional SQLite DDL."""
        calls.append("upgrade")
        table.create(connection)

    monkeypatch.setattr(migrations, "DATABASE_VERSION", 2)
    monkeypatch.setattr(migrations, "MIGRATIONS", {2: add_probe})
    try:
        yield table, calls
    finally:
        Base.metadata.remove(table)


def test_upgrade_precedes_metadata_and_matches_fresh_bootstrap(
    tmp_path, upgraded_metadata
):
    """Versioned upgrade owns new DDL; fresh bootstrap skips historical steps."""
    table, calls = upgraded_metadata
    upgraded = Database(f"sqlite:///{tmp_path / 'upgraded.db'}")
    # Construct the supported previous database before exposing the new model.
    Base.metadata.create_all(
        upgraded.engine,
        tables=[item for item in Base.metadata.sorted_tables if item is not table],
    )
    with upgraded.engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA user_version = 1")
    upgraded.migrate()
    assert calls == ["upgrade"]
    assert _version(upgraded) == 2
    upgraded.migrate()
    assert calls == ["upgrade"]
    fresh = Database(f"sqlite:///{tmp_path / 'fresh-current.db'}")
    fresh.migrate()
    assert calls == ["upgrade"]
    assert _version(fresh) == 2
    assert _snapshot(fresh) == _snapshot(upgraded)
    fresh.close()
    upgraded.close()


def test_unversioned_nonempty_database_is_not_guessed(tmp_path, upgraded_metadata):
    """Later releases reject an unknown baseline, but permit explicit adoption."""
    database = Database(f"sqlite:///{tmp_path / 'unknown.db'}")
    with database.engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE sentinel (value INTEGER)")
    before = _snapshot(database)
    with pytest.raises(RuntimeError, match="baseline"):
        database.migrate()
    assert _version(database) == 0
    assert _snapshot(database) == before
    # An operator's explicit confirmation enables the ordinary upgrade path.
    with database.engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA user_version = 1")
    database.migrate()
    assert _version(database) == 2
    database.close()


def test_failed_upgrade_rolls_back_ddl_dml_and_header_then_retries(
    tmp_path, monkeypatch
):
    """A failing step leaves no partial work; a later successful attempt commits."""
    database = Database(f"sqlite:///{tmp_path / 'rollback.db'}")
    database.migrate()
    with database.engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE sentinel (value INTEGER)")
        connection.exec_driver_sql("INSERT INTO sentinel VALUES (7)")
    before = _snapshot(database)

    def apply_changes(connection):
        """Perform both DDL and DML inside the runner-owned transaction."""
        connection.exec_driver_sql("CREATE TABLE migration_probe (id INTEGER)")
        connection.exec_driver_sql("UPDATE sentinel SET value = 8")

    def fail_after_changes(connection):
        """Inject an error after real changes to exercise physical rollback."""
        apply_changes(connection)
        raise RuntimeError("injected failure")

    monkeypatch.setattr(migrations, "DATABASE_VERSION", 2)
    monkeypatch.setattr(migrations, "MIGRATIONS", {2: fail_after_changes})
    with pytest.raises(RuntimeError, match="injected failure"):
        database.migrate()
    assert _version(database) == 1
    assert _snapshot(database) == before
    monkeypatch.setattr(migrations, "MIGRATIONS", {2: apply_changes})
    database.migrate()
    assert _version(database) == 2
    with database.engine.connect() as connection:
        assert (
            connection.exec_driver_sql("SELECT value FROM sentinel").scalar_one() == 8
        )
    database.close()


@pytest.mark.parametrize(
    "registry", [{}, {3: lambda c: None}, {2: None}, {True: lambda c: None}]
)
def test_invalid_registry_fails_before_any_writes(tmp_path, monkeypatch, registry):
    """Missing, out-of-order, non-callable and baseline registrations are errors."""
    database = Database(f"sqlite:///{tmp_path / 'registry.db'}")
    database.migrate()
    before = _snapshot(database)
    monkeypatch.setattr(migrations, "DATABASE_VERSION", 2)
    monkeypatch.setattr(migrations, "MIGRATIONS", registry)
    with pytest.raises(RuntimeError, match="registry"):
        database.migrate()
    assert _version(database) == 1
    assert _snapshot(database) == before
    database.close()


def test_sequential_upgrades_commit_each_completed_step(tmp_path, monkeypatch):
    """A later error preserves a prior completed step and resumes at the failure."""
    database = Database(f"sqlite:///{tmp_path / 'sequence.db'}")
    database.migrate()

    def first(connection):
        """Install observable work in the first upgrade."""
        connection.exec_driver_sql("CREATE TABLE first_upgrade (value INTEGER)")

    def second(connection):
        """Verify the prior step's version before injecting a later failure."""
        assert connection.exec_driver_sql("PRAGMA user_version").scalar_one() == 2
        connection.exec_driver_sql("CREATE TABLE second_upgrade (value INTEGER)")
        raise RuntimeError("later failure")

    monkeypatch.setattr(migrations, "DATABASE_VERSION", 3)
    monkeypatch.setattr(migrations, "MIGRATIONS", {2: first, 3: second})
    with pytest.raises(RuntimeError, match="later failure"):
        migrations.run_pending_migrations(database.engine)
    assert _version(database) == 2
    with database.engine.connect() as connection:
        assert (
            connection.exec_driver_sql(
                "SELECT count(*) FROM first_upgrade"
            ).scalar_one()
            == 0
        )
        assert (
            connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE name='second_upgrade'"
            ).all()
            == []
        )
    monkeypatch.setattr(
        migrations,
        "MIGRATIONS",
        {
            2: first,
            3: lambda c: c.exec_driver_sql(
                "CREATE TABLE second_upgrade (value INTEGER)"
            ),
        },
    )
    migrations.run_pending_migrations(database.engine)
    assert _version(database) == 3
    database.close()


def test_concurrent_runners_reread_header_under_writer_lock(tmp_path, monkeypatch):
    """Two runners seeing the old version execute one real DDL/DML step once."""
    path = tmp_path / "concurrent.db"
    database = Database(f"sqlite:///{path}")
    database.migrate()
    other = Database(f"sqlite:///{path}")
    barrier = Barrier(2)

    def wait_before_lock(connection, cursor, statement, parameters, context, many):
        """Force both preflight reads to finish before either reserves the writer."""
        if statement == "BEGIN IMMEDIATE":
            barrier.wait(timeout=10)

    for owner in (database, other):
        event.listen(owner.engine, "before_cursor_execute", wait_before_lock)

    def upgrade(connection):
        """DDL fails if invoked twice, making duplicate execution observable."""
        assert connection.connection.driver_connection.in_transaction
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
        connection.exec_driver_sql("CREATE TABLE migration_probe (id INTEGER)")
        connection.exec_driver_sql("INSERT INTO migration_probe VALUES (1)")

    monkeypatch.setattr(migrations, "DATABASE_VERSION", 2)
    monkeypatch.setattr(migrations, "MIGRATIONS", {2: upgrade})
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(migrations.run_pending_migrations, owner.engine)
            for owner in (database, other)
        ]
        for future in futures:
            future.result(timeout=15)
    with database.engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT * FROM migration_probe").all() == [
            (1,)
        ]
    assert _version(database) == 2
    database.close()
    other.close()


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
    """Unmarked baseline adoption and reopening preserve rows, URLs, keys and IDs."""
    path = tmp_path / "federation.db"
    database = Database(f"sqlite:///{path}")
    database.create_all()
    _seed_federation(database)
    with database.engine.begin() as connection:
        connection.exec_driver_sql(f"PRAGMA user_version = {initial_version}")
    before = _snapshot(database)[1]
    database.migrate()
    assert _version(database) == 1
    assert _snapshot(database)[1] == before
    database.close()
    # A new owner exercises an actual reopened database, rather than relying
    # on an in-memory flag or the prior connection pool.
    reopened = Database(f"sqlite:///{path}")
    reopened.migrate()
    assert _version(reopened) == 1
    assert _snapshot(reopened)[1] == before
    with reopened.engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    reopened.close()


def test_bootstrap_failure_leaves_database_unversioned(tmp_path, monkeypatch):
    """Legacy helper failure must not certify a partially prepared baseline."""
    database = Database(f"sqlite:///{tmp_path / 'bootstrap-error.db'}")
    original = migrations.run_legacy_schema_helpers

    def fail_helpers(engine):
        """Inject a helper boundary failure after metadata created the tables."""
        raise RuntimeError("helper failure")

    monkeypatch.setattr(migrations, "run_legacy_schema_helpers", fail_helpers)
    with pytest.raises(RuntimeError, match="helper failure"):
        database.migrate()
    assert _version(database) == 0
    monkeypatch.setattr(migrations, "run_legacy_schema_helpers", original)
    database.migrate()
    assert _version(database) == 1
    database.close()


def test_version_becoming_newer_before_lock_is_rejected(tmp_path, monkeypatch):
    """The authoritative locked reread rejects a concurrently upgraded header."""
    path = tmp_path / "changed-header.db"
    database = Database(f"sqlite:///{path}")
    database.migrate()
    before = _snapshot(database)
    monkeypatch.setattr(migrations, "DATABASE_VERSION", 2)

    def upgrade(connection):
        """Any executed DDL would violate the newly unsupported header."""
        connection.exec_driver_sql("CREATE TABLE forbidden_upgrade (id INTEGER)")

    monkeypatch.setattr(migrations, "MIGRATIONS", {2: upgrade})

    def change_header(connection, cursor, statement, parameters, context, many):
        """Simulate a newer application completing between read and writer lock."""
        if statement == "BEGIN IMMEDIATE":
            with sqlite3.connect(path) as external:
                external.execute("PRAGMA user_version = 3")

    event.listen(database.engine, "before_cursor_execute", change_header)
    with pytest.raises(RuntimeError, match="Unsupported"):
        migrations.run_pending_migrations(database.engine)
    with database.engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA user_version").scalar_one() == 3
    assert _snapshot(database) == before
    database.close()


def test_runner_rejects_unestablished_baseline(tmp_path):
    """Direct runner use cannot silently treat an unversioned database as upgraded."""
    database = Database(f"sqlite:///{tmp_path / 'no-baseline.db'}")
    with pytest.raises(RuntimeError, match="baseline"):
        migrations.run_pending_migrations(database.engine)
    assert _version(database) == 0
    assert _snapshot(database) == ([], {})
    database.close()


def test_failure_after_header_assignment_rolls_back_the_version(tmp_path, monkeypatch):
    """An error after PRAGMA assignment rolls back the header together with DDL."""
    database = Database(f"sqlite:///{tmp_path / 'header-rollback.db'}")
    database.migrate()
    before = _snapshot(database)

    def upgrade(connection):
        """Create observable transactional work before the runner assigns a version."""
        connection.exec_driver_sql("CREATE TABLE migration_probe (id INTEGER)")

    def fail_after_stamp(connection, cursor, statement, parameters, context, many):
        """Fail after SQLite has changed its header but before transaction commit."""
        if statement == "PRAGMA user_version = 2":
            raise RuntimeError("header failure")

    monkeypatch.setattr(migrations, "DATABASE_VERSION", 2)
    monkeypatch.setattr(migrations, "MIGRATIONS", {2: upgrade})
    event.listen(database.engine, "after_cursor_execute", fail_after_stamp)
    with pytest.raises(RuntimeError, match="header failure"):
        database.migrate()
    assert _version(database) == 1
    assert _snapshot(database) == before
    event.remove(database.engine, "after_cursor_execute", fail_after_stamp)
    database.migrate()
    assert _version(database) == 2
    database.close()


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
