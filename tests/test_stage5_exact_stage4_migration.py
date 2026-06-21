"""Exact Stage 4 database upgrade preservation for Stage 5."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

from sqlalchemy import text

from src.db import Database
from src.db.migrations import Stage4IdentityMigrationConfig

_STAGE4_SCHEMA = Path(__file__).with_name("fixtures") / "stage4_schema.sql"
_AFFECTED_TABLES = (
    "published_activity_objects",
    "user_actor_outbound_sources",
    "user_actor_outbound_deliveries",
    "user_actor_outbound_delivery_followers",
    "user_actor_outbound_source_media",
    "user_actor_content_mutations",
)


def _normalize_sql(value: str | None) -> str | None:
    if value is None:
        return None
    return re.sub(r"\s+", " ", value.strip())


def _schema_signature(connection: Any, table: str) -> dict[str, Any]:
    columns = [
        tuple(row) for row in connection.execute(text(f'PRAGMA table_info("{table}")'))
    ]
    foreign_keys = [
        tuple(row)
        for row in connection.execute(text(f'PRAGMA foreign_key_list("{table}")'))
    ]
    indexes: list[tuple[Any, ...]] = []
    for row in connection.execute(text(f'PRAGMA index_list("{table}")')):
        # PRAGMA index_list.seq is an internal enumeration value whose ordering
        # may differ between equivalent fresh and migrated schemas. Compare only
        # the stable semantic fields: name, uniqueness, origin, partial flag, and
        # the ordered indexed columns.
        index_row = tuple(row)
        index_name = index_row[1]
        index_columns = tuple(
            info[2]
            for info in connection.execute(text(f'PRAGMA index_info("{index_name}")'))
        )
        indexes.append(
            (
                index_name,
                index_row[2],  # unique
                index_row[3],  # origin
                index_row[4],  # partial
                index_columns,
            )
        )
    table_sql = connection.execute(
        text("SELECT sql FROM sqlite_master WHERE type='table' AND name=:name"),
        {"name": table},
    ).scalar_one_or_none()
    return {
        "columns": columns,
        "foreign_keys": sorted(foreign_keys),
        "indexes": sorted(indexes, key=lambda item: str(item[0])),
        "table_sql": _normalize_sql(table_sql),
    }


def _build_exact_stage4_database(path: Path) -> None:
    schema_sql = _STAGE4_SCHEMA.read_text(encoding="utf-8")
    connection = sqlite3.connect(path)
    try:
        connection.executescript(schema_sql)
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript(
            """
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
            """
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        connection.commit()
    finally:
        connection.close()


def test_exact_stage4_delivery_graph_survives_complete_stage5_upgrade(tmp_path) -> None:
    """Upgrade the real Stage 4 schema and preserve the complete delivery graph."""
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

    upgraded = Database(f"sqlite:///{path}")
    upgraded.migrate(
        Stage4IdentityMigrationConfig(
            public_origin="https://bridge.example", service_actor_identifier="bridge"
        )
    )
    with upgraded.engine.connect() as connection:
        source = (
            connection.execute(
                text("SELECT * FROM user_actor_outbound_sources WHERE id=61")
            )
            .mappings()
            .one()
        )
        delivery = (
            connection.execute(
                text("SELECT * FROM user_actor_outbound_deliveries WHERE id=71")
            )
            .mappings()
            .one()
        )
        evidence = (
            connection.execute(
                text("SELECT * FROM user_actor_outbound_delivery_followers WHERE id=81")
            )
            .mappings()
            .one()
        )
        hold = (
            connection.execute(
                text("SELECT * FROM user_actor_outbound_source_media WHERE id=101")
            )
            .mappings()
            .one()
        )
        published = (
            connection.execute(
                text("SELECT * FROM published_activity_objects WHERE id=31")
            )
            .mappings()
            .one()
        )

        assert source["id"] == 61
        assert source["ordering_key"] == "profile:41"
        assert source["logical_version"] == 7
        assert source["snapshot_json"] == '{"displayName":"Alice"}'
        assert source["activity_json"] == (
            '{"id":"https://bridge.example/activities/profile/7"}'
        )
        assert delivery["id"] == 71
        assert delivery["status"] == "failed"
        assert delivery["attempt_count"] == 3
        assert delivery["last_error"] == "retry me"
        assert delivery["next_attempt_at"] is not None
        assert evidence["id"] == 81
        assert evidence["relationship_id_snapshot"] == 91
        assert evidence["snapshotted_endpoint_url"] == "https://remote.example/inbox"
        assert hold["id"] == 101
        assert hold["media_asset_id"] == 51
        assert published["content_revision"] == 1
        assert published["deleted_at"] is None
        assert published["desired_kind"] == "none"
        assert published["discord_reconciliation_needed"] in (False, 0)
        assert published["discord_reconciliation_attempt_count"] == 0
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
        upgraded_signatures = {
            table: _schema_signature(connection, table) for table in _AFFECTED_TABLES
        }

    fresh_path = tmp_path / "fresh.db"
    fresh = Database(f"sqlite:///{fresh_path}")
    fresh.create_all()
    fresh.migrate()
    with fresh.engine.connect() as connection:
        fresh_signatures = {
            table: _schema_signature(connection, table) for table in _AFFECTED_TABLES
        }

    assert upgraded_signatures == fresh_signatures
