"""Create, retain, and restore consistent local SQLite backup snapshots."""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

BACKUP_PREFIX = "discord-fediverse-bridge-"
BACKUP_SUFFIX = ".sqlite3"
TEMP_SUFFIX = ".partial"


def create_backup(database: Path, output_dir: Path, *, now: datetime | None = None) -> Path:
    """Create and validate one online SQLite snapshot before publishing it."""
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    final_path = output_dir / f"{BACKUP_PREFIX}{timestamp}{BACKUP_SUFFIX}"
    temporary_path = output_dir / f".{final_path.name}{TEMP_SUFFIX}"
    temporary_path.unlink(missing_ok=True)
    try:
        with sqlite3.connect(database) as source, sqlite3.connect(temporary_path) as destination:
            source.backup(destination)
        validate_snapshot(temporary_path)
        os.chmod(temporary_path, 0o600)
        temporary_path.replace(final_path)
        return final_path
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def validate_snapshot(snapshot: Path) -> None:
    """Fail unless one SQLite snapshot opens and passes integrity checking."""
    with sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True) as connection:
        result = connection.execute("PRAGMA integrity_check").fetchone()
    if result is None or result[0] != "ok":
        raise RuntimeError("SQLite backup failed integrity_check")


def apply_retention(output_dir: Path, retention_count: int) -> list[Path]:
    """Delete only the oldest final project snapshots beyond the retention count."""
    if retention_count < 1:
        raise ValueError("backup retention count must be at least 1")
    snapshots = sorted(output_dir.glob(f"{BACKUP_PREFIX}*{BACKUP_SUFFIX}"))
    removed: list[Path] = []
    for path in snapshots[:-retention_count]:
        path.unlink()
        removed.append(path)
    return removed


def restore_backup(database: Path, source: Path) -> Path | None:
    """Validate and atomically restore a snapshot while preserving the live DB."""
    validate_snapshot(source)
    database.parent.mkdir(parents=True, exist_ok=True)
    recovery: Path | None = None
    if database.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        recovery = database.with_name(f"{database.name}.pre-restore-{stamp}")
        database.replace(recovery)
    # Stopped SQLite processes may leave WAL/SHM sidecars that belong to the
    # previous database image; remove them before publishing the restored file.
    for suffix in ("-wal", "-shm"):
        database.with_name(database.name + suffix).unlink(missing_ok=True)
    temporary = database.with_name(f".{database.name}.restore{TEMP_SUFFIX}")
    try:
        shutil.copy2(source, temporary)
        validate_snapshot(temporary)
        os.chmod(temporary, 0o600)
        temporary.replace(database)
    except Exception:
        temporary.unlink(missing_ok=True)
        if recovery is not None and not database.exists():
            recovery.replace(database)
        raise
    return recovery


def serve(database: Path, output_dir: Path, interval_seconds: int, retention_count: int) -> None:
    """Run immediate and periodic backups until SIGTERM or SIGINT is received."""
    if interval_seconds < 1:
        raise ValueError("backup interval must be at least 1 second")
    stop = threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        # Event-based waiting lets Docker SIGTERM interrupt the sleep promptly.
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    while not stop.is_set():
        try:
            created = create_backup(database, output_dir)
            removed = apply_retention(output_dir, retention_count)
            print(f"backup created path={created}", flush=True)
            for path in removed:
                print(f"backup retention deleted path={path}", flush=True)
        except Exception as exc:
            # Keep the periodic service alive after transient filesystem or DB
            # failures, while never including private key material in logs.
            print(f"backup failed error={type(exc).__name__}: {exc}", flush=True)
        stop.wait(interval_seconds)


def _parser() -> argparse.ArgumentParser:
    """Build the CLI parser shared by one-shot, periodic, and restore commands."""
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("backup", "serve"):
        command = subparsers.add_parser(name)
        command.add_argument("--database", type=Path, required=True)
        command.add_argument("--output-dir", type=Path, required=True)
        if name == "serve":
            command.add_argument("--interval-seconds", type=int, default=86400)
            command.add_argument("--retention-count", type=int, default=14)
    restore = subparsers.add_parser("restore")
    restore.add_argument("--database", type=Path, required=True)
    restore.add_argument("--source", type=Path, required=True)
    return parser


def main() -> None:
    """Execute one backup CLI command."""
    args = _parser().parse_args()
    if args.command == "backup":
        print(create_backup(args.database, args.output_dir))
    elif args.command == "serve":
        serve(args.database, args.output_dir, args.interval_seconds, args.retention_count)
    else:
        recovery = restore_backup(args.database, args.source)
        print(recovery or "restored without prior live database")


if __name__ == "__main__":
    main()
