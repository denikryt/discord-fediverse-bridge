"""Exclusive process-lifetime ownership of one file-backed bridge SQLite database.

The deployment state lock still serializes recovery/restore against backups;
this independent lease closes the EX-to-SH transition race between runtimes.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.engine import make_url

from .profile_media import FileLock


def runtime_lifetime_lock_path(database_url: str) -> Path:
    """Derive one stable sibling lock pathname for a canonical SQLite file.

    Resolve a symlinked database pathname to avoid alias-based competing bridge
    owners, but never lock the database inode: restore may replace that inode.
    In-memory, URI-mode and non-SQLite databases have no supported stable lease.
    """
    try:
        parsed = make_url(database_url)
    except (TypeError, ValueError) as error:
        raise ValueError("Runtime requires a file-backed SQLite database URL") from error
    database_name = parsed.database
    if (
        parsed.drivername != "sqlite"
        or not database_name
        or database_name == ":memory:"
        or database_name.startswith("file:")
        or parsed.query
        or "\x00" in database_name
    ):
        raise ValueError("Runtime requires a file-backed SQLite database URL")
    database_path = Path(database_name).resolve(strict=False)
    return database_path.with_name(database_path.name + ".runtime.lock")


def acquire_runtime_lifetime_lease(database_url: str) -> FileLock:
    """Acquire a fail-fast POSIX exclusive lease before any recovery or SDK work.

    The caller owns the returned descriptor until all callbacks, SDK clients
    and database connections are closed. Never unlink this lock on shutdown.
    """
    lock_path = runtime_lifetime_lock_path(database_url)
    try:
        return FileLock(lock_path).acquire(exclusive=True, timeout_seconds=0)
    except TimeoutError as error:
        # A contender must fail before running recovery or opening a Gateway.
        raise RuntimeError("runtime already active for SQLite database") from error
