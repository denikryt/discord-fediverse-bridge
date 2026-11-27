"""The only release schema is updated SQLite v2, with no v2→v3 transition.

Scenarios exercise genuine v1 rows and the application migration runner; this
must remain independent of any old development v2 database contents.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import select

from src.db import Database
from src.db.migrations import DATABASE_VERSION, MIGRATIONS, read_database_state
from src.models import CommunityDiscordTargetClaim, CommunityThreadGroup, CommunityMessageGroup

SQL_DIR = Path(__file__).parents[1] / "src" / "db" / "schema_versions"


def _create_v1_with_inbound_delivery(path: Path) -> None:
    """Create verified inbound proof, a completed claim, and an uncertain claim."""
    note = {
        "ap_id": "https://origin.example/n/42", "kind": "post",
        "author_name": "alice", "body_markdown": "original",
    }
    with sqlite3.connect(path) as conn:
        conn.executescript((SQL_DIR / "v1.sql").read_text(encoding="utf-8"))
        conn.execute("PRAGMA user_version=1")
        conn.execute(
            "INSERT INTO community_thread_groups "
            "(id, community_actor_id, ap_object_id, source_owner_actor_id, "
            "inbound_create_snapshot_json, created_at) VALUES (1, ?, ?, ?, ?, ?)",
            ("https://group.example/c/2", note["ap_id"],
             "https://origin.example/users/alice", json.dumps(note), "2026-11-01"),
        )
        conn.execute(
            "INSERT INTO community_thread_group_deliveries "
            "(thread_group_id,discord_channel_id,discord_thread_id,discord_starter_message_id,role) "
            "VALUES (1,10,100,100,'inbound')"
        )
        conn.execute(
            "INSERT INTO community_discord_target_claims "
            "(thread_group_id,discord_channel_id,kind,status,attempt_id) "
            "VALUES (1,10,'post','completed','ack-10')"
        )
        conn.execute(
            "INSERT INTO community_discord_target_claims "
            "(thread_group_id,discord_channel_id,kind,status,attempt_id) "
            "VALUES (1,11,'post','unknown','lost-11')"
        )
        conn.execute(
            "INSERT INTO community_discord_target_claims "
            "(thread_group_id,discord_channel_id,kind,status,attempt_id) "
            "VALUES (1,14,'post','completed','no-proof-14')"
        )
        reply = {**note, "ap_id": "https://origin.example/n/43", "kind": "comment"}
        conn.execute(
            "INSERT INTO community_message_groups "
            "(id,community_actor_id,thread_group_id,ap_object_id,source_owner_actor_id,"
            "inbound_create_snapshot_json,created_at) VALUES(2,?,?,?,?,?,?)",
            ("https://group.example/c/2", 1, reply["ap_id"],
             "https://origin.example/users/alice", json.dumps(reply), "2026-11-01"),
        )
        conn.execute(
            "INSERT INTO community_message_group_deliveries "
            "(message_group_id,discord_channel_id,discord_thread_id,discord_message_id,role) "
            "VALUES(2,10,100,101,'inbound')"
        )
        conn.execute(
            "INSERT INTO community_discord_target_claims "
            "(message_group_id,discord_channel_id,kind,status,attempt_id) "
            "VALUES(2,10,'comment','completed','ack-comment')"
        )


def test_final_version_is_v2_with_only_two_steps(tmp_path: Path) -> None:
    """Fresh DB admits the current Group-view columns without a v3 snapshot."""
    assert DATABASE_VERSION == 2
    assert set(MIGRATIONS) == {1, 2}
    assert not (SQL_DIR / "v3.sql").exists()
    db = Database(f"sqlite:///{tmp_path / 'new.db'}")
    try:
        db.migrate()
        assert read_database_state(db.engine).profile == "ready_v2"
        with db.engine.connect() as conn:
            assert conn.exec_driver_sql("PRAGMA user_version").scalar_one() == 2
            columns = {row[1] for row in conn.exec_driver_sql(
                "PRAGMA table_info('community_thread_groups')"
            )}
            assert {"display_object_json", "view_revision", "view_state"} <= columns
    finally:
        db.close()


def test_v1_to_updated_v2_preserves_claim_identity_and_display(tmp_path: Path) -> None:
    """Admitted v1 data gains proof-backed current view and cursor in one step."""
    path = tmp_path / "v1.db"
    _create_v1_with_inbound_delivery(path)
    db = Database(f"sqlite:///{path}")
    try:
        assert read_database_state(db.engine).profile == "ready_v1"
        db.migrate()
        assert read_database_state(db.engine).profile == "ready_v2"
        with db.session() as session:
            group = session.get(CommunityThreadGroup, 1)
            assert group is not None
            assert group.view_state == "live" and group.view_revision == 0
            assert group.display_object_json == group.inbound_create_snapshot_json
            child = session.get(CommunityMessageGroup, 2)
            assert child is not None and child.display_object_json == child.inbound_create_snapshot_json
            claims = {row.discord_channel_id: row for row in session.scalars(
                select(CommunityDiscordTargetClaim).where(CommunityDiscordTargetClaim.kind == "post")
            )}
            assert set(claims) == {10, 11, 14}
            # A saved Discord ID proves completion even when a pre-v2 claim exists.
            assert claims[10].status == "completed" and claims[10].applied_revision == 0
            assert claims[10].attempt_id == "ack-10"
            assert claims[11].status == "unknown" and claims[11].applied_revision is None
            assert claims[11].attempt_id == "lost-11"
            assert claims[14].applied_revision is None  # No proven Discord ID.
            [comment_claim] = session.scalars(select(CommunityDiscordTargetClaim).where(
                CommunityDiscordTargetClaim.kind == "comment"
            )).all()
            assert comment_claim.applied_revision == 0
            assert comment_claim.attempt_id == "ack-comment"
        with db.engine.connect() as conn:
            assert conn.exec_driver_sql("PRAGMA foreign_key_check").all() == []
            assert conn.exec_driver_sql("PRAGMA integrity_check").scalar_one() == "ok"
        before = path.read_bytes()
        db.migrate()
        assert path.read_bytes() == before
    finally:
        db.close()


def test_old_physical_v2_is_rejected_without_hidden_upgrade(tmp_path: Path) -> None:
    """A user_version=2 stamp does not excuse missing Group-view columns."""
    path = tmp_path / "legacy-v2.db"
    with sqlite3.connect(path) as conn:
        conn.executescript((SQL_DIR / "v1.sql").read_text(encoding="utf-8"))
        conn.execute("PRAGMA user_version=2")
    original = path.read_bytes()
    db = Database(f"sqlite:///{path}")
    try:
        with pytest.raises(RuntimeError, match="v2 physical schema differs"):
            db.migrate()
        assert path.read_bytes() == original
    finally:
        db.close()


def test_v3_header_is_not_admitted(tmp_path: Path) -> None:
    """A v3 file is not relabeled v2 or silently modified during startup."""
    path = tmp_path / "future.db"
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA user_version=3")
    original = path.read_bytes()
    db = Database(f"sqlite:///{path}")
    try:
        with pytest.raises(RuntimeError, match="Unsupported SQLite user_version=3"):
            db.migrate()
        assert path.read_bytes() == original
    finally:
        db.close()
