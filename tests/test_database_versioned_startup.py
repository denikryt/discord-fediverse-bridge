"""Strict read-only SQLite admission and transactional CP03 migration boundaries.

The complete 0→1 converter and frozen v1 DDL are deliberately supplied by
CP04/CP05; this checkpoint verifies that incomplete code never stamps v1.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import text

from src.db import Database
from src.db.migrations import (
    DATABASE_VERSION,
    MIGRATIONS,
    SQLiteDatabaseState,
    read_database_state,
)


_FIXTURES = Path(__file__).parent / "fixtures" / "database"


def _schema_file(tmp_path: Path, name: str) -> Path:
    """Construct a source SQLite file using pinned, not current, schema SQL."""
    path = tmp_path / f"{name}.db"
    with sqlite3.connect(path) as connection:
        connection.executescript((_FIXTURES / f"{name}_schema.sql").read_text())
    return path


@pytest.mark.parametrize("name,profile", [
    ("released_v0", "released_v0"),
    ("known_main_v0", "known_main_v0"),
])
def test_supported_v0_preflight_is_read_only_and_upgrade_fails_closed(
    tmp_path: Path, name: str, profile: str
) -> None:
    """Classify both exact sources but never commit an incomplete converter."""
    path = _schema_file(tmp_path, name)
    database = Database(f"sqlite:///{path}")
    try:
        before = path.read_bytes()
        assert read_database_state(database.engine) == SQLiteDatabaseState(0, profile)
        assert path.read_bytes() == before
        with pytest.raises(RuntimeError, match="incomplete.*CP04/CP05"):
            database.migrate()
        assert path.read_bytes() == before
        assert read_database_state(database.engine) == SQLiteDatabaseState(0, profile)
        with database.engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
    finally:
        database.close()


def test_empty_v0_bootstrap_rolls_back_all_ddl_until_full_v1_schema(tmp_path: Path) -> None:
    """A fresh bootstrap must never acquire v1 without its frozen schema."""
    path = tmp_path / "new.db"
    database = Database(f"sqlite:///{path}")
    try:
        assert read_database_state(database.engine) == SQLiteDatabaseState(0, "empty_v0")
        with pytest.raises(RuntimeError, match="v1 schema snapshot is not ready"):
            database.migrate()
        with sqlite3.connect(path) as connection:
            assert connection.execute("PRAGMA user_version").fetchone() == (0,)
            assert not connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    finally:
        database.close()


@pytest.mark.parametrize("bad_version", [-1, 2, 42])
def test_invalid_or_future_header_is_rejected_without_ddl(
    tmp_path: Path, bad_version: int
) -> None:
    """Reject unsupported header versions without schema creation or repair."""
    path = _schema_file(tmp_path, "known_main_v0")
    with sqlite3.connect(path) as connection:
        connection.execute(f"PRAGMA user_version={bad_version}")
    before = path.read_bytes()
    database = Database(f"sqlite:///{path}")
    try:
        with pytest.raises(RuntimeError, match="Unsupported SQLite user_version"):
            database.migrate()
        assert path.read_bytes() == before
    finally:
        database.close()


@pytest.mark.parametrize("tamper", ["new_table", "missing_table", "new_column"])
def test_unknown_or_partial_source_rejected_before_writes(tmp_path: Path, tamper: str) -> None:
    """Structural tampering is not silently treated as a known v0 upgrade."""
    path = _schema_file(tmp_path, "known_main_v0")
    with sqlite3.connect(path) as connection:
        if tamper == "new_table":
            connection.execute("CREATE TABLE shadow_table (id INTEGER)")
        elif tamper == "missing_table":
            connection.execute("DROP TABLE bridge_policy_entries")
        else:
            connection.execute("ALTER TABLE users ADD COLUMN surprise TEXT")
    before = path.read_bytes()
    database = Database(f"sqlite:///{path}")
    try:
        with pytest.raises(RuntimeError, match="Unknown or partially upgraded"):
            database.migrate()
        assert path.read_bytes() == before
    finally:
        database.close()


def test_forged_v1_header_on_old_schema_is_rejected(tmp_path: Path) -> None:
    """A v1 header alone is not proof of a complete target schema."""
    path = _schema_file(tmp_path, "released_v0")
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version=1")
    before = path.read_bytes()
    database = Database(f"sqlite:///{path}")
    try:
        with pytest.raises(RuntimeError, match="v1 schema snapshot is not ready"):
            database.migrate()
        assert path.read_bytes() == before
    finally:
        database.close()


def test_registry_gap_fails_before_sqlite_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing upgrade step must be a fail-closed registry error."""
    database = Database(f"sqlite:///{tmp_path / 'new.db'}")
    try:
        monkeypatch.setattr("src.db.migrations.MIGRATIONS", {})
        with pytest.raises(RuntimeError, match="invalid SQLite migration registry"):
            database.migrate()
        assert DATABASE_VERSION == 1
        assert MIGRATIONS.get(1) is not None
    finally:
        database.close()
