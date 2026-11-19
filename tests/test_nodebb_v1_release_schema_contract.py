"""CP24 release-floor checks for the immutable SQLite v3 schema.

These scenarios compare the *actual* versioned migration output with the
independent historical SQL file. The SQL snapshot is executed only on a
throwaway test database, never on a production migration connection.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

from src.db import Database


CURRENT_SQL = Path(__file__).parents[1] / "src" / "db" / "schema_versions" / "v3.sql"


def _manifest(db: Path) -> dict[tuple[str, str], str]:
    """Compare the same SQLite physical DDL independent of formatting details."""
    with sqlite3.connect(db) as conn:
        return {
            (kind, name): re.sub(r"\s+", "", ddl.replace('"', "").lower())
            for kind, name, ddl in conn.execute(
                "SELECT type,name,sql FROM sqlite_master "
                "WHERE type IN ('table','index','trigger') "
                "AND name NOT LIKE 'sqlite_%' AND sql IS NOT NULL"
            )
        }


@pytest.mark.parametrize("source_schema", ["empty", "released_v0", "known_main_v0"])
def test_versioned_database_matches_frozen_v2_schema(
    tmp_path: Path, source_schema: str
):
    """All supported source profiles upgrade to the same immutable target DDL."""
    migrated = tmp_path / "migrated.sqlite"
    snapshot = tmp_path / "frozen.sqlite"
    if source_schema != "empty":
        fixture = (
            Path(__file__).parent / "fixtures" / "database"
            / f"{source_schema}_schema.sql"
        )
        with sqlite3.connect(migrated) as conn:
            conn.executescript(fixture.read_text())
    database = Database(f"sqlite:///{migrated}")
    database.migrate()
    database.close()

    # This is a separate disposable test-only SQLite connection. `executescript`
    # must not be used in the production transaction-controlled upgrade path.
    with sqlite3.connect(snapshot) as conn:
        conn.executescript(CURRENT_SQL.read_text())
    actual = _manifest(migrated)
    frozen = _manifest(snapshot)
    assert actual == frozen
    assert len(actual) >= 80
    assert ("index", "uq_local_relay_create") in actual
    assert "canonical_relay_projection_json" in actual[("table", "local_community_relay_source_activities")]
    with sqlite3.connect(migrated) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_v3_restart_is_read_only_for_saved_relay_and_follow_rows(tmp_path: Path):
    """A second v2 admission never rewrites accepted state or source content."""
    db_file = tmp_path / "restart.sqlite"
    database = Database(f"sqlite:///{db_file}")
    database.migrate()
    database.close()
    with sqlite3.connect(db_file) as conn:
        conn.execute(
            "INSERT INTO local_community_relay_source_activities "
            "(id,local_community_id,object_kind,operation,source_object_ap_id,"
            "source_activity_id,source_announce_id,origin_remote_actor_id,"
            "source_activity_json,canonical_relay_projection_json,created_at) "
            "VALUES (1,42,'post','create','https://remote.example/object/1',"
            "'https://remote.example/create/1',NULL,'https://remote.example/actors/services/bridge',"
            "'{}','{\"body_markdown\":\"original\"}','2026-11-12 00:00:00')"
        )
        before = conn.execute("SELECT * FROM local_community_relay_source_activities").fetchall()
        schema_before = conn.execute("SELECT type,name,sql FROM sqlite_master ORDER BY name").fetchall()
    for _ in range(3):
        database = Database(f"sqlite:///{db_file}")
        database.migrate()
        database.close()
    with sqlite3.connect(db_file) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
        assert conn.execute("SELECT * FROM local_community_relay_source_activities").fetchall() == before
        assert conn.execute("SELECT type,name,sql FROM sqlite_master ORDER BY name").fetchall() == schema_before


def test_unknown_drift_in_stamped_v3_is_rejected_without_repair(tmp_path: Path):
    """A missing canonical UNIQUE index cannot be silently re-created on startup."""
    db_file = tmp_path / "corrupt.sqlite"
    database = Database(f"sqlite:///{db_file}")
    database.migrate()
    database.close()
    with sqlite3.connect(db_file) as conn:
        conn.execute("DROP INDEX uq_local_relay_create")
        before = _manifest(db_file)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
    database = Database(f"sqlite:///{db_file}")
    with pytest.raises((RuntimeError, ValueError)):
        database.migrate()
    database.close()
    assert _manifest(db_file) == before
    with sqlite3.connect(db_file) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
