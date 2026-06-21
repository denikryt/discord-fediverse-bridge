"""Behavior scenarios for complete DB-plus-profile-media backup and restore."""

from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

from PIL import Image

from src.db import Database
from src.db.backup import create_complete_backup, restore_complete_backup
from src.models import PublishedActivityObject
from src.profile_media import ProfileMediaStore
from src.user_profiles import UserProfileService


def _image() -> BytesIO:
    """Create one valid image at the external acquisition boundary."""
    stream = BytesIO()
    Image.new("RGB", (12, 12), "white").save(stream, "JPEG")
    stream.seek(0)
    return stream


def test_complete_backup_restores_matching_database_and_media_generation(
    tmp_path: Path,
) -> None:
    """A published archive restores every media object referenced by its DB snapshot."""
    data = tmp_path / "data"
    data.mkdir()
    database_path = data / "bridge.db"
    media_root = data / "profile-media"
    database = Database(f"sqlite:///{database_path}")
    database.create_all()
    database.migrate()
    user = database.users.create_user(
        discord_user_id="1",
        activitypub_username="alice",
        actor_url="https://x/actors/users/alice",
        inbox_url="https://x/actors/users/alice/inbox",
        outbox_url="https://x/actors/users/alice/outbox",
        followers_url="https://x/actors/users/alice/followers",
        public_key_pem="p",
        private_key_pem="s",
    )
    store = ProfileMediaStore(
        media_root, max_upload_bytes=1_048_576, max_pixels=1_000_000
    )
    store.initialize()
    profile = UserProfileService(database, store).replace_media(
        user.id, "avatar", _image()
    )
    original_digest = profile.avatar.sha256_hex
    community = database.activitypub_objects.create_published_activity_object(
        actor_username=user.activitypub_username,
        actor_url=user.actor_url,
        community_actor_url="https://x/communities/test",
        activity_id="https://x/activities/community",
        object_id="https://x/objects/community",
        kind="post",
        title=None,
        body_markdown="community",
        in_reply_to_object_id=None,
        discord_channel_id=10,
        discord_message_id=11,
        published_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    with database.session() as session:
        personal = PublishedActivityObject(
            actor_username=user.activitypub_username,
            actor_url=user.actor_url,
            publication_context="personal_feed",
            community_actor_url=None,
            activity_id="https://x/activities/personal",
            object_id="https://x/objects/personal",
            kind="comment",
            title=None,
            body_markdown="personal",
            in_reply_to_object_id=community.object_id,
            discord_guild_id=2,
            discord_channel_id=20,
            discord_message_id=21,
            published_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
        )
        deleted = PublishedActivityObject(
            actor_username=user.activitypub_username,
            actor_url=user.actor_url,
            publication_context="personal_feed",
            community_actor_url=None,
            activity_id="https://x/activities/deleted",
            object_id="https://x/objects/deleted",
            kind="post",
            title=None,
            body_markdown="deleted",
            discord_guild_id=2,
            discord_channel_id=20,
            discord_message_id=22,
            published_at=datetime(2026, 1, 3, tzinfo=timezone.utc),
            deleted_at=datetime(2026, 1, 4, tzinfo=timezone.utc),
        )
        session.add_all([personal, deleted])
    database.close()

    archive = create_complete_backup(
        database_path,
        media_root,
        tmp_path / "backups",
        deployment_lock_path=data / "deployment-state.lock",
        media_lock_path=data / "profile-media.lock",
        now=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    database_path.unlink()
    import shutil

    shutil.rmtree(media_root)
    restore_complete_backup(
        database_path,
        media_root,
        archive,
        deployment_lock_path=data / "deployment-state.lock",
    )
    restored = Database(f"sqlite:///{database_path}")
    restored_profile = restored.user_profiles.get(user.id)
    assert restored_profile is not None
    assert restored_profile.avatar.sha256_hex == original_digest
    assert (media_root / "objects" / restored_profile.avatar.storage_key).is_file()
    restored_user = restored.users.get_user_by_activitypub_username("alice")
    assert restored_user is not None
    outbox = restored.activitypub_objects.load_user_outbox_slice(
        user=restored_user, offset=0, limit=40
    )
    assert outbox.total_items == 2
    assert [row.object_id for row in outbox.items] == [
        "https://x/objects/personal",
        "https://x/objects/community",
    ]
    deleted_row = (
        restored.activitypub_objects.get_published_activity_object_by_object_id(
            "https://x/objects/deleted"
        )
    )
    assert deleted_row is not None and deleted_row.deleted_at is not None


def test_backup_compose_process_has_no_application_secret_environment() -> None:
    """The long-lived backup process receives paths and timing, not bridge secrets."""
    compose = Path("compose.yaml").read_text(encoding="utf-8")
    backup = compose.split("\n  backup:\n", 1)[1].split("\nvolumes:\n", 1)[0]
    assert "env_file:" not in backup
    assert "--media-root" in backup
    assert "--deployment-lock" in backup
    assert "--media-lock" in backup
    for secret_name in (
        "DISCORD_TOKEN",
        "FEDIFY_SHARED_SECRET",
        "DISCORD_OAUTH_CLIENT_SECRET",
    ):
        assert secret_name not in backup


def test_legacy_restore_rejects_snapshot_with_profile_media_references(
    tmp_path: Path,
) -> None:
    """SQLite-only restore cannot publish a post-media snapshot without matching bytes."""
    import pytest
    from src.db.backup import create_backup, restore_backup

    data = tmp_path / "data"
    data.mkdir()
    database_path = data / "bridge.db"
    media_root = data / "profile-media"
    database = Database(f"sqlite:///{database_path}")
    database.create_all()
    database.migrate()
    user = database.users.create_user(
        discord_user_id="1",
        activitypub_username="alice",
        actor_url="https://x/actors/users/alice",
        inbox_url="https://x/actors/users/alice/inbox",
        outbox_url="https://x/actors/users/alice/outbox",
        followers_url="https://x/actors/users/alice/followers",
        public_key_pem="p",
        private_key_pem="s",
    )
    store = ProfileMediaStore(
        media_root, max_upload_bytes=1_048_576, max_pixels=1_000_000
    )
    store.initialize()
    UserProfileService(database, store).replace_media(user.id, "avatar", _image())
    database.close()
    snapshot = create_backup(database_path, tmp_path / "legacy")
    with pytest.raises(RuntimeError, match="profile media"):
        restore_backup(tmp_path / "restored.db", snapshot)


def test_recovery_restores_old_database_after_crash_between_component_moves(
    tmp_path: Path,
) -> None:
    """Recovery never deletes the sole old DB when a crash precedes its journal update."""
    import json
    import os
    from src.db.backup import RESTORE_MARKER, recover_incomplete_restore

    data = tmp_path / "data"
    data.mkdir()
    database = data / "bridge.db"
    database.write_bytes(b"old-db")
    media = data / "profile-media"
    media.mkdir()
    (media / "sentinel").write_text("old", encoding="utf-8")
    old = data / ".restore-test-old"
    staged = data / ".restore-test-new"
    old.mkdir()
    staged.mkdir()
    state = {
        "version": 2,
        "operation": "restore-test",
        "phase": "prepared",
        "staged": staged.name,
        "old": old.name,
        "original_presence": {
            "database": True,
            "wal": False,
            "shm": False,
            "media": True,
        },
        "moved_old": [],
        "installed_new": [],
        "archive_sha256": "unused",
    }
    (data / RESTORE_MARKER).write_text(json.dumps(state), encoding="utf-8")
    os.replace(database, old / "bridge.db")
    recover_incomplete_restore(database, media)
    assert database.read_bytes() == b"old-db"
    assert (media / "sentinel").read_text(encoding="utf-8") == "old"
    assert not (data / RESTORE_MARKER).exists()


def test_recovery_rolls_back_every_uncommitted_component_boundary(
    tmp_path: Path,
) -> None:
    """Each component rename boundary converges to the exact retained old generation."""
    import json
    import os
    from src.db.backup import RESTORE_MARKER, recover_incomplete_restore

    for boundary in range(1, 7):
        data = tmp_path / f"case-{boundary}"
        data.mkdir()
        database = data / "bridge.db"
        database.write_bytes(b"old-db")
        wal = data / "bridge.db-wal"
        wal.write_bytes(b"old-wal")
        shm = data / "bridge.db-shm"
        shm.write_bytes(b"old-shm")
        media = data / "profile-media"
        media.mkdir()
        (media / "old").write_text("old")
        old = data / ".restore-old"
        old.mkdir()
        staged = data / ".restore-new"
        staged.mkdir()
        (staged / "bridge.db").write_bytes(b"new-db")
        (staged / "profile-media").mkdir()
        (staged / "profile-media" / "new").write_text("new")
        components = [
            ("database", database, old / "bridge.db", staged / "bridge.db"),
            ("wal", wal, old / "bridge.db-wal", None),
            ("shm", shm, old / "bridge.db-shm", None),
            ("media", media, old / "profile-media", staged / "profile-media"),
        ]
        state = {
            "version": 2,
            "operation": "restore",
            "phase": "prepared",
            "staged": staged.name,
            "old": old.name,
            "original_presence": {name: True for name, *_ in components},
            "moved_old": [],
            "installed_new": [],
            "archive_sha256": "x",
        }
        (data / RESTORE_MARKER).write_text(json.dumps(state))
        actions = []
        for name, live, retained, new in components:
            actions.append(("old", name, live, retained))
        actions.extend(
            ("new", name, new, live)
            for name, live, _retained, new in components
            if new is not None
        )
        for index, (kind, name, source, target) in enumerate(actions[:boundary]):
            if source is not None and source.exists():
                os.replace(source, target)
            if kind == "old":
                state["moved_old"].append(name)
            else:
                state["installed_new"].append(name)
            # Simulate both sides of the durable marker boundary on alternating cases.
            if index < boundary - 1 or boundary % 2 == 0:
                (data / RESTORE_MARKER).write_text(json.dumps(state))
        recover_incomplete_restore(database, media)
        assert database.read_bytes() == b"old-db"
        assert wal.read_bytes() == b"old-wal"
        assert shm.read_bytes() == b"old-shm"
        assert (media / "old").read_text() == "old"
        assert not (data / RESTORE_MARKER).exists()


def _rewrite_archive(
    source: Path,
    destination: Path,
    *,
    duplicate_name: str | None = None,
    extra_member: tuple[str, bytes] | None = None,
    omit_name: str | None = None,
    special_member: tuple[str, bytes, int, str | None] | None = None,
) -> None:
    """Rewrite a valid archive with one deliberate structural defect."""
    import io
    import tarfile

    with tarfile.open(source, "r:gz") as archive:
        records = []
        for member in archive.getmembers():
            extracted = archive.extractfile(member)
            records.append(
                (member.name, extracted.read() if extracted is not None else b"")
            )

    with tarfile.open(destination, "w:gz") as archive:
        for name, content in records:
            if name == omit_name:
                continue
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
            if name == duplicate_name:
                duplicate = tarfile.TarInfo(name)
                duplicate.size = len(content)
                archive.addfile(duplicate, io.BytesIO(content))
        if extra_member is not None:
            name, content = extra_member
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
        if special_member is not None:
            name, content, member_type, linkname = special_member
            info = tarfile.TarInfo(name)
            info.type = member_type
            info.linkname = linkname or ""
            info.size = len(content) if member_type == tarfile.REGTYPE else 0
            archive.addfile(info, io.BytesIO(content) if info.size else None)


def _archive_with_media(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Create one valid complete archive and return live DB/media paths."""
    data = tmp_path / "data"
    data.mkdir()
    database_path = data / "bridge.db"
    media_root = data / "profile-media"
    database = Database(f"sqlite:///{database_path}")
    database.create_all()
    database.migrate()
    user = database.users.create_user(
        discord_user_id="1",
        activitypub_username="alice",
        actor_url="https://x/actors/users/alice",
        inbox_url="https://x/actors/users/alice/inbox",
        outbox_url="https://x/actors/users/alice/outbox",
        followers_url="https://x/actors/users/alice/followers",
        public_key_pem="p",
        private_key_pem="s",
    )
    store = ProfileMediaStore(
        media_root, max_upload_bytes=1_048_576, max_pixels=1_000_000
    )
    store.initialize()
    UserProfileService(database, store).replace_media(user.id, "avatar", _image())
    database.close()
    archive = create_complete_backup(
        database_path,
        media_root,
        tmp_path / "backups",
        deployment_lock_path=data / "deployment-state.lock",
        media_lock_path=data / "profile-media.lock",
        now=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    return archive, database_path, media_root


def _assert_invalid_archive_preserves_live_state(
    archive: Path,
    database_path: Path,
    media_root: Path,
    data_root: Path,
) -> None:
    """Reject malformed input before any live-generation rename."""
    import pytest

    database_before = database_path.read_bytes()
    media_files = [
        path for path in (media_root / "objects").rglob("*") if path.is_file()
    ]
    media_before = {
        path.relative_to(media_root): path.read_bytes() for path in media_files
    }
    with pytest.raises(RuntimeError):
        restore_complete_backup(
            database_path,
            media_root,
            archive,
            deployment_lock_path=data_root / "deployment-state.lock",
        )
    assert database_path.read_bytes() == database_before
    assert {
        path.relative_to(media_root): path.read_bytes() for path in media_files
    } == media_before


def test_restore_rejects_duplicate_manifest_database_and_media_entries(
    tmp_path: Path,
) -> None:
    """Security-sensitive duplicate names are rejected rather than resolved by tar ordering."""
    import tarfile

    source, database_path, media_root = _archive_with_media(tmp_path)
    with tarfile.open(source, "r:gz") as archive:
        media_name = next(
            member.name
            for member in archive.getmembers()
            if member.name.startswith("profile-media/objects/")
        )
    for index, name in enumerate(("manifest.json", "bridge.db", media_name)):
        malformed = tmp_path / f"duplicate-{index}.tar.gz"
        _rewrite_archive(source, malformed, duplicate_name=name)
        _assert_invalid_archive_preserves_live_state(
            malformed, database_path, media_root, database_path.parent
        )


def test_restore_rejects_extra_and_missing_archive_entries(tmp_path: Path) -> None:
    """The extracted member set must exactly equal the manifest-derived schema."""
    import tarfile

    source, database_path, media_root = _archive_with_media(tmp_path)
    extra = tmp_path / "extra.tar.gz"
    _rewrite_archive(source, extra, extra_member=("unexpected.txt", b"unexpected"))
    _assert_invalid_archive_preserves_live_state(
        extra, database_path, media_root, database_path.parent
    )

    with tarfile.open(source, "r:gz") as archive:
        media_name = next(
            member.name
            for member in archive.getmembers()
            if member.name.startswith("profile-media/objects/")
        )
    for index, name in enumerate(("bridge.db", media_name)):
        missing = tmp_path / f"missing-{index}.tar.gz"
        _rewrite_archive(source, missing, omit_name=name)
        _assert_invalid_archive_preserves_live_state(
            missing, database_path, media_root, database_path.parent
        )


def test_restore_rejects_links_devices_fifos_and_directory_entries(
    tmp_path: Path,
) -> None:
    """Only canonical regular files from the manifest schema are accepted."""
    import tarfile

    source, database_path, media_root = _archive_with_media(tmp_path)
    special_types = (
        (tarfile.SYMTYPE, "bridge.db"),
        (tarfile.LNKTYPE, "bridge.db"),
        (tarfile.CHRTYPE, None),
        (tarfile.BLKTYPE, None),
        (tarfile.FIFOTYPE, None),
        (tarfile.DIRTYPE, None),
    )
    for index, (member_type, linkname) in enumerate(special_types):
        malformed = tmp_path / f"special-{index}.tar.gz"
        _rewrite_archive(
            source,
            malformed,
            special_member=(f"special-{index}", b"", member_type, linkname),
        )
        _assert_invalid_archive_preserves_live_state(
            malformed, database_path, media_root, database_path.parent
        )
