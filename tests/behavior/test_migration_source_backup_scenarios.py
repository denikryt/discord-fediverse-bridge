"""Runtime behavior for exact source-aware, no-clobber migration archives."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tarfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import text

from src.db import Database
from src.db.backup import (
    apply_retention,
    create_complete_backup,
    create_migration_backup,
    inspect_migration_backup_source,
    restore_complete_backup,
)
from src.profile_media import FileLock


_FIXED_TIME = datetime(2026, 11, 11, 0, tzinfo=timezone.utc)
_RELEASE_SQL = Path(__file__).parents[1] / "fixtures/database/released_v0_schema.sql"
_KNOWN_MAIN_SQL = Path(__file__).parents[1] / "fixtures/database/known_main_v0_schema.sql"


def _tagged_release_database(tmp_path: Path) -> Path:
    """Produce real release ORM DDL from the tag-pinned SQLite test snapshot."""
    path = tmp_path / "release-v0.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(_RELEASE_SQL.read_text(encoding="utf-8"))
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
    return path


def _known_main_database_with_media(tmp_path: Path) -> tuple[Path, Path, bytes]:
    """Populate one canonical main-schema writer with referenced profile bytes."""
    path = tmp_path / "main-v0.db"
    root = tmp_path / "media"
    # Restore the actual old-main migrated schema rather than asking the new
    # versioned runner to construct an old, unversioned database.
    with sqlite3.connect(path) as connection:
        connection.executescript(_KNOWN_MAIN_SQL.read_text(encoding="utf-8"))
    database = Database(f"sqlite:///{path}")
    database.users.create_user(
        discord_user_id="user-1", activitypub_username="alice",
        actor_url="https://bridge.example/actors/users/alice",
        inbox_url="https://bridge.example/actors/users/alice/inbox",
        outbox_url="https://bridge.example/actors/users/alice/outbox",
        followers_url="https://bridge.example/actors/users/alice/followers",
        public_key_pem="fixture-public-key", private_key_pem="fixture-private-key",
    )
    image = b"known-main-media-bytes"
    digest = hashlib.sha256(image).hexdigest()
    with database.engine.begin() as connection:
        connection.execute(text("""
            INSERT INTO profile_media_assets
                (id, public_id, storage_key, media_kind, content_type,
                 byte_size, width, height, sha256_hex, created_at)
            VALUES (7, 'avatar-7', 'aa/test-media', 'avatar', 'image/jpeg',
                    :byte_size, 1, 1, :digest, CURRENT_TIMESTAMP)
        """), {"byte_size": len(image), "digest": digest})
        connection.execute(text("UPDATE users SET avatar_media_id=7 WHERE discord_user_id='user-1'"))
    database.close()
    output = root / "objects" / "aa" / "test-media"
    output.parent.mkdir(parents=True)
    output.write_bytes(image)
    return path, root, image


def _migration_backup(
    database: Path, media_root: Path, tmp_path: Path, *, now: datetime = _FIXED_TIME
) -> Path:
    """Exercise the protected, caller-held EX deployment lease entrypoint."""
    with FileLock(tmp_path / "deploy.lock").acquire(exclusive=True):
        return create_migration_backup(
            database, media_root, tmp_path / "backups",
            media_lock_path=tmp_path / "media.lock", now=now,
        )


def _manifest(archive: Path) -> dict[str, object]:
    """Read metadata through the archive boundary rather than a temp sidecar."""
    with tarfile.open(archive, "r:gz") as tar:
        stream = tar.extractfile("manifest.json")
        assert stream is not None
        return json.loads(stream.read())


def test_two_tagged_release_migration_backups_cannot_clobber_and_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two same-second, same-token backups preserve both restore-capable artifacts."""
    database = _tagged_release_database(tmp_path)
    assert inspect_migration_backup_source(database) == "released_v0"
    # Force an identical candidate path to exercise atomic link collision
    # handling instead of trusting randomness or timestamps to be unique.
    monkeypatch.setattr("src.db.backup.secrets.token_hex", lambda _count: "fixedtoken")
    first = _migration_backup(database, tmp_path / "unused-media", tmp_path)
    first_bytes = first.read_bytes()
    second = _migration_backup(database, tmp_path / "unused-media", tmp_path)
    assert first != second
    assert first.read_bytes() == first_bytes
    assert first.parent == second.parent == tmp_path / "backups" / "migrations"
    assert "-1.tar.gz" in second.name
    assert apply_retention(tmp_path / "backups", 1) == []

    for index, backup in enumerate((first, second)):
        manifest = _manifest(backup)
        assert manifest["source_profile"] == "released_v0"
        assert manifest["media"] == []
        restored = tmp_path / f"restored-{index}.db"
        restore_complete_backup(
            restored, tmp_path / f"restored-media-{index}", backup,
            deployment_lock_path=tmp_path / f"restore-{index}.lock",
        )
        assert inspect_migration_backup_source(restored) == "released_v0"
        with sqlite3.connect(restored) as connection:
            assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_main_source_migration_backup_preserves_all_referenced_media(
    tmp_path: Path,
) -> None:
    """A known-main source restores profile bytes alongside its SQL references."""
    database, media_root, media = _known_main_database_with_media(tmp_path)
    assert inspect_migration_backup_source(database) == "known_main_v0"
    archive = _migration_backup(database, media_root, tmp_path)
    assert _manifest(archive)["source_profile"] == "known_main_v0"
    assert len(_manifest(archive)["media"]) == 1
    restored_media = tmp_path / "restored-media"
    restored_db = tmp_path / "restored.db"
    restore_complete_backup(
        restored_db, restored_media, archive,
        deployment_lock_path=tmp_path / "restore.lock",
    )
    assert inspect_migration_backup_source(restored_db) == "known_main_v0"
    assert (restored_media / "objects/aa/test-media").read_bytes() == media
    with sqlite3.connect(restored_db) as connection:
        assert connection.execute("SELECT avatar_media_id FROM users WHERE discord_user_id='user-1'").fetchone() == (7,)


def test_unknown_or_wrong_version_source_fails_before_backup_publication(
    tmp_path: Path,
) -> None:
    """Arbitrary SQLite and future DB versions cannot enter the upgrade pipeline."""
    unknown = tmp_path / "unknown.db"
    with sqlite3.connect(unknown) as connection:
        connection.execute("CREATE TABLE profile_media_assets (name TEXT)")
        connection.execute("INSERT INTO profile_media_assets VALUES ('unchanged')")
    with pytest.raises(RuntimeError, match="unsupported or modified"):
        _migration_backup(unknown, tmp_path / "media", tmp_path)
    with sqlite3.connect(unknown) as connection:
        assert connection.execute("SELECT name FROM profile_media_assets").fetchone() == ("unchanged",)
    assert not (tmp_path / "backups" / "migrations").exists()

    release = _tagged_release_database(tmp_path)
    with sqlite3.connect(release) as connection:
        connection.execute("PRAGMA user_version=2")
    with pytest.raises(RuntimeError, match="unversioned v0"):
        _migration_backup(release, tmp_path / "media", tmp_path)
    assert not (tmp_path / "backups" / "migrations").exists()


def test_missing_or_corrupted_referenced_media_refuses_migration_snapshot(
    tmp_path: Path,
) -> None:
    """A failed verification leaves source rows unchanged and no final archive."""
    database, media_root, media = _known_main_database_with_media(tmp_path)
    stored_path = media_root / "objects/aa/test-media"
    stored_path.unlink()
    with pytest.raises(RuntimeError, match="referenced profile media is missing"):
        _migration_backup(database, media_root, tmp_path)
    assert list((tmp_path / "backups" / "migrations").glob("*")) == []
    stored_path.write_bytes(b"corrupted")
    with pytest.raises(RuntimeError, match="hash mismatch"):
        _migration_backup(database, media_root, tmp_path)
    stored_path.write_bytes(media)
    archive = _migration_backup(database, media_root, tmp_path)
    assert archive.is_file()
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT avatar_media_id FROM users WHERE discord_user_id='user-1'").fetchone() == (7,)


def test_existing_complete_archive_manifest_format_stays_compatible(
    tmp_path: Path,
) -> None:
    """Normal backup/restore remains manifest v1 without new migration metadata."""
    database = _tagged_release_database(tmp_path)
    archive = create_complete_backup(
        database, tmp_path / "unused-media", tmp_path / "backups",
        deployment_lock_path=tmp_path / "deploy.lock",
        media_lock_path=tmp_path / "media.lock", now=_FIXED_TIME,
    )
    manifest = _manifest(archive)
    assert manifest["version"] == 1
    assert "source_profile" not in manifest
    restore_complete_backup(
        tmp_path / "restored.db", tmp_path / "restored-media", archive,
        deployment_lock_path=tmp_path / "restore.lock",
    )
    assert inspect_migration_backup_source(tmp_path / "restored.db") == "released_v0"


def test_altered_release_schema_and_redirected_backup_dir_fail_closed(
    tmp_path: Path,
) -> None:
    """A release-like but changed schema or symlink output is never admitted."""
    release = _tagged_release_database(tmp_path)
    with sqlite3.connect(release) as connection:
        connection.execute("ALTER TABLE users ADD COLUMN injected TEXT")
    with pytest.raises(RuntimeError, match="unsupported or modified"):
        _migration_backup(release, tmp_path / "media", tmp_path)
    assert not (tmp_path / "backups" / "migrations").exists()

    release.unlink()
    release = _tagged_release_database(tmp_path)
    redirected = tmp_path / "redirected-output"
    redirected.mkdir()
    (tmp_path / "backups").symlink_to(redirected, target_is_directory=True)
    with pytest.raises(RuntimeError, match="must not be a symlink"):
        _migration_backup(release, tmp_path / "media", tmp_path)
    assert list(redirected.iterdir()) == []


def test_restore_rejects_incorrect_migration_source_profile_before_live_changes(
    tmp_path: Path,
) -> None:
    """A valid DB digest with a false source declaration is not restorable."""
    from io import BytesIO

    release = _tagged_release_database(tmp_path)
    original = _migration_backup(release, tmp_path / "media", tmp_path)
    tampered = tmp_path / "tampered.tar.gz"
    with tarfile.open(original, "r:gz") as source, tarfile.open(tampered, "w:gz") as output:
        for member in source.getmembers():
            stream = source.extractfile(member)
            assert stream is not None
            contents = stream.read()
            if member.name == "manifest.json":
                manifest = json.loads(contents)
                manifest["source_profile"] = "known_main_v0"
                contents = json.dumps(manifest, sort_keys=True).encode("utf-8")
                member.size = len(contents)
            output.addfile(member, BytesIO(contents))

    live = tmp_path / "live.db"
    with sqlite3.connect(live) as connection:
        connection.execute("CREATE TABLE retained (value TEXT)")
        connection.execute("INSERT INTO retained VALUES ('unchanged')")
    with pytest.raises(RuntimeError, match="backup source profile does not match"):
        restore_complete_backup(
            live, tmp_path / "restored-media", tampered,
            deployment_lock_path=tmp_path / "restore.lock",
        )
    with sqlite3.connect(live) as connection:
        assert connection.execute("SELECT value FROM retained").fetchone() == ("unchanged",)
