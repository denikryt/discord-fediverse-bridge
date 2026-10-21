"""Behavior and SQLite consistency tests for the public User Actor outbox read model."""

from __future__ import annotations


import sqlite3
import pytest
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import text

from src.db import Database
from src.models import PublishedActivityObject


def _database(tmp_path: Path) -> tuple[Database, object]:
    """Create one registered user in the real file-backed database."""
    database = Database(f"sqlite:///{tmp_path / 'bridge.db'}")
    database.migrate()
    user = database.users.create_user(
        discord_user_id="1",
        activitypub_username="alice",
        actor_url="https://bridge.test/actors/users/alice",
        inbox_url="https://bridge.test/actors/users/alice/inbox",
        outbox_url="https://bridge.test/actors/users/alice/outbox",
        followers_url="https://bridge.test/actors/users/alice/followers",
        public_key_pem="public",
        private_key_pem="private",
    )
    return database, user


def _row(database: Database, user, index: int, *, kind: str = "post"):
    """Persist one eligible canonical object with deterministic publication time."""
    return database.activitypub_objects.create_published_activity_object(
        actor_username=user.activitypub_username,
        actor_url=user.actor_url,
        community_actor_url="https://bridge.test/actors/communities/test",
        activity_id=f"https://bridge.test/actors/users/alice/activities/create/posts/{index}",
        object_id=f"https://bridge.test/objects/{index}",
        kind=kind,
        title=None,
        body_markdown=f"body {index}",
        in_reply_to_object_id=None,
        discord_channel_id=2,
        discord_message_id=index,
        published_at=datetime(2026, 1, 1, tzinfo=timezone.utc)
        + timedelta(seconds=index),
    )


def test_outbox_slice_orders_filters_and_is_bounded(tmp_path: Path) -> None:
    """Only current public bridge-owned posts/comments for the resolved user appear."""
    database, user = _database(tmp_path)
    first = _row(database, user, 1)
    second = _row(database, user, 2, kind="comment")
    with database.session() as session:
        session.get(PublishedActivityObject, first.id).deleted_at = datetime.now(
            timezone.utc
        )
        session.add(
            PublishedActivityObject(
                actor_username="mallory",
                actor_url=user.actor_url,
                publication_context="community",
                community_actor_url="https://bridge.test/actors/communities/test",
                activity_id="https://bridge.test/actors/users/mallory/activities/create/posts/leak",
                object_id="https://bridge.test/objects/leak",
                kind="post",
                title=None,
                body_markdown="leak",
                discord_channel_id=2,
                discord_message_id=999,
                published_at=datetime.now(timezone.utc),
            )
        )
    result = database.activitypub_objects.load_user_outbox_slice(
        user=user, offset=0, limit=40
    )
    assert result.total_items == 1
    assert [item.id for item in result.items] == [second.id]


def test_outbox_query_plan_uses_community_writers_composite_index(tmp_path: Path) -> None:
    """Representative page lookup uses the migration-owned composite index."""
    database, user = _database(tmp_path)
    _row(database, user, 1)
    with database.engine.connect() as connection:
        plan = connection.execute(
            text(
                "EXPLAIN QUERY PLAN SELECT * FROM published_activity_objects "
                "WHERE actor_username=:username AND actor_url=:actor_url "
                "AND deleted_at IS NULL AND kind IN ('post','comment') "
                "ORDER BY published_at DESC, id DESC LIMIT 40"
            ),
            {"username": user.activitypub_username, "actor_url": user.actor_url},
        ).all()
    assert any("ix_published_objects_user_outbox" in str(row) for row in plan)


def test_current_missing_outbox_index_is_not_silently_repaired(tmp_path: Path) -> None:
    """A damaged current schema is rejected without recreating indexes or losing rows."""
    path = tmp_path / "legacy.db"
    database = Database(f"sqlite:///{path}")
    database.migrate()
    with database.engine.begin() as connection:
        connection.execute(text("DROP INDEX ix_published_objects_user_outbox"))
        connection.execute(
            text(
                "INSERT INTO published_activity_objects "
                "(actor_username, actor_url, publication_context, community_actor_url, "
                "activity_id, object_id, kind, body_markdown, content_revision, desired_kind, "
                "discord_reconciliation_needed, discord_reconciliation_attempt_count, "
                "published_at, created_at) VALUES "
                "('alice', 'https://bridge.test/actors/users/alice', 'community', "
                "'https://bridge.test/actors/communities/test', 'a1', 'o1', 'post', 'body', "
                "1, 'none', 0, 0, '2026-01-01', '2026-01-01')"
            )
        )
    with pytest.raises(RuntimeError, match="published_activity_objects.indexes"):
        database.migrate()
    with database.engine.connect() as migrated:
        assert migrated.exec_driver_sql("PRAGMA user_version").scalar_one() == 1
        assert (
            migrated.exec_driver_sql(
                "SELECT count(*) FROM published_activity_objects"
            ).scalar_one()
            == 1
        )
        assert not any(
            row[1] == "ix_published_objects_user_outbox"
            for row in migrated.exec_driver_sql(
                "PRAGMA index_list(published_activity_objects)"
            )
        )


def _assert_rollback_writer_waits_for_reader(tmp_path: Path, mutation: str) -> None:
    path = tmp_path / f"rollback-{mutation}.db"
    setup = sqlite3.connect(path)
    setup.execute(
        "CREATE TABLE items (id INTEGER PRIMARY KEY, deleted INTEGER DEFAULT 0)"
    )
    setup.execute("INSERT INTO items DEFAULT VALUES")
    setup.commit()
    setup.close()
    reader = sqlite3.connect(path, isolation_level=None)
    reader.execute("BEGIN")
    assert (
        reader.execute("SELECT count(*) FROM items WHERE deleted=0").fetchone()[0] == 1
    )
    started = threading.Event()
    finished = threading.Event()
    outcome: list[str] = []

    def write_once() -> None:
        writer = sqlite3.connect(path, timeout=5)
        try:
            started.set()
            if mutation == "insert":
                writer.execute("INSERT INTO items DEFAULT VALUES")
            else:
                writer.execute("UPDATE items SET deleted=1 WHERE id=1")
            writer.commit()
            outcome.append("committed")
        finally:
            writer.close()
            finished.set()

    thread = threading.Thread(target=write_once)
    thread.start()
    assert started.wait(1)
    assert not finished.wait(0.2)
    assert reader.execute("SELECT id FROM items WHERE deleted=0").fetchall() == [(1,)]
    reader.commit()
    reader.close()
    assert finished.wait(5)
    thread.join()
    assert outcome == ["committed"]
    check = sqlite3.connect(path)
    expected = [(1,), (2,)] if mutation == "insert" else []
    assert (
        check.execute("SELECT id FROM items WHERE deleted=0 ORDER BY id").fetchall()
        == expected
    )
    check.close()


def test_rollback_journal_insert_writer_waits_then_commits(tmp_path: Path) -> None:
    """The production journal mode preserves the read and later commits the same writer."""
    _assert_rollback_writer_waits_for_reader(tmp_path, "insert")


def test_rollback_journal_delete_writer_waits_then_commits(tmp_path: Path) -> None:
    """A committed delete cannot change page membership inside the open read transaction."""
    _assert_rollback_writer_waits_for_reader(tmp_path, "delete")


def test_wal_reader_keeps_count_snapshot_after_concurrent_insert_and_delete(
    tmp_path: Path,
) -> None:
    """The WAL-specific fixture proves the count-established snapshot stays stable."""
    path = tmp_path / "wal.db"
    setup = sqlite3.connect(path)
    assert setup.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() == "wal"
    setup.execute(
        "CREATE TABLE items (id INTEGER PRIMARY KEY, deleted INTEGER DEFAULT 0)"
    )
    setup.execute("INSERT INTO items DEFAULT VALUES")
    setup.commit()
    setup.close()
    reader = sqlite3.connect(path, isolation_level=None)
    writer = sqlite3.connect(path)
    reader.execute("BEGIN")
    assert (
        reader.execute("SELECT count(*) FROM items WHERE deleted=0").fetchone()[0] == 1
    )
    writer.execute("UPDATE items SET deleted=1 WHERE id=1")
    writer.execute("INSERT INTO items DEFAULT VALUES")
    writer.commit()
    assert reader.execute("SELECT id FROM items WHERE deleted=0").fetchall() == [(1,)]
    reader.commit()
    assert reader.execute("SELECT id FROM items WHERE deleted=0").fetchall() == [(2,)]
    reader.close()
    writer.close()
