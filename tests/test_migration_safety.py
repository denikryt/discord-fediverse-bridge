"""File-backed SQLite startup preservation and database version contracts."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import event, text

from src.db import Database, migrations
from test_legacy_v0_to_v1_migration import legacy, _seed

BASE_REGISTRY = dict(migrations.MIGRATIONS)


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
            (
                Path(__file__).parent / "fixtures/unsupported_prerelease_schema.sql"
            ).read_text()
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


def test_direct_transfer_shares_unstamped_transaction(legacy):
    """A reader sees released v0 while target creation and transfer share the writer lock."""
    _seed(legacy)
    seen = []

    def inspect(connection, cursor, statement, parameters, context, many):
        """Observe from an independent connection at the first target DDL."""
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


def _seed_current_federation_confirmations(database):
    """Retain accepted followers, Follow ledger, service keys, claims and retry metadata on v1."""
    from src.models import (
        User,
        BridgeActorKey,
        LocalCommunity,
        LocalSubscriber,
        RemoteSubscriber,
        ChannelCommunitySubscription,
        BridgeActorFollow,
        UserActorOutboundDelivery,
    )

    with database.immediate_session() as session:
        user = session.query(User).one()
        user.profile_version = 3
        service = "https://bridge.test/actors/services/bridge"
        session.add(
            BridgeActorKey(
                actor_url=service,
                key_id=service + "#main-key",
                key_format="jwk",
                algorithm="RSASSA-PKCS1-v1_5",
                public_key_data="{}",
                private_key_data="{}",
            )
        )
        database.user_actor_followers.create_or_refresh(
            session,
            user_id=user.id,
            remote_actor_id="https://remote.test/u/bob",
            remote_inbox_url="https://remote.test/inbox",
            remote_shared_inbox_url=None,
            follow_activity_id="https://remote.test/follows/1",
        )
        actor = "https://bridge.test/actors/communities/news"
        community = LocalCommunity(
            slug="news",
            display_name="News",
            actor_url=actor,
            inbox_url=actor + "/inbox",
            outbox_url=actor + "/outbox",
            followers_url=actor + "/followers",
            public_key_pem="community-public",
            private_key_pem="community-private",
        )
        session.add(community)
        session.flush()
        subscriber = LocalSubscriber(
            local_community_id=community.id, discord_guild_id=1, discord_channel_id=991
        )
        follow_id = service + "/activities/follow/stable"
        subscription = ChannelCommunitySubscription(
            discord_guild_id=1,
            discord_channel_id=992,
            lemmy_community_actor_id="https://remote.test/c/news",
            status="accepted",
            follow_activity_id=follow_id,
        )
        session.add_all(
            [
                subscriber,
                subscription,
                RemoteSubscriber(
                    local_community_id=community.id,
                    remote_actor_id="https://remote.test/u/bob",
                    remote_inbox_url="https://remote.test/inbox",
                    follow_activity_id="https://remote.test/follows/community",
                ),
                BridgeActorFollow(
                    community_actor_id="https://remote.test/c/news",
                    follow_activity_id=follow_id,
                    status="accepted",
                ),
            ]
        )
        session.flush()
        repo = database.platform_surfaces
        host = repo.ensure_channel(
            session,
            connector_key="discord-primary",
            external_channel_id="990",
            external_guild_id="1",
            kind="forum",
        )
        repo.claim_local_host(session, host.id, community.id)
        local = repo.ensure_channel(
            session,
            connector_key="discord-primary",
            external_channel_id="991",
            external_guild_id="1",
            kind="forum",
        )
        repo.claim_local_subscriber(session, local.id, subscriber.id)
        remote = repo.ensure_channel(
            session,
            connector_key="discord-primary",
            external_channel_id="992",
            external_guild_id="1",
            kind="forum",
        )
        repo.claim_remote_subscription(session, remote.id, subscription.id)
        delivery = session.query(UserActorOutboundDelivery).one()
        delivery.status = "failed"
        delivery.attempt_count = 2
        delivery.last_error = "retry retained"


@pytest.mark.parametrize("source", ["release", "current"])
def test_restart_preserves_complete_federation_snapshot(tmp_path, source):
    """Supported release/current state survives migration and a fresh process-style reopen."""
    from test_platform_data_model_migration import (
        _released_database,
        _current_database,
        _seed_cascade_graph,
    )

    path = tmp_path / "federation.db"
    db = _released_database(path) if source == "release" else _current_database(path)
    (_seed if source == "release" else _seed_cascade_graph)(db)
    if source == "current":
        _seed_current_federation_confirmations(db)
    with db.engine.connect() as c:
        columns = {
            name: [
                row[1]
                for row in c.exec_driver_sql(f'PRAGMA table_info("{name}")')
                if name != "local_communities"
                or row[1] not in {"discord_guild_id", "discord_forum_channel_id"}
            ]
            for (name,) in c.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT GLOB 'sqlite_*'"
            )
        }
        before = {
            name: c.exec_driver_sql(
                f'SELECT {",".join(cols)} FROM "{name}" ORDER BY id'
            ).all()
            for name, cols in columns.items()
        }
    db.migrate()
    after = _snapshot(db)
    db.close()
    reopened = Database(f"sqlite:///{path}")
    reopened.migrate()
    assert _snapshot(reopened) == after
    with reopened.engine.connect() as c:
        for name, cols in columns.items():
            assert (
                c.exec_driver_sql(
                    f'SELECT {",".join(cols)} FROM "{name}" ORDER BY id'
                ).all()
                == before[name]
            )
        assert c.exec_driver_sql("PRAGMA foreign_key_check").all() == []
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
