"""Pinned regression reproducers for migration and backup failures in main.

Each expected failure is removed by its owning checkpoint, not made to pass by
loosening the behavior assertion. These use the actual DB/backup components.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import text

from src.db import Database
from src.db.backup import create_complete_backup, restore_complete_backup
from src.db.migrations import Stage4IdentityMigrationConfig


@pytest.mark.xfail(strict=True, reason="CP03: repeated identity cutover erases unaffected Follow")
def test_repeat_migrate_retains_unchanged_remote_follow_and_channel(tmp_path: Path) -> None:
    """A v1 restart must not delete an accepted remote Group relationship."""
    database = Database(f"sqlite:///{tmp_path / 'main.db'}")
    config = Stage4IdentityMigrationConfig(
        public_origin="https://bridge.example", service_actor_identifier="bridge"
    )
    database.create_all()
    database.migrate(config)
    # Both remote IDs are unrelated to any local actor URL conversion.
    with database.engine.begin() as connection:
        connection.execute(text("""
            INSERT INTO bridge_actor_follows
                (community_actor_id, follow_activity_id, community_inbox_url, status, created_at, updated_at)
            VALUES ('https://lemmy.example/c/python', 'https://bridge.example/follows/123',
                    'https://lemmy.example/inbox', 'accepted', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """))
        connection.execute(text("""
            INSERT INTO channel_community_subscriptions
                (discord_channel_id, lemmy_community_actor_id, follow_activity_id,
                 status, created_at, updated_at)
            VALUES (1234, 'https://lemmy.example/c/python',
                    'https://bridge.example/follows/123', 'accepted', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """))
    try:
        database.migrate(config)
        with database.engine.connect() as connection:
            assert connection.execute(text(
                "SELECT COUNT(*) FROM bridge_actor_follows WHERE status='accepted'"
            )).scalar_one() == 1
            assert connection.execute(text(
                "SELECT status, follow_activity_id FROM channel_community_subscriptions "
                "WHERE discord_channel_id=1234"
            )).one() == ("accepted", "https://bridge.example/follows/123")
    finally:
        database.close()


def test_release_v0_complete_backup_can_be_restored_without_media_table(tmp_path: Path) -> None:
    """The tagged release DB must be a valid zero-media migration backup source."""
    schema = Path(__file__).parents[1] / "fixtures/database/released_v0_schema.sql"
    database_path = tmp_path / "released.db"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(schema.read_text(encoding="utf-8"))
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name='profile_media_assets'"
        ).fetchone() is None

    original = sqlite3.connect(database_path).execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchone()[0]
    assert original == 30
    archive = create_complete_backup(
        database_path, tmp_path / "media", tmp_path / "backups",
        deployment_lock_path=tmp_path / "deploy.lock",
        media_lock_path=tmp_path / "media.lock",
        now=datetime(2026, 11, 11, tzinfo=timezone.utc),
    )
    restore_complete_backup(
        tmp_path / "restored.db", tmp_path / "restored-media", archive,
        deployment_lock_path=tmp_path / "restore.lock",
    )
    with sqlite3.connect(tmp_path / "restored.db") as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchone()[0] == original
