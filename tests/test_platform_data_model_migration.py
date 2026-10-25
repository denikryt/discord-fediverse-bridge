"""Supported released-source migration and current-v1 cascade/restart guarantees."""

import shutil
import runpy
import sqlite3
from pathlib import Path
import pytest
from sqlalchemy import text, event
from sqlalchemy.orm import Session
from src.db import Database, migrations
from src import models
from test_legacy_v0_to_v1_migration import _seed, _evidence


def _released_database(path):
    """Open the independently captured published v0, never an unsupported dev schema."""
    shutil.copy2(
        Path(__file__).parent / "fixtures/release_v0_1_0_original.sqlite", path
    )
    return Database(f"sqlite:///{path}")


def _current_database(path):
    """Bootstrap actual v1 before seeding features absent from released v0."""
    db = Database(f"sqlite:///{path}")
    db.migrate()
    return db


def _seed_cascade_graph(database):
    """Populate current personal-feed/outbox/mutation descendants with real physical FKs."""
    old = vars(models)
    with Session(database.engine, expire_on_commit=False) as session:
        surface = database.platform_surfaces.ensure_channel(
            session,
            connector_key="discord-primary",
            external_channel_id="900",
            kind="text",
        )
        user = old["User"](
            discord_user_id="1",
            activitypub_username="alice",
            actor_url="https://bridge.test/actors/users/alice",
            inbox_url="https://bridge.test/actors/users/alice/inbox",
            outbox_url="https://bridge.test/actors/users/alice/outbox",
            followers_url="https://bridge.test/actors/users/alice/followers",
            public_key_pem="public",
            private_key_pem="private",
        )
        obj = old["PublishedActivityObject"](
            actor_username="alice",
            actor_url=user.actor_url,
            publication_context="personal_feed",
            activity_id="https://bridge.test/actors/users/alice/activities/1",
            object_id="https://bridge.test/objects/1",
            kind="Note",
            body_markdown="original",
            discord_guild_id=1,
            discord_channel_id=900,
            discord_message_id=111,
        )
        session.add_all([user, obj])
        session.flush()
        root = old["PersonalFeedThreadMapping"](
            connector_id=surface.connector_id,
            channel_surface_id=surface.id,
            external_thread_id="111",
            root_published_object_id=obj.id,
            root_discord_channel_id=900,
            root_discord_message_id=111,
            thread_id=111,
        )
        source = old["UserActorOutboundSource"](
            user_id=user.id,
            source_kind="content_create",
            ordering_key=obj.object_id,
            logical_version=1,
            activity_id=obj.activity_id,
            published_object_id=obj.id,
            snapshot_json='{"body":"original"}',
        )
        inbound = old["InboundConversationObject"](
            object_id="https://remote.test/objects/reply",
            conversation_root_object_id=obj.object_id,
            parent_object_id=obj.object_id,
            actor_id="https://remote.test/users/bob",
            content_markdown="reply",
            revision_fingerprint="abc",
        )
        mutation = old["UserActorContentMutation"](
            published_object_id=obj.id,
            user_id=user.id,
            kind="content_update",
            expected_revision=1,
            next_revision=2,
            operation_id="op1",
            activity_id="https://bridge.test/actors/users/alice/activities/edit",
            request_json="{}",
            request_fingerprint="def",
        )
        session.add_all([root, source, inbound, mutation])
        session.flush()
        delivery = old["UserActorOutboundDelivery"](
            source_id=source.id, target_endpoint_url="https://remote.test/inbox"
        )
        session.add_all(
            [
                delivery,
                old["InboundDiscordDelivery"](
                    connector_id=surface.connector_id,
                    channel_surface_id=surface.id,
                    external_thread_id="111",
                    inbound_object_id=inbound.id,
                    root_published_object_id=obj.id,
                    destination_channel_id=900,
                    destination_thread_id=111,
                    state="ready",
                ),
            ]
        )
        session.flush()
        session.add(
            old["UserActorOutboundDeliveryFollower"](
                delivery_id=delivery.id,
                relationship_id_snapshot=123,
                remote_actor_id=inbound.actor_id,
                snapshotted_endpoint_url=delivery.target_endpoint_url,
            )
        )
        session.commit()


@pytest.mark.parametrize("delete_root", [False, True])
def test_current_restart_preserves_graph_and_explicit_delete_cascades(
    tmp_path, delete_root
):
    """Restart preserves current-only descendants; deleting the publication cascades all owned rows."""
    db = _current_database(tmp_path / "current.db")
    _seed_cascade_graph(db)
    before = _evidence(db)
    db.migrate()
    assert _evidence(db) == before
    if delete_root:
        with db.engine.begin() as c:
            c.exec_driver_sql("DELETE FROM published_activity_objects")
        with db.engine.connect() as c:
            for name in (
                "personal_feed_thread_mappings",
                "inbound_discord_deliveries",
                "user_actor_outbound_sources",
                "user_actor_content_mutations",
                "user_actor_outbound_deliveries",
                "user_actor_outbound_delivery_followers",
            ):
                assert (
                    c.exec_driver_sql(f"SELECT count(*) FROM {name}").scalar_one() == 0
                )
            assert c.exec_driver_sql("PRAGMA foreign_key_check").all() == []
            assert (
                c.exec_driver_sql(
                    "SELECT count(*) FROM inbound_conversation_objects"
                ).scalar_one()
                == 1
            )
    db.close()


@pytest.mark.parametrize("phase", ["drop", "copy"])
def test_published_replacement_fault_restores_released_graph(tmp_path, phase):
    """An actual source DROP or target INSERT failure restores source rows, schema and FK mode."""
    db = _released_database(tmp_path / "fault.db")
    _seed(db)
    before = _evidence(db)

    def interrupt(conn, cursor, statement, parameters, context, many):
        """Interrupt after SQLite enters the physical published-parent replacement window."""
        sql = " ".join(statement.lower().split())
        if (
            phase == "drop"
            and sql == 'drop table main."published_activity_objects"'
            or phase == "copy"
            and sql.startswith("insert into main.published_activity_objects")
        ):
            raise RuntimeError("published replacement fault")

    event.listen(db.engine, "after_cursor_execute", interrupt)
    try:
        with pytest.raises(RuntimeError, match="published replacement fault"):
            db.migrate()
    finally:
        event.remove(db.engine, "after_cursor_execute", interrupt)
    assert _evidence(db) == before
    with db.engine.connect() as c:
        assert c.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
    db.migrate()
    db.close()


def test_known_discord_source_without_channel_keeps_scoped_dedup_identity(tmp_path):
    """A known source connector is retained even when its legacy channel is unknown."""
    database = _released_database(tmp_path / "unknown-channel.db")
    _seed(database)
    old = runpy.run_path(
        str(Path(__file__).parent / "fixtures/release_v0_1_0_models.py")
    )
    with Session(database.engine, expire_on_commit=False) as session:
        row = old["MessageMapping"](
            source_platform="discord",
            source_id="opaque-source",
            activity_id="https://bridge.test/actors/users/alice/activities/create/posts/source",
            object_id="https://bridge.test/actors/users/alice/objects/posts/source",
            actor_url="https://bridge.test/actors/users/alice",
            community_actor_url="https://remote.test/c/news",
        )
        session.add(row)
        session.commit()
    database.migrate()
    actual = database.message_mappings.get_message_mapping_by_source(
        "discord", "opaque-source"
    )
    assert actual is not None and actual.id == row.id
    assert (
        actual.source_connector_id is not None
        and actual.source_channel_surface_id is None
    )
    # The repository detects ambiguous NULL-container identity before INSERT.
    with pytest.raises(ValueError, match="source"):
        database.message_mappings.create_message_mapping(
            source_platform="discord",
            source_id="opaque-source",
            activity_id="https://bridge.test/actors/users/alice/activities/other",
            object_id="https://bridge.test/objects/other",
            actor_url=row.actor_url,
            community_actor_url=row.community_actor_url,
            discord_channel_id=None,
            discord_message_id=None,
        )

    assert database.message_mappings.get_message_mapping_by_source("discord", "opaque-source").id == row.id
    assert database.message_mappings.get_message_mapping_by_object_id("https://bridge.test/objects/other") is None
    database.close()


def test_v1_upgrade_and_fresh_bootstrap_have_equivalent_schema(tmp_path):
    """Released-source conversion matches atomically bootstrapped current v1."""
    upgraded = _released_database(tmp_path / "old.db")
    fresh = Database(f"sqlite:///{tmp_path / 'fresh.db'}")
    for db in (upgraded, fresh):
        db.migrate()
        with db.engine.connect() as conn:
            assert conn.exec_driver_sql("PRAGMA user_version").scalar_one() == 1
            assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
            assert conn.exec_driver_sql("PRAGMA foreign_key_check").all() == []
            assert (
                conn.exec_driver_sql(
                    "SELECT count(*) FROM platform_connectors"
                ).scalar_one()
                == 1
            )
            assert "discord_guild_id" not in migrations._table_columns(
                conn, "local_communities"
            )
            assert (
                next(
                    row
                    for row in conn.exec_driver_sql(
                        "PRAGMA table_xinfo(platform_channel_surfaces)"
                    )
                    if row[1] == "role"
                )[6]
                == 3
            )
        db.migrate()

    def inventory(db):
        """Compare actual constraints/index/trigger DDL, allowing quoting of rebuilt names."""
        with db.engine.connect() as conn:
            return [
                (r[0], r[1], " ".join(r[2].replace('"', "").split()))
                for r in conn.exec_driver_sql(
                    "SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT GLOB 'sqlite_*' ORDER BY type,name"
                )
            ]

    assert inventory(upgraded) == inventory(fresh)


def test_bootstrap_fault_rolls_back_schema_and_header(tmp_path, monkeypatch):
    """Failure after actual schema creation leaves a truly empty version-0 file."""
    database = Database(f"sqlite:///{tmp_path / 'fresh.db'}")
    original = migrations._install_profile_media_triggers

    def fail(conn):
        """Inject a bootstrap failure after DDL on the reserved connection."""
        original(conn)
        raise RuntimeError("injected failure")

    monkeypatch.setattr(migrations, "_install_profile_media_triggers", fail)
    with pytest.raises(RuntimeError, match="injected failure"):
        database.migrate()
    with database.engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA user_version").scalar_one() == 0
        assert (
            conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT GLOB 'sqlite_*'"
            ).all()
            == []
        )
        assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
    monkeypatch.setattr(migrations, "_install_profile_media_triggers", original)
    database.migrate()


@pytest.mark.parametrize("upgrade", [False, True])
def test_two_migrators_recheck_header_under_writer_lock(tmp_path, upgrade):
    """Concurrent startup initializes or upgrades once without losing data."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    path = tmp_path / "concurrent.db"
    first = Database(f"sqlite:///{path}")
    if upgrade:
        with sqlite3.connect(path) as conn:
            conn.executescript(
                (
                    Path(__file__).parent / "fixtures/release_v0_1_0_schema.sql"
                ).read_text()
            )
    if upgrade:
        from test_legacy_v0_to_v1_migration import _seed

        _seed(first)
    second = Database(f"sqlite:///{path}")
    barrier = Barrier(2)

    def start(db):
        """Release both independently connected migration runners together."""
        barrier.wait(timeout=10)
        db.migrate()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [pool.submit(start, db) for db in (first, second)]
        for result in results:
            result.result(timeout=30)
    with first.engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA user_version").scalar_one() == 1
        assert (
            conn.exec_driver_sql(
                "SELECT count(*) FROM platform_connectors"
            ).scalar_one()
            == 1
        )
        assert conn.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
        if upgrade:
            assert (
                conn.exec_driver_sql(
                    "SELECT count(*) FROM published_activity_objects"
                ).scalar_one()
                == 2
            )


@pytest.mark.parametrize("upgrade", [False, True])
def test_published_composite_fk_is_enforced_after_commit(tmp_path, upgrade):
    """Fresh and migrated schemas reject cross-connector INSERT and UPDATE."""
    from sqlalchemy.exc import IntegrityError
    from src.models import PublishedActivityObject

    path = tmp_path / "enforced.db"
    database = _released_database(path) if upgrade else Database(f"sqlite:///{path}")
    database.migrate()
    with database.immediate_session() as session:
        repo = database.platform_surfaces
        connector = repo.add_connector(
            session,
            connector_key="fluxer-test",
            platform="fluxer",
            instance_origin="https://fluxer.test",
        )
        surface = repo.ensure_channel(
            session, connector_key="discord-primary", external_channel_id="900"
        )
        connector_id, surface_id = connector.id, surface.id
        row = PublishedActivityObject(
            actor_username="alice",
            actor_url="https://bridge.test/actors/users/alice",
            publication_context="community",
            community_actor_url="https://remote.test/c/news",
            activity_id="https://bridge.test/a/1",
            object_id="https://bridge.test/o/1",
            kind="Note",
            body_markdown="body",
            source_connector_id=surface.connector_id,
            source_channel_surface_id=surface.id,
        )
        session.add(row)
        session.flush()
        row_id = row.id
    with pytest.raises(IntegrityError):
        with database.immediate_session() as session:
            session.execute(
                text(
                    "UPDATE published_activity_objects SET source_connector_id=:connector WHERE id=:row"
                ),
                {"connector": connector_id, "row": row_id},
            )
    with pytest.raises(IntegrityError):
        with database.immediate_session() as session:
            session.execute(
                text(
                    "INSERT INTO published_activity_objects (actor_username,actor_url,publication_context,community_actor_url,activity_id,object_id,kind,body_markdown,source_connector_id,source_channel_surface_id) VALUES ('alice','https://bridge.test/actors/users/alice','community','https://remote.test/c/news','https://bridge.test/a/2','https://bridge.test/o/2','Note','body',:connector,:surface)"
                ),
                {"connector": connector_id, "surface": surface_id},
            )
    with database.engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA foreign_key_check").all() == []


def test_unsupported_sqlite_fails_before_any_write(tmp_path):
    """An old runtime is rejected before empty bootstrap creates any objects."""
    from sqlalchemy import event

    database = Database(f"sqlite:///{tmp_path / 'unsupported.db'}")

    def old_version(dbapi_connection, connection_record):
        """Override only the version probe, retaining the real SQLite engine."""
        dbapi_connection.create_function("sqlite_version", 0, lambda: "3.30.1")

    event.listen(database.engine, "connect", old_version)
    with pytest.raises(RuntimeError, match="SQLite >= 3.31.0"):
        database.migrate()
    with database.engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA user_version").scalar_one() == 0
        assert (
            conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).all()
            == []
        )
