"""Create, retain, restore, and recover complete SQLite/profile-media backups."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import signal
import sqlite3
import tarfile
import tempfile
import threading
import warnings
from datetime import datetime, timezone
from pathlib import Path

from ..profile_media import FileLock

BACKUP_PREFIX = "discord-fediverse-bridge-"
BACKUP_SUFFIX = ".sqlite3"
COMPLETE_BACKUP_SUFFIX = ".tar.gz"
TEMP_SUFFIX = ".partial"
MANIFEST_VERSION = 1
RESTORE_MARKER = ".bridge-restore-state.json"

# Exact SQLite DDL manifests from the tagged release writer and the unchanged
# main writer. They are an archive source guard, not a substitute for migration
# identity/row admission performed by the versioned runner (CP03/CP04).
_SOURCE_SCHEMA_DIGESTS = {
    "4edc027e25054a91cc3e5144bc5bc37ee955339fc3492192cc707b9cf177537d": "released_v0",
    "3b5a087fc4d5df3452d4ff8b63de9b2bfe8ec4c4740fd5cc05c2911df2bf408b": "known_main_v0",
}


def schema_entries_fingerprint(entries: object) -> str:
    """Hash complete SQLite DDL without incidental trailing formatting changes.

    SQLite's `iterdump` removes per-line trailing whitespace from stored DDL.
    Preserve every other token, constraint, index, trigger and table identity
    instead of treating a table count or one column as source evidence.
    """
    canonical_entries = [
        (
            kind, name, table,
            "\n".join(line.rstrip() for line in sql.splitlines()) if sql else None,
        )
        for kind, name, table, sql in entries
    ]
    return hashlib.sha256(
        json.dumps(canonical_entries, ensure_ascii=True, separators=(",", ":")).encode()
    ).hexdigest()


def classify_legacy_sqlite_schema(entries: object) -> str | None:
    """Classify only the two source-writer DDL profiles in the feature scope."""
    return _SOURCE_SCHEMA_DIGESTS.get(schema_entries_fingerprint(entries))


def inspect_migration_backup_source(database: Path) -> str:
    """Classify a schema-exact v0 or v1 pre-upgrade backup without writes.

    This is deliberately stricter than ordinary backups: unknown or partially
    upgraded layouts cannot be passed to the subsequent v0→v1 runner by merely
    supplying a convenient source label. Persisted identities need additional
    read-only migration admission before any upgrade writes.
    """
    # mode=ro prevents SQLite from silently creating an absent source DB.
    with sqlite3.connect(f"{database.absolute().as_uri()}?mode=ro", uri=True) as connection:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version not in (0, 1):
            raise RuntimeError("migration backup requires an admitted v0 or v1 source")
        entries = connection.execute("""
            SELECT type, name, tbl_name, sql
            FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'
            ORDER BY type, name, tbl_name
        """).fetchall()
        if version == 1:
            # Compare complete DDL using the same fingerprint as startup; do
            # not accept a forged version header on a partially upgraded DB.
            with sqlite3.connect(":memory:") as reference:
                snapshot = Path(__file__).parent / "schema_versions" / "v1.sql"
                reference.executescript(snapshot.read_text(encoding="utf-8"))
                expected = reference.execute(
                    "SELECT type,name,tbl_name,sql FROM sqlite_master "
                    "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name,tbl_name"
                ).fetchall()
            if schema_entries_fingerprint(entries) != schema_entries_fingerprint(expected):
                raise RuntimeError("unsupported or modified SQLite v1 backup source schema")
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise RuntimeError("SQLite v1 backup source has foreign-key violations")
            return "ready_v1"
        profile = classify_legacy_sqlite_schema(entries)
        if profile is None:
            raise RuntimeError("unsupported or modified SQLite v0 backup source profile")
        return profile


def create_migration_backup(
    database: Path,
    media_root: Path,
    output_dir: Path,
    *,
    media_lock_path: Path,
    lock_timeout_seconds: float = 30,
    now: datetime | None = None,
) -> Path:
    """Create a verified, no-clobber pre-upgrade archive under `output_dir/migrations`.

    The caller must already hold the existing EX deployment lock across source
    admission, this backup, and the eventual writer transaction. Acquiring a
    second deployment lock here would deadlock startup; this helper acquires
    only the media reader lock. No ordinary retention policy reaches the
    dedicated migration directory, and source SQLite is never modified.
    """
    profile = inspect_migration_backup_source(database)
    protected = output_dir / "migrations"
    if output_dir.is_symlink() or protected.is_symlink():
        raise RuntimeError("migration backup output directory must not be a symlink")
    protected.mkdir(parents=True, exist_ok=True, mode=0o700)
    timestamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    prefix = f"{BACKUP_PREFIX}migration-{timestamp}-{secrets.token_hex(8)}"
    fd, temporary_name = tempfile.mkstemp(
        prefix=".migration-", suffix=TEMP_SUFFIX, dir=protected
    )
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        with FileLock(media_lock_path).acquire(
            exclusive=False, timeout_seconds=lock_timeout_seconds
        ):
            _write_complete_archive(
                database, media_root, protected, temporary, source_profile=profile
            )
        os.chmod(temporary, 0o600)
        # POSIX link(2) is atomic and refuses to replace an existing archive.
        # Even a fixed timestamp/random token cannot overwrite older evidence.
        for collision in range(100):
            suffix = f"-{collision}" if collision else ""
            final = protected / f"{prefix}{suffix}{COMPLETE_BACKUP_SUFFIX}"
            try:
                os.link(temporary, final)
                _fsync_directory(protected)
                return final
            except FileExistsError:
                continue
        raise RuntimeError("could not reserve a unique migration backup filename")
    finally:
        temporary.unlink(missing_ok=True)


def _write_complete_archive(
    database: Path,
    media_root: Path,
    output_dir: Path,
    temporary: Path,
    *,
    source_profile: str | None = None,
) -> None:
    """Snapshot DB and media together, then validate archive bytes before publish."""
    with tempfile.TemporaryDirectory(dir=output_dir) as work_dir_text:
        work_dir = Path(work_dir_text)
        snapshot = work_dir / "bridge.db"
        with (
            sqlite3.connect(database) as source,
            sqlite3.connect(snapshot) as destination,
        ):
            source.backup(destination)
        validate_snapshot(snapshot)
        if (
            source_profile is not None
            and inspect_migration_backup_source(snapshot) != source_profile
        ):
            raise RuntimeError("SQLite source profile changed while backing up")
        assets = _referenced_assets(snapshot)
        manifest: dict[str, object] = {
            "version": MANIFEST_VERSION,
            "database": "bridge.db",
            "database_sha256": _sha256(snapshot),
            "media": [],
        }
        if source_profile is not None:
            # This additive field is optional, so legacy manifest-v1 archives
            # remain readable by the original restore path.
            manifest["source_profile"] = source_profile
        media_destination = work_dir / "profile-media" / "objects"
        media_destination.mkdir(parents=True, exist_ok=True)
        for asset in assets:
            source_path = _safe_media_path(media_root, asset["storage_key"])
            if not source_path.is_file() or source_path.is_symlink():
                raise RuntimeError("referenced profile media is missing")
            digest = _sha256(source_path)
            if digest != asset["sha256_hex"]:
                raise RuntimeError("referenced profile media hash mismatch")
            destination = media_destination / asset["storage_key"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, destination)
            manifest["media"].append(asset)
        (work_dir / "manifest.json").write_text(
            json.dumps(manifest, sort_keys=True), encoding="utf-8"
        )
        with tarfile.open(temporary, "w:gz") as archive:
            archive.add(snapshot, arcname="bridge.db", recursive=False)
            archive.add(
                work_dir / "manifest.json", arcname="manifest.json", recursive=False
            )
            for asset in assets:
                storage_key = str(asset["storage_key"])
                archive.add(
                    media_destination / storage_key,
                    arcname=f"profile-media/objects/{storage_key}",
                    recursive=False,
                )
        # A valid SQLite snapshot is not enough; all tar entries, declared
        # hashes and media references must also survive extraction/validation.
        with tempfile.TemporaryDirectory(dir=output_dir) as verification_text:
            verification = Path(verification_text)
            _safe_extract(temporary, verification)
            _validate_complete_tree(verification)
        with temporary.open("rb") as file:
            os.fsync(file.fileno())


def create_backup(
    database: Path, output_dir: Path, *, now: datetime | None = None
) -> Path:
    """Create and validate one legacy online SQLite snapshot before publishing it."""
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    final_path = output_dir / f"{BACKUP_PREFIX}{timestamp}{BACKUP_SUFFIX}"
    temporary_path = output_dir / f".{final_path.name}{TEMP_SUFFIX}"
    temporary_path.unlink(missing_ok=True)
    try:
        with (
            sqlite3.connect(database) as source,
            sqlite3.connect(temporary_path) as destination,
        ):
            source.backup(destination)
        validate_snapshot(temporary_path)
        os.chmod(temporary_path, 0o600)
        temporary_path.replace(final_path)
        return final_path
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def create_complete_backup(
    database: Path,
    media_root: Path,
    output_dir: Path,
    *,
    deployment_lock_path: Path,
    media_lock_path: Path,
    lock_timeout_seconds: float = 30,
    now: datetime | None = None,
) -> Path:
    """Publish one archive containing a consistent SQLite snapshot and referenced media."""
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    final = output_dir / f"{BACKUP_PREFIX}{timestamp}{COMPLETE_BACKUP_SUFFIX}"
    temporary = output_dir / f".{final.name}{TEMP_SUFFIX}"
    temporary.unlink(missing_ok=True)
    with FileLock(deployment_lock_path).acquire(
        exclusive=False, timeout_seconds=lock_timeout_seconds
    ):
        with FileLock(media_lock_path).acquire(
            exclusive=False, timeout_seconds=lock_timeout_seconds
        ):
            _write_complete_archive(database, media_root, output_dir, temporary)
            os.chmod(temporary, 0o600)
            os.replace(temporary, final)
            _fsync_directory(output_dir)
    return final


def validate_snapshot(snapshot: Path) -> None:
    """Fail unless one SQLite snapshot opens and passes integrity checking."""
    with sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True) as connection:
        result = connection.execute("PRAGMA integrity_check").fetchone()
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
    if result is None or result[0] != "ok" or foreign_keys:
        raise RuntimeError("SQLite backup failed integrity checking")


def apply_retention(output_dir: Path, retention_count: int) -> list[Path]:
    """Delete only oldest final project backups beyond the retention count."""
    if retention_count < 1:
        raise ValueError("backup retention count must be at least 1")
    snapshots = sorted(
        [
            *output_dir.glob(f"{BACKUP_PREFIX}*{BACKUP_SUFFIX}"),
            *output_dir.glob(f"{BACKUP_PREFIX}*{COMPLETE_BACKUP_SUFFIX}"),
        ]
    )
    removed: list[Path] = []
    for path in snapshots[:-retention_count]:
        path.unlink()
        removed.append(path)
    return removed


def restore_backup(database: Path, source: Path) -> Path | None:
    """Restore only a confirmed pre-media SQLite snapshot."""
    validate_snapshot(source)
    if _snapshot_has_profile_media_references(source):
        raise RuntimeError(
            "legacy SQLite restore cannot restore profile media references"
        )
    warnings.warn(
        "restoring confirmed pre-media SQLite snapshot without profile media",
        RuntimeWarning,
        stacklevel=2,
    )
    database.parent.mkdir(parents=True, exist_ok=True)
    recovery: Path | None = None
    if database.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        recovery = database.with_name(f"{database.name}.pre-restore-{stamp}")
        database.replace(recovery)
    for suffix in ("-wal", "-shm"):
        database.with_name(database.name + suffix).unlink(missing_ok=True)
    temporary = database.with_name(f".{database.name}.restore{TEMP_SUFFIX}")
    try:
        shutil.copy2(source, temporary)
        validate_snapshot(temporary)
        os.chmod(temporary, 0o600)
        temporary.replace(database)
        _fsync_directory(database.parent)
    except Exception:
        temporary.unlink(missing_ok=True)
        if recovery is not None and not database.exists():
            recovery.replace(database)
        raise
    return recovery


def restore_complete_backup(
    database: Path,
    media_root: Path,
    source: Path,
    *,
    deployment_lock_path: Path,
    lock_timeout_seconds: float = 30,
) -> None:
    """Install one complete archive through a per-component durable journal."""
    with FileLock(deployment_lock_path).acquire(
        exclusive=True, timeout_seconds=lock_timeout_seconds
    ):
        recover_incomplete_restore(database, media_root)
        data_root = database.parent
        operation = f"restore-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}"
        staged = data_root / f".{operation}-new"
        old = data_root / f".{operation}-old"
        staged.mkdir(mode=0o700)
        old.mkdir(mode=0o700)
        _safe_extract(source, staged)
        _validate_complete_tree(staged)
        marker = data_root / RESTORE_MARKER
        components = [
            ("database", database, old / "bridge.db", staged / "bridge.db"),
            (
                "wal",
                database.with_name(database.name + "-wal"),
                old / "bridge.db-wal",
                None,
            ),
            (
                "shm",
                database.with_name(database.name + "-shm"),
                old / "bridge.db-shm",
                None,
            ),
            ("media", media_root, old / "profile-media", staged / "profile-media"),
        ]
        state: dict[str, object] = {
            "version": 2,
            "operation": operation,
            "phase": "prepared",
            "staged": staged.name,
            "old": old.name,
            "archive_sha256": _sha256(source),
            "expected_database_sha256": _sha256(staged / "bridge.db"),
            "expected_media": json.loads(
                (staged / "manifest.json").read_text(encoding="utf-8")
            ).get("media", []),
            "live_paths": {name: str(live) for name, live, _old, _new in components},
            "original_presence": {
                name: live.exists() for name, live, _old, _new in components
            },
            "moved_old": [],
            "installed_new": [],
        }
        _write_marker(marker, state)
        for name, live, retained, _new in components:
            if live.exists():
                os.replace(live, retained)
                _fsync_directory(live.parent)
                _fsync_directory(retained.parent)
            state["moved_old"].append(name)
            _write_marker(marker, state)
        for name, live, _retained, new_component in components:
            if new_component is not None and new_component.exists():
                os.replace(new_component, live)
                _fsync_directory(live.parent)
                _fsync_directory(new_component.parent)
            state["installed_new"].append(name)
            _write_marker(marker, state)
        validate_snapshot(database)
        _validate_live_media(database, media_root)
        if _sha256(database) != state["expected_database_sha256"]:
            raise RuntimeError("installed database hash mismatch")
        state["phase"] = "committed"
        _write_marker(marker, state)
        _cleanup_restore_generations(staged, old)
        marker.unlink()
        _fsync_directory(data_root)


def recover_incomplete_restore(database: Path, media_root: Path) -> None:
    """Converge an interrupted restore to verified new state or retained old state."""
    marker = database.parent / RESTORE_MARKER
    if not marker.exists():
        return
    state = json.loads(marker.read_text(encoding="utf-8"))
    staged = database.parent / state["staged"]
    old = database.parent / state["old"]
    if state.get("phase") == "committed":
        validate_snapshot(database)
        _validate_live_media(database, media_root)
        expected = state.get("expected_database_sha256")
        if expected and _sha256(database) != expected:
            raise RuntimeError("committed restore database hash mismatch")
        _cleanup_restore_generations(staged, old)
    else:
        _rollback_restore(database, media_root, old, state)
        _cleanup_restore_generations(staged, old)
    marker.unlink()
    _fsync_directory(database.parent)


def _restore_components(
    database: Path, media_root: Path
) -> list[tuple[str, Path, Path, Path | None]]:
    """Return stable live/old/new paths for every restore component."""
    data_root = database.parent
    marker = (
        json.loads((data_root / RESTORE_MARKER).read_text())
        if (data_root / RESTORE_MARKER).exists()
        else None
    )
    old = data_root / marker["old"] if marker else data_root
    staged = data_root / marker["staged"] if marker else data_root
    return [
        ("database", database, old / "bridge.db", staged / "bridge.db"),
        (
            "wal",
            database.with_name(database.name + "-wal"),
            old / "bridge.db-wal",
            None,
        ),
        (
            "shm",
            database.with_name(database.name + "-shm"),
            old / "bridge.db-shm",
            None,
        ),
        ("media", media_root, old / "profile-media", staged / "profile-media"),
    ]


def _rollback_restore(
    database: Path, media_root: Path, old: Path, state: dict[str, object]
) -> None:
    """Restore the exact pre-restore component-presence set idempotently."""
    presence = dict(state.get("original_presence", {}))
    staged = database.parent / str(state["staged"])
    components = [
        ("database", database, old / "bridge.db", staged / "bridge.db"),
        (
            "wal",
            database.with_name(database.name + "-wal"),
            old / "bridge.db-wal",
            None,
        ),
        (
            "shm",
            database.with_name(database.name + "-shm"),
            old / "bridge.db-shm",
            None,
        ),
        ("media", media_root, old / "profile-media", staged / "profile-media"),
    ]
    for name, live, retained, _new in components:
        # A crash may happen after rename but before the journal update, so the
        # filesystem is inspected only to recover a component recorded as
        # originally present; retained old state is never discarded first.
        if presence.get(name):
            if retained.exists():
                _remove_path(live)
                os.replace(retained, live)
                _fsync_directory(live.parent)
            elif not live.exists():
                raise RuntimeError(f"cannot recover original restore component: {name}")
        else:
            _remove_path(live)


def _cleanup_restore_generations(staged: Path, old: Path) -> None:
    """Remove completed generation directories without suppressing failures."""
    for path in (staged, old):
        if path.exists():
            shutil.rmtree(path)
            _fsync_directory(path.parent)


def _remove_path(path: Path) -> None:
    """Remove one file or directory when rolling back an installed component."""
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _snapshot_has_profile_media_references(snapshot: Path) -> bool:
    """Return whether a SQLite snapshot requires a matching media generation."""
    with sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "users" not in tables:
            return False
        columns = {row[1] for row in connection.execute("PRAGMA table_info(users)")}
        if not {"avatar_media_id", "header_media_id"}.issubset(columns):
            return False
        return (
            connection.execute(
                "SELECT 1 FROM users WHERE avatar_media_id IS NOT NULL OR header_media_id IS NOT NULL LIMIT 1"
            ).fetchone()
            is not None
        )


def serve(
    database: Path,
    output_dir: Path,
    interval_seconds: int,
    retention_count: int,
    *,
    media_root: Path | None = None,
    deployment_lock_path: Path | None = None,
    media_lock_path: Path | None = None,
    lock_timeout_seconds: float = 30,
) -> None:
    """Run immediate and periodic backups until SIGTERM or SIGINT."""
    if interval_seconds < 1:
        raise ValueError("backup interval must be at least 1 second")
    stop = threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    while not stop.is_set():
        try:
            if media_root and deployment_lock_path and media_lock_path:
                created = create_complete_backup(
                    database,
                    media_root,
                    output_dir,
                    deployment_lock_path=deployment_lock_path,
                    media_lock_path=media_lock_path,
                    lock_timeout_seconds=lock_timeout_seconds,
                )
            else:
                created = create_backup(database, output_dir)
            removed = apply_retention(output_dir, retention_count)
            print(f"backup created path={created}", flush=True)
            for path in removed:
                print(f"backup retention deleted path={path}", flush=True)
        except Exception as exc:
            print(f"backup failed error={type(exc).__name__}: {exc}", flush=True)
        stop.wait(interval_seconds)


def _referenced_assets(snapshot: Path) -> list[dict[str, object]]:
    """Read exactly the immutable objects referenced by one completed snapshot."""
    with sqlite3.connect(snapshot) as connection:
        has_media_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='profile_media_assets'"
        ).fetchone() is not None
        if not has_media_table:
            if inspect_migration_backup_source(snapshot) != "released_v0":
                raise RuntimeError("unsupported source without a profile media table")
            # No media schema existed in the tagged release. Inventing empty
            # profile-media rows or running a SELECT from that table is wrong.
            return []
        rows = connection.execute("""
            SELECT DISTINCT a.storage_key, a.sha256_hex, a.public_id, a.media_kind,
                            a.content_type, a.byte_size, a.width, a.height
            FROM profile_media_assets a
            JOIN users u ON u.avatar_media_id = a.id OR u.header_media_id = a.id
            ORDER BY a.storage_key
        """).fetchall()
    keys = (
        "storage_key",
        "sha256_hex",
        "public_id",
        "media_kind",
        "content_type",
        "byte_size",
        "width",
        "height",
    )
    return [dict(zip(keys, row, strict=True)) for row in rows]


def _safe_media_path(media_root: Path, storage_key: object) -> Path:
    """Resolve archive source paths without permitting traversal or symlinks."""
    key = str(storage_key)
    root = (media_root / "objects").resolve()
    candidate = root / key
    parent = candidate.parent.resolve()
    if root not in (parent, *parent.parents) or ".." in Path(key).parts:
        raise RuntimeError("unsafe profile media storage key")
    return candidate


def _safe_extract(source: Path, destination: Path) -> None:
    """Validate the exact versioned archive schema before extracting regular files."""
    destination_root = destination.resolve()
    with tarfile.open(source, "r:gz") as archive:
        members = archive.getmembers()
        by_name: dict[str, tarfile.TarInfo] = {}
        for member in members:
            name = _normalized_archive_name(member.name)
            if name in by_name:
                raise RuntimeError(f"duplicate backup archive entry: {name}")
            if not member.isfile():
                raise RuntimeError(f"unsupported backup archive entry type: {name}")
            by_name[name] = member

        manifest_member = by_name.get("manifest.json")
        if manifest_member is None:
            raise RuntimeError("backup archive is missing manifest.json")
        manifest_file = archive.extractfile(manifest_member)
        if manifest_file is None:
            raise RuntimeError("backup manifest is unreadable")
        try:
            manifest = json.load(manifest_file)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
            raise RuntimeError("backup manifest is invalid") from exc

        permitted = _manifest_member_names(manifest)
        actual = set(by_name)
        missing = permitted - actual
        additional = actual - permitted
        if missing:
            raise RuntimeError(
                f"backup archive is missing declared entries: {sorted(missing)}"
            )
        if additional:
            raise RuntimeError(
                f"backup archive contains unexpected entries: {sorted(additional)}"
            )

        for name, member in by_name.items():
            target = destination / name
            resolved = target.resolve()
            if destination_root not in (resolved, *resolved.parents):
                raise RuntimeError("unsafe backup archive entry")
            target.parent.mkdir(parents=True, exist_ok=True)
            source_file = archive.extractfile(member)
            if source_file is None:
                raise RuntimeError(f"backup archive entry is unreadable: {name}")
            with target.open("xb") as output:
                shutil.copyfileobj(source_file, output)


def _normalized_archive_name(raw_name: str) -> str:
    """Return one canonical relative POSIX archive path or reject it."""
    from pathlib import PurePosixPath

    if not raw_name or raw_name in {".", "./"} or "\x00" in raw_name:
        raise RuntimeError("unsafe backup archive entry name")
    path = PurePosixPath(raw_name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise RuntimeError("unsafe backup archive entry name")
    normalized = path.as_posix()
    if normalized != raw_name:
        raise RuntimeError("non-canonical backup archive entry name")
    return normalized


def _manifest_member_names(manifest: object) -> set[str]:
    """Derive the exact permitted archive member set from a valid manifest shape."""
    if not isinstance(manifest, dict) or manifest.get("version") != MANIFEST_VERSION:
        raise RuntimeError("unsupported backup manifest version")
    database_name = manifest.get("database")
    if database_name != "bridge.db":
        raise RuntimeError("unsupported backup database filename")
    media = manifest.get("media")
    if not isinstance(media, list):
        raise RuntimeError("backup manifest media must be a list")
    permitted = {"manifest.json", database_name}
    public_ids: set[str] = set()
    storage_keys: set[str] = set()
    for record in media:
        if not isinstance(record, dict):
            raise RuntimeError("backup manifest media record is invalid")
        storage_key = record.get("storage_key")
        public_id = record.get("public_id")
        sha256_hex = record.get("sha256_hex")
        byte_size = record.get("byte_size")
        if (
            not isinstance(storage_key, str)
            or _normalized_archive_name(storage_key) != storage_key
        ):
            raise RuntimeError("backup manifest media storage key is invalid")
        if (
            storage_key in storage_keys
            or not isinstance(public_id, str)
            or public_id in public_ids
        ):
            raise RuntimeError("backup manifest contains duplicate media identity")
        if not isinstance(sha256_hex, str) or len(sha256_hex) != 64:
            raise RuntimeError("backup manifest media hash is invalid")
        if (
            not isinstance(byte_size, int)
            or isinstance(byte_size, bool)
            or byte_size < 0
        ):
            raise RuntimeError("backup manifest media size is invalid")
        storage_keys.add(storage_key)
        public_ids.add(public_id)
        permitted.add(f"profile-media/objects/{storage_key}")
    return permitted


def _validate_complete_tree(root: Path) -> None:
    """Validate the manifest against snapshot metadata and every extracted byte."""
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    _manifest_member_names(manifest)
    database = root / str(manifest["database"])
    validate_snapshot(database)
    if _sha256(database) != manifest.get("database_sha256"):
        raise RuntimeError("backup database hash mismatch")
    source_profile = manifest.get("source_profile")
    if source_profile is not None and (
        source_profile not in {"released_v0", "known_main_v0", "ready_v1"}
        or inspect_migration_backup_source(database) != source_profile
    ):
        raise RuntimeError("backup source profile does not match SQLite schema")

    snapshot_assets = {
        str(item["storage_key"]): item for item in _referenced_assets(database)
    }
    manifest_assets = {str(item["storage_key"]): item for item in manifest["media"]}
    if set(snapshot_assets) != set(manifest_assets):
        raise RuntimeError(
            "backup manifest media set does not match database references"
        )
    for storage_key, expected in snapshot_assets.items():
        declared = manifest_assets[storage_key]
        for field in (
            "sha256_hex",
            "public_id",
            "media_kind",
            "content_type",
            "byte_size",
            "width",
            "height",
        ):
            if declared.get(field) != expected[field]:
                raise RuntimeError(f"backup manifest media metadata mismatch: {field}")
        path = _safe_media_path(root / "profile-media", storage_key)
        if not path.is_file() or path.is_symlink():
            raise RuntimeError("backup archive is missing declared media bytes")
        if path.stat().st_size != declared["byte_size"]:
            raise RuntimeError("backup media size mismatch")
        if _sha256(path) != declared["sha256_hex"]:
            raise RuntimeError("backup media hash mismatch")


def _validate_live_media(database: Path, media_root: Path) -> None:
    """Verify every committed media reference has matching immutable bytes."""
    for asset in _referenced_assets(database):
        path = _safe_media_path(media_root, asset["storage_key"])
        if not path.is_file() or _sha256(path) != asset["sha256_hex"]:
            raise RuntimeError("restored profile media verification failed")


def _sha256(path: Path) -> str:
    """Hash one file without loading it fully into memory."""
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _write_marker(marker: Path, state: dict[str, object]) -> None:
    """Durably replace restore-state metadata before the next rename boundary."""
    temporary = marker.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, marker)
    _fsync_directory(marker.parent)


def _fsync_directory(path: Path) -> None:
    """Persist directory entry changes used by backup and restore publication."""
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _parser() -> argparse.ArgumentParser:
    """Build the secret-independent backup/restore CLI parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("backup", "serve"):
        command = subparsers.add_parser(name)
        command.add_argument("--database", type=Path, required=True)
        command.add_argument("--output-dir", type=Path, required=True)
        command.add_argument("--media-root", type=Path)
        command.add_argument("--deployment-lock", type=Path)
        command.add_argument("--media-lock", type=Path)
        command.add_argument("--lock-timeout-seconds", type=float, default=30)
        if name == "serve":
            command.add_argument("--interval-seconds", type=int, default=86400)
            command.add_argument("--retention-count", type=int, default=14)
    restore = subparsers.add_parser("restore")
    restore.add_argument("--database", type=Path, required=True)
    restore.add_argument("--media-root", type=Path)
    restore.add_argument("--deployment-lock", type=Path)
    restore.add_argument("--source", type=Path, required=True)
    restore.add_argument("--lock-timeout-seconds", type=float, default=30)
    return parser


def main() -> None:
    """Execute one backup CLI command without constructing application Settings."""
    args = _parser().parse_args()
    if args.command == "backup":
        if args.media_root and args.deployment_lock and args.media_lock:
            print(
                create_complete_backup(
                    args.database,
                    args.media_root,
                    args.output_dir,
                    deployment_lock_path=args.deployment_lock,
                    media_lock_path=args.media_lock,
                    lock_timeout_seconds=args.lock_timeout_seconds,
                )
            )
        else:
            print(create_backup(args.database, args.output_dir))
    elif args.command == "serve":
        serve(
            args.database,
            args.output_dir,
            args.interval_seconds,
            args.retention_count,
            media_root=args.media_root,
            deployment_lock_path=args.deployment_lock,
            media_lock_path=args.media_lock,
            lock_timeout_seconds=args.lock_timeout_seconds,
        )
    elif (
        args.media_root
        and args.deployment_lock
        and args.source.name.endswith(COMPLETE_BACKUP_SUFFIX)
    ):
        restore_complete_backup(
            args.database,
            args.media_root,
            args.source,
            deployment_lock_path=args.deployment_lock,
            lock_timeout_seconds=args.lock_timeout_seconds,
        )
        print("complete backup restored")
    else:
        recovery = restore_backup(args.database, args.source)
        print(recovery or "restored without prior live database")


if __name__ == "__main__":
    main()
