"""Behavior scenarios for secure immutable profile-media publication and serving."""

from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from src.db import Database
from src.http_api import create_http_app
from src.profile_media import ProfileMediaError, ProfileMediaStore
from src.user_profiles import UserProfileService


def _png(width: int = 8, height: int = 8) -> BytesIO:
    """Create one in-memory valid still image at the network boundary."""
    output = BytesIO()
    Image.new("RGBA", (width, height), (10, 20, 30, 128)).save(
        output, "PNG", pnginfo=None
    )
    output.seek(0)
    return output


def _system(tmp_path: Path):
    """Build the real database/store/service graph for one user."""
    database = Database(f"sqlite:///{tmp_path / 'bridge.db'}")
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
        tmp_path / "media", max_upload_bytes=1024 * 1024, max_pixels=1_000_000
    )
    store.initialize()
    return database, store, UserProfileService(database, store), user


def test_replace_media_publishes_canonical_bytes_and_switches_only_one_slot(
    tmp_path: Path,
) -> None:
    """Valid bytes become immutable canonical media and one atomic avatar reference."""
    database, store, service, user = _system(tmp_path)
    profile = service.replace_media(user.id, "avatar", _png())
    assert profile.avatar is not None and profile.header is None
    assert profile.avatar.content_type == "image/png"
    path = store.resolve_storage_key(profile.avatar.storage_key)
    assert path.read_bytes().startswith(b"\x89PNG")
    assert database.users.get_user_by_id(user.id).avatar_media_id == profile.avatar.id


def test_invalid_or_oversized_image_does_not_change_reference(tmp_path: Path) -> None:
    """Invalid source bytes fail before any public row or user reference exists."""
    database, _store, service, user = _system(tmp_path)
    with pytest.raises(ProfileMediaError):
        service.replace_media(user.id, "avatar", BytesIO(b"not-an-image"))
    assert service.get_profile(user.id).avatar is None
    with database.session() as session:
        from src.models import ProfileMediaAsset

        assert session.query(ProfileMediaAsset).count() == 0


def test_public_route_requires_persisted_opaque_id_and_sets_immutable_headers(
    tmp_path: Path,
) -> None:
    """HTTP delivery cannot enumerate or bypass database ownership of media bytes."""
    database, store, service, user = _system(tmp_path)
    asset = service.replace_media(user.id, "avatar", _png()).avatar
    runtime = SimpleNamespace(database=database, profile_media_store=store)
    client = TestClient(create_http_app(runtime))
    response = client.get(f"/media/profile/{asset.public_id}")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "immutable" in response.headers["cache-control"]
    assert client.get("/media/profile/../../bridge.db").status_code == 404
    assert client.get("/media/profile/not-valid").status_code == 404


def test_cleanup_removes_metadata_when_orphan_bytes_were_already_unlinked(
    tmp_path: Path,
) -> None:
    """Interrupted cleanup may retry after bytes disappeared and still remove the orphan row."""
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import select
    from src.models import ProfileMediaAsset

    database, store, service, user = _system(tmp_path)
    replacement = service.replace_media(user.id, "avatar", _png())
    asset = replacement.avatar
    service.clear_media(user.id, "avatar")
    # Zero-audience sources release media only after canonical rendering is
    # durably complete; before that, cleanup must retain immutable retry input.
    database.user_actor_deliveries.record_rendered(
        replacement.source_id, {"id": replacement.activity_id, "type": "Update"}
    )
    assert (
        database.user_actor_deliveries.release_terminal_source_media(
            replacement.source_id
        )
        == 1
    )
    with database.session() as session:
        row = session.scalar(
            select(ProfileMediaAsset).where(ProfileMediaAsset.id == asset.id)
        )
        row.created_at = datetime.now(timezone.utc) - timedelta(days=2)
    store.resolve_storage_key(asset.storage_key).unlink()
    assert (
        service.cleanup_orphans(
            grace_seconds=300,
            deployment_lock_path=tmp_path / "deployment.lock",
            media_lock_path=tmp_path / "media.lock",
        )
        == 1
    )
    with database.session() as session:
        assert session.get(ProfileMediaAsset, asset.id) is None


def test_settings_reject_media_paths_outside_data_or_inside_object_tree(
    monkeypatch,
) -> None:
    """Startup rejects non-durable and self-overlapping media/lock path layouts."""
    from pydantic import ValidationError
    from src.config import Settings

    base = {
        "DISCORD_TOKEN": "x",
        "PUBLIC_BASE_URL": "https://example.test",
        "FEDIFY_SHARED_SECRET": "x",
        "BRIDGE_SUPER_ADMIN_USER_IDS": "1",
    }
    with pytest.raises(ValidationError):
        Settings(
            **base,
            PROFILE_MEDIA_ROOT="/tmp/media",
            DEPLOYMENT_STATE_LOCK_PATH="/tmp/d.lock",
            PROFILE_MEDIA_LOCK_PATH="/tmp/m.lock",
        )
    with pytest.raises(ValidationError):
        Settings(
            **base,
            PROFILE_MEDIA_ROOT="/data/profile-media",
            DEPLOYMENT_STATE_LOCK_PATH="/data/profile-media/objects/d.lock",
            PROFILE_MEDIA_LOCK_PATH="/data/m.lock",
        )


def test_media_store_rejects_symlinked_storage_subdirectories(tmp_path: Path) -> None:
    """Pre-created volume symlinks cannot redirect staging or object writes."""
    root = tmp_path / "media"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "objects").symlink_to(outside, target_is_directory=True)
    store = ProfileMediaStore(root, max_upload_bytes=1024, max_pixels=1000)
    with pytest.raises(RuntimeError, match="symlink"):
        store.initialize()
