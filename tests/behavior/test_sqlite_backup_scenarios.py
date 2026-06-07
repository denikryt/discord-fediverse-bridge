"""Behavior scenarios for consistent local SQLite backups and restores."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from src.db.backup import apply_retention, create_backup, restore_backup, validate_snapshot


def test_live_database_backup_contains_committed_actor_keys(tmp_path) -> None:
    """A running WAL database produces a valid snapshot containing all state."""
    database = tmp_path / "bridge.db"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE bridge_actor_keys (private_key_data TEXT)")
        connection.execute("CREATE TABLE users (private_key_pem TEXT)")
        connection.execute("CREATE TABLE local_communities (private_key_pem TEXT)")
        connection.execute("INSERT INTO bridge_actor_keys VALUES ('bridge-secret')")
        connection.execute("INSERT INTO users VALUES ('user-secret')")
        connection.execute("INSERT INTO local_communities VALUES ('community-secret')")
        connection.commit()
    backup = create_backup(
        database,
        tmp_path / "backups",
        now=datetime(2026, 6, 7, 2, tzinfo=timezone.utc),
    )
    validate_snapshot(backup)
    with sqlite3.connect(backup) as connection:
        assert connection.execute("SELECT private_key_data FROM bridge_actor_keys").fetchone() == ("bridge-secret",)
        assert connection.execute("SELECT private_key_pem FROM users").fetchone() == ("user-secret",)
        assert connection.execute("SELECT private_key_pem FROM local_communities").fetchone() == ("community-secret",)
    assert oct(backup.stat().st_mode & 0o777) == "0o600"


def test_retention_is_scoped_and_restore_preserves_live_on_corruption(tmp_path) -> None:
    """Cleanup ignores unrelated files and corrupt restores leave live data intact."""
    output = tmp_path / "backups"
    output.mkdir()
    snapshots = []
    for day in range(1, 4):
        source = tmp_path / f"source-{day}.db"
        with sqlite3.connect(source) as connection:
            connection.execute("CREATE TABLE value (data TEXT)")
            connection.execute("INSERT INTO value VALUES (?)", (str(day),))
        snapshots.append(create_backup(source, output, now=datetime(2026, 6, day, tzinfo=timezone.utc)))
    unrelated = output / "notes.txt"
    unrelated.write_text("keep")
    removed = apply_retention(output, 2)
    assert removed == [snapshots[0]]
    assert unrelated.exists()

    live = tmp_path / "live.db"
    with sqlite3.connect(live) as connection:
        connection.execute("CREATE TABLE value (data TEXT)")
        connection.execute("INSERT INTO value VALUES ('live')")
    corrupt = tmp_path / "corrupt.sqlite3"
    corrupt.write_text("not sqlite")
    with pytest.raises(sqlite3.DatabaseError):
        restore_backup(live, corrupt)
    with sqlite3.connect(live) as connection:
        assert connection.execute("SELECT data FROM value").fetchone() == ("live",)

    restore_backup(live, snapshots[-1])
    with sqlite3.connect(live) as connection:
        assert connection.execute("SELECT data FROM value").fetchone() == ("3",)
