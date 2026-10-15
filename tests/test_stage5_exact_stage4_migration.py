"""Reject an unversioned Stage 4 file without altering its retained graph."""

from __future__ import annotations

import sqlite3
import pytest
from pathlib import Path

from sqlalchemy import text

from src.db import Database

_STAGE4_SCHEMA = Path(__file__).with_name("fixtures") / "stage4_schema.sql"


def _build_exact_stage4_database(path: Path) -> None:
    schema_sql = _STAGE4_SCHEMA.read_text(encoding="utf-8")
    connection = sqlite3.connect(path)
    try:
        connection.executescript(schema_sql)
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript("""
            INSERT INTO users (
                id, discord_user_id, activitypub_username, actor_url, inbox_url,
                outbox_url, followers_url, public_key_pem, private_key_pem,
                profile_version, created_at, updated_at
            ) VALUES (
                41, '41', 'alice', 'https://bridge.example/actors/users/alice',
                'https://bridge.example/actors/users/alice/inbox',
                'https://bridge.example/actors/users/alice/outbox',
                'https://bridge.example/actors/users/alice/followers',
                'public', 'private', 7, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            );

            INSERT INTO profile_media_assets (
                id, public_id, storage_key, media_kind, content_type, byte_size,
                width, height, sha256_hex, created_at
            ) VALUES (
                51, 'media-1', 'avatar/media-1.png', 'avatar', 'image/png', 10,
                1, 1,
                'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                CURRENT_TIMESTAMP
            );

            INSERT INTO published_activity_objects (
                id, actor_username, actor_url, community_actor_url, activity_id,
                object_id, kind, title, body_markdown, in_reply_to_object_id,
                discord_channel_id, discord_message_id, published_at, created_at
            ) VALUES (
                31, 'alice', 'https://bridge.example/actors/users/alice',
                'https://bridge.example/actors/communities/test',
                'https://bridge.example/activities/create/31',
                'https://bridge.example/objects/31', 'post', 'Original title',
                'Original body', NULL, 1001, 2001, CURRENT_TIMESTAMP,
                CURRENT_TIMESTAMP
            );

            INSERT INTO user_actor_outbound_sources (
                id, user_id, source_kind, logical_version, activity_id,
                profile_snapshot_json, activity_json, state,
                render_attempt_count, last_render_attempted_at, last_error,
                next_attempt_at, created_at, updated_at
            ) VALUES (
                61, 41, 'profile_update', 7,
                'https://bridge.example/activities/profile/7',
                '{"displayName":"Alice"}',
                '{"id":"https://bridge.example/activities/profile/7"}',
                'ready', 2, CURRENT_TIMESTAMP, 'old render error',
                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            );

            INSERT INTO user_actor_outbound_deliveries (
                id, source_id, target_endpoint_url, status, claim_token,
                claimed_at, attempt_count, last_attempted_at, delivered_at,
                last_error, next_attempt_at, created_at, updated_at
            ) VALUES (
                71, 61, 'https://remote.example/inbox', 'failed', NULL, NULL,
                3, CURRENT_TIMESTAMP, NULL, 'retry me', CURRENT_TIMESTAMP,
                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            );

            INSERT INTO user_actor_outbound_delivery_followers (
                id, delivery_id, relationship_id_snapshot, remote_actor_id,
                snapshotted_endpoint_url
            ) VALUES (
                81, 71, 91, 'https://remote.example/users/a',
                'https://remote.example/inbox'
            );

            INSERT INTO user_actor_outbound_source_media (
                id, source_id, media_asset_id, slot_kind
            ) VALUES (101, 61, 51, 'avatar');
            """)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        connection.commit()
    finally:
        connection.close()


def test_unversioned_stage4_delivery_graph_is_rejected_without_changes(
    tmp_path,
) -> None:
    """Refuse unknown Stage 4 adoption while preserving its complete delivery graph."""
    path = tmp_path / "stage4.db"
    _build_exact_stage4_database(path)

    before = sqlite3.connect(path)
    try:
        published_columns = {
            row[1]
            for row in before.execute("PRAGMA table_info(published_activity_objects)")
        }
        assert "desired_kind" not in published_columns
        assert "discord_reconciliation_needed" not in published_columns
        assert (
            before.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='user_actor_content_mutations'"
            ).fetchone()
            is None
        )
    finally:
        before.close()

    before = sqlite3.connect(path)
    schema_before = before.execute(
        "SELECT type,name,sql FROM sqlite_master WHERE name NOT GLOB 'sqlite_*' ORDER BY type,name"
    ).fetchall()
    rows_before = {
        r[1]: before.execute(f'SELECT * FROM "{r[1]}" ORDER BY rowid').fetchall()
        for r in schema_before
        if r[0] == "table"
    }
    before.close()
    upgraded = Database(f"sqlite:///{path}")
    with pytest.raises(RuntimeError, match="Unsupported"):
        upgraded.migrate()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
        assert (
            connection.execute(
                "SELECT type,name,sql FROM sqlite_master WHERE name NOT GLOB 'sqlite_*' ORDER BY type,name"
            ).fetchall()
            == schema_before
        )
        for name, rows in rows_before.items():
            assert (
                connection.execute(f'SELECT * FROM "{name}" ORDER BY rowid').fetchall()
                == rows
            )
    upgraded.close()
