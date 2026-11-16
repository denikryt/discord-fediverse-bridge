"""Offline SQLite admission, protected backup and upgrade to v2.

The caller must own the existing exclusive deployment flock while this module
validates the source and changes its schema. No lock upgrade/downgrade API or
new recovery service is part of this boundary.
"""

from __future__ import annotations

from pathlib import Path

from .backup import create_migration_backup
from .database import Database
from .migrations import (
    DATABASE_VERSION,
    Stage4IdentityMigrationConfig,
    read_database_state,
)


def prepare_sqlite_under_exclusive_lease(
    database: Database,
    *,
    database_path: Path,
    media_root: Path,
    media_lock_path: Path,
    lock_timeout_seconds: float,
    identity: Stage4IdentityMigrationConfig,
) -> Path | None:
    """Upgrade the offline SQLite file only after a verified, no-clobber backup.

    This function does not acquire deployment flock: startup already holds EX.
    Every populated pre-v2 source gets a protected restore-capable backup;
    empty v0 bootstraps v2 without one. A valid v2 is read-admitted.
    Any failure stops startup without pretending a partial migration worked.
    """
    source = read_database_state(database.engine)
    archived: Path | None = None
    if source.version < DATABASE_VERSION and source.profile != "empty_v0":
        # Keep migration archives outside ordinary backup retention. The media
        # lock protects the immutable referenced-file view while the EX lease
        # already excludes other new application processes.
        archived = create_migration_backup(
            database_path,
            media_root,
            database_path.parent / "backups",
            media_lock_path=media_lock_path,
            lock_timeout_seconds=lock_timeout_seconds,
        )
    database.migrate(identity)
    admitted = read_database_state(database.engine)
    if admitted.version != DATABASE_VERSION or admitted.profile != f"ready_v{DATABASE_VERSION}":
        raise RuntimeError("offline SQLite current version admission failed after upgrade")
    return archived


def admit_sqlite_under_shared_lease(database: Database) -> None:
    """Reject anything but validated v2 after the ordinary lifetime SH lease.

    This separate admission deliberately cannot run any mutation or backup;
    the preceding exclusive lease is already gone, and concurrent v2 owners
    may only share an immutable schema and independently managed rows.
    """
    admitted = read_database_state(database.engine)
    if admitted.version != DATABASE_VERSION or admitted.profile != f"ready_v{DATABASE_VERSION}":
        raise RuntimeError("SQLite current version read-only admission failed under SH lease")
