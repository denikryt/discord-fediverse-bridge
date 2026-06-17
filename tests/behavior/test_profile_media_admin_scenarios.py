"""Behavior tests for the destructive profile-media cleanup adapter."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.profile_media import FileLock
from src.profile_media_admin import main


def _args(tmp_path: Path) -> list[str]:
    """Return one valid cleanup invocation rooted in the test data directory."""
    return [
        "cleanup",
        "--database-url",
        f"sqlite:///{tmp_path / 'bridge.db'}",
        "--media-root",
        str(tmp_path / "media"),
        "--deployment-lock",
        str(tmp_path / "deployment.lock"),
        "--media-lock",
        str(tmp_path / "media.lock"),
    ]


def test_cleanup_subcommand_and_non_negative_grace_are_required(tmp_path) -> None:
    """Typos and negative retention windows fail before database creation."""
    with pytest.raises(SystemExit):
        main([])
    with pytest.raises(SystemExit):
        main(_args(tmp_path) + ["--grace-seconds", "-1"])
    assert not (tmp_path / "bridge.db").exists()


def test_cleanup_rejects_symlinked_media_root_before_database_or_deletion(
    tmp_path,
) -> None:
    """A redirected storage root cannot become the owner of destructive cleanup."""
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "keep.txt"
    sentinel.write_text("keep")
    (tmp_path / "media").symlink_to(outside, target_is_directory=True)
    with pytest.raises(RuntimeError, match="symlink"):
        main(_args(tmp_path))
    assert sentinel.read_text() == "keep"
    assert not (tmp_path / "bridge.db").exists()


def test_cleanup_rejects_symlinked_lock_path_before_database(tmp_path) -> None:
    """Lock redirection is rejected before the cleanup transaction can open."""
    target = tmp_path / "real.lock"
    target.touch()
    (tmp_path / "media.lock").symlink_to(target)
    with pytest.raises(SystemExit, match="symlink"):
        main(_args(tmp_path))
    assert not (tmp_path / "bridge.db").exists()


def test_cleanup_rejects_unwritable_or_wrong_owner_storage_before_database(
    tmp_path, monkeypatch
) -> None:
    """Storage access and ownership failures stop before durable state opens."""
    import src.profile_media as profile_media

    root = tmp_path / "media"
    root.mkdir()
    monkeypatch.setattr(profile_media.os, "access", lambda *_args: False)
    with pytest.raises(RuntimeError, match="writable"):
        main(_args(tmp_path))
    assert not (tmp_path / "bridge.db").exists()


def test_cleanup_lock_contention_cannot_delete_files(tmp_path) -> None:
    """A held media lock makes cleanup fail without touching candidate bytes."""
    media = tmp_path / "media"
    (media / "objects").mkdir(parents=True)
    sentinel = media / "objects" / "keep.txt"
    sentinel.write_text("keep")
    held = FileLock(tmp_path / "media.lock").acquire(exclusive=True)
    try:
        with pytest.raises(TimeoutError):
            main(_args(tmp_path) + ["--lock-timeout-seconds", "0.01"])
    finally:
        held.close()
    assert sentinel.read_text() == "keep"
