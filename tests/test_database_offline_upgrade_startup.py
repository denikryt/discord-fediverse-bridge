"""Offline EX migration backup and v3 SH-readmission observable regressions."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.db import Database
from src.db.backup import inspect_migration_backup_source
from src.db.migrations import Stage4IdentityMigrationConfig, read_database_state
from src.db.startup import prepare_sqlite_under_exclusive_lease
from src.profile_media import FileLock


FIXTURE = Path(__file__).parent / "fixtures/database/released_v0_schema.sql"
IDENTITY = Stage4IdentityMigrationConfig("https://bridge.example", "bridge")


def _database(tmp_path: Path, *, release: bool) -> tuple[Database, Path]:
    """Build one disposable source file using the exact released SQLite DDL."""
    path = tmp_path / "bridge.db"
    if release:
        with sqlite3.connect(path) as source:
            source.executescript(FIXTURE.read_text(encoding="utf-8"))
    return Database(f"sqlite:///{path}"), path


def _prepare(database: Database, path: Path, root: Path) -> Path | None:
    """Follow actual EX ownership rather than calling backup without its lease."""
    with FileLock(root / "deployment.lock").acquire(exclusive=True):
        return prepare_sqlite_under_exclusive_lease(
            database,
            database_path=path,
            media_root=root / "profile-media",
            media_lock_path=root / "media.lock",
            lock_timeout_seconds=5,
            identity=IDENTITY,
        )


def test_release_v0_startup_creates_backup_before_stamping_v1(tmp_path: Path) -> None:
    """The historical source remains restorable after a successful v3 admission."""
    database, path = _database(tmp_path, release=True)
    try:
        archived = _prepare(database, path, tmp_path)
        assert archived is not None and archived.is_file()
        assert archived.parent == tmp_path / "backups" / "migrations"
        assert read_database_state(database.engine).profile == "ready_v3"
        with sqlite3.connect(path) as conn:
            assert conn.execute("PRAGMA user_version").fetchone() == (3,)
        # The verified backup is the *original* v0 source, not the new v1.
        restored = tmp_path / "restorable-v0.db"
        import tarfile
        with tarfile.open(archived, "r:gz") as tar:
            item = tar.extractfile("bridge.db")
            assert item is not None
            restored.write_bytes(item.read())
        assert inspect_migration_backup_source(restored) == "released_v0"
        # SQLite online backup may use a different header change counter; the
        # authoritative equivalence is a valid original schema+row snapshot.
        with sqlite3.connect(restored) as original:
            assert original.execute("PRAGMA user_version").fetchone() == (0,)
            assert original.execute("PRAGMA integrity_check").fetchone() == ("ok",)
            assert original.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchone() == (30,)
        with FileLock(tmp_path / "deployment.lock").acquire(exclusive=False):
            assert read_database_state(database.engine).profile == "ready_v3"
    finally:
        database.close()


def test_v1_restart_under_shared_lock_is_read_only_and_has_no_new_backup(tmp_path: Path) -> None:
    """Second startup must not change rows or regenerate migration archives."""
    database, path = _database(tmp_path, release=True)
    try:
        first = _prepare(database, path, tmp_path)
        assert first is not None
        before = path.read_bytes()
        assert _prepare(database, path, tmp_path) is None
        assert path.read_bytes() == before
        assert list((tmp_path / "backups" / "migrations").glob("*.tar.gz")) == [first]
        with FileLock(tmp_path / "deployment.lock").acquire(exclusive=False):
            assert read_database_state(database.engine).profile == "ready_v3"
            assert path.read_bytes() == before
    finally:
        database.close()


def test_failed_backup_blocks_migration_and_preserves_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A backup failure is terminal: source DB header and contents stay v0."""
    database, path = _database(tmp_path, release=True)
    try:
        before = path.read_bytes()
        def fail_backup(*_args: object, **_kwargs: object) -> None:
            """Inject a verifiable refusal before the writer transaction."""
            raise RuntimeError("unreadable migration archive")
        monkeypatch.setattr("src.db.startup.create_migration_backup", fail_backup)
        with pytest.raises(RuntimeError, match="unreadable migration archive"):
            _prepare(database, path, tmp_path)
        assert path.read_bytes() == before
        assert read_database_state(database.engine).profile == "released_v0"
    finally:
        database.close()


def test_empty_v0_can_bootstrap_without_migration_archive(tmp_path: Path) -> None:
    """An empty SQLite file has no historical data to back up or misclassify."""
    database, path = _database(tmp_path, release=False)
    try:
        assert _prepare(database, path, tmp_path) is None
        assert read_database_state(database.engine).profile == "ready_v3"
        assert not (tmp_path / "backups" / "migrations").exists()
    finally:
        database.close()


def test_future_version_is_rejected_before_backup_and_network(tmp_path: Path) -> None:
    """Unsupported database headers cause no protected backup or migration DDL."""
    database, path = _database(tmp_path, release=True)
    try:
        with sqlite3.connect(path) as conn:
            conn.execute("PRAGMA user_version=4")
        before = path.read_bytes()
        with pytest.raises(RuntimeError, match="Unsupported SQLite user_version"):
            _prepare(database, path, tmp_path)
        assert path.read_bytes() == before
        assert not (tmp_path / "backups" / "migrations").exists()
    finally:
        database.close()


def test_shared_startup_rejects_stale_v0_without_writing(tmp_path: Path) -> None:
    """The ordinary lifetime lease cannot silently migrate an old process DB."""
    from src.db.startup import admit_sqlite_under_shared_lease

    database, path = _database(tmp_path, release=True)
    try:
        before = path.read_bytes()
        with FileLock(tmp_path / "deployment.lock").acquire(exclusive=False):
            with pytest.raises(RuntimeError, match="current version read-only admission failed"):
                admit_sqlite_under_shared_lease(database)
        assert path.read_bytes() == before
        assert inspect_migration_backup_source(path) == "released_v0"
    finally:
        database.close()


def test_two_independent_v1_shared_starters_read_without_rewrites(tmp_path: Path) -> None:
    """Independent SH admission does not change accepted v1 rows or SQLite header."""
    from src.db.startup import admit_sqlite_under_shared_lease

    database, path = _database(tmp_path, release=True)
    other = Database(f"sqlite:///{path}")
    try:
        assert _prepare(database, path, tmp_path) is not None
        before = path.read_bytes()
        with FileLock(tmp_path / "deployment.lock").acquire(exclusive=False):
            with FileLock(tmp_path / "deployment.lock").acquire(exclusive=False):
                admit_sqlite_under_shared_lease(database)
                admit_sqlite_under_shared_lease(other)
        assert path.read_bytes() == before
    finally:
        other.close()
        database.close()
