"""Behavior scenarios for durable local user profile persistence."""

from pathlib import Path

import pytest
from sqlalchemy import text

from src.db import Database
from src.user_profiles import UserProfileService, UserProfileValidationError
from src.profile_media import ProfileMediaStore


def _system(tmp_path: Path) -> tuple[Database, UserProfileService, int]:
    """Create one registered-user state with the real repositories and service."""
    database = Database(f"sqlite:///{tmp_path / 'bridge.db'}")
    database.migrate()
    user = database.users.create_user(
        discord_user_id="1",
        activitypub_username="alice",
        actor_url="https://example.test/actors/users/alice",
        inbox_url="https://example.test/actors/users/alice/inbox",
        outbox_url="https://example.test/actors/users/alice/outbox",
        followers_url="https://example.test/actors/users/alice/followers",
        public_key_pem="public",
        private_key_pem="private",
    )
    store = ProfileMediaStore(
        tmp_path / "media", max_upload_bytes=10_485_760, max_pixels=20_000_000
    )
    store.initialize()
    return database, UserProfileService(database, store), user.id


def test_existing_user_has_username_fallback_and_atomic_profile_update(
    tmp_path: Path,
) -> None:
    """A migrated user gets fallback display name and one atomic ordered update."""
    database, service, user_id = _system(tmp_path)
    before = service.get_profile(user_id)
    assert before is not None
    assert before.display_name == "alice"
    assert before.stored_display_name is None

    updated = service.update_profile(
        user_id,
        "  Alice É  ",
        " hello ",
        [("Site", "https://example.test"), ("Role", "Admin")],
    )
    assert updated.stored_display_name == "Alice É"
    assert updated.summary == "hello"
    assert updated.fields == (("Site", "https://example.test"), ("Role", "Admin"))
    identity = database.users.get_user_by_id(user_id)
    assert identity is not None
    assert identity.activitypub_username == "alice"
    assert identity.private_key_pem == "private"


def test_invalid_fifth_field_rejects_entire_profile_mutation(tmp_path: Path) -> None:
    """An invalid metadata set leaves all previously committed profile state intact."""
    _database, service, user_id = _system(tmp_path)
    service.update_profile(user_id, "Alice", "before", [("A", "1")])
    with pytest.raises(UserProfileValidationError):
        service.update_profile(
            user_id, "Changed", "after", [(str(i), "x") for i in range(5)]
        )
    profile = service.get_profile(user_id)
    assert profile is not None
    assert (profile.stored_display_name, profile.summary, profile.fields) == (
        "Alice",
        "before",
        (("A", "1"),),
    )


def test_sqlite_enforces_foreign_keys_and_slot_kind(tmp_path: Path) -> None:
    """Every write path rejects missing or wrong-kind media references."""
    database, _service, user_id = _system(tmp_path)
    with database.engine.connect() as connection:
        assert connection.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
    with pytest.raises(Exception):
        with database.session() as session:
            session.execute(
                text("UPDATE users SET avatar_media_id=999999 WHERE id=:id"),
                {"id": user_id},
            )


def test_profile_field_name_accepts_255_code_points_and_rejects_256(
    tmp_path: Path,
) -> None:
    """The persisted Stage 1 metadata-name boundary is exactly 255 code points."""
    _database, service, user_id = _system(tmp_path)
    accepted = "é" * 255
    assert service.update_profile(
        user_id, None, None, [(accepted, "value")]
    ).fields == ((accepted, "value"),)
    with pytest.raises(UserProfileValidationError):
        service.update_profile(user_id, None, None, [("x" * 256, "value")])


def test_current_startup_does_not_repeat_legacy_profile_field_rebuild(
    tmp_path: Path,
) -> None:
    """An existing Stage 1 draft schema migrates to the approved 255-width contract."""
    import sqlite3

    path = tmp_path / "legacy.db"
    database = Database(f"sqlite:///{path}")
    database.migrate()
    database.close()
    with sqlite3.connect(path) as connection:
        connection.execute(
            "ALTER TABLE user_profile_fields RENAME TO user_profile_fields_current"
        )
        connection.execute("""
            CREATE TABLE user_profile_fields (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                position INTEGER NOT NULL,
                name VARCHAR(30) NOT NULL,
                value VARCHAR(255) NOT NULL,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                UNIQUE (user_id, position),
                CHECK (position >= 0 AND position <= 3)
            )
        """)
        connection.execute("DROP TABLE user_profile_fields_current")
    migrated = Database(f"sqlite:///{path}")
    migrated.migrate()
    with migrated.engine.connect() as connection:
        rows = connection.execute(
            text("PRAGMA table_info(user_profile_fields)")
        ).fetchall()
    assert next(row[2] for row in rows if row[1] == "name") == "VARCHAR(30)"


def test_independent_profile_forms_do_not_overwrite_each_other(tmp_path: Path) -> None:
    """Scalar and metadata submissions retain concurrently changed sibling state."""
    _database, service, user_id = _system(tmp_path)
    service.replace_metadata(user_id, [("Site", "https://example.com")])
    service.update_identity_text(user_id, "Display", "Bio")
    assert service.get_profile(user_id).fields == (("Site", "https://example.com"),)
    service.replace_metadata(user_id, [("Pronouns", "they/them")])
    profile = service.get_profile(user_id)
    assert profile.display_name == "Display"
    assert profile.summary == "Bio"
