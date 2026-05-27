"""Regression checks for Stage 5 obsolete schema migration cleanup.

Stage 5 intentionally removes support for upgrading pre-Stage-1 and
pre-Stage-2 SQLite schemas.  These tests protect the current baseline instead:
current databases must still migrate idempotently, while the obsolete upgrade
helpers and table names must stay out of runtime code.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import text

from src.db import Database
from src.local_communities.service import LocalCommunityService
from support.db import build_database


def _create_current_local_community_activity(database: Database) -> None:
    """Seed one current-schema local community with host thread/message surfaces."""
    # LocalCommunityService creates the current LocalCommunity actor metadata so
    # the repository helpers can resolve the host forum when creating surfaces.
    LocalCommunityService(
        database=database,
        base_url="https://bridge.example",
        keypair_generator=lambda: ("public-key", "private-key"),
    ).create_local_community(
        discord_guild_id=10,
        discord_forum_channel_id=100,
        slug="hackers",
        name="Hackers",
        description="A local hackerspace forum.",
    )
    community = database.get_local_community_by_slug("hackers")
    assert community is not None
    thread = database.create_local_community_thread(
        local_community_id=community.id,
        discord_thread_id=200,
        discord_starter_message_id=300,
        ap_activity_id="https://bridge.example/users/alice/activities/create/post/1",
        ap_object_id="https://bridge.example/users/alice/post/1",
        direction="discord_to_ap",
        origin_kind="discord_local",
    )
    database.create_local_community_message(
        local_community_thread_id=thread.id,
        discord_message_id=301,
        ap_activity_id="https://bridge.example/users/alice/activities/create/comment/1",
        ap_object_id="https://bridge.example/users/alice/comment/1",
        parent_ap_object_id="https://bridge.example/users/alice/post/1",
        parent_discord_message_id=300,
        direction="discord_to_ap",
    )


def _columns(database: Database, table: str) -> set[str]:
    """Return SQLite column names for one test database table."""
    with database.engine.connect() as connection:
        return {
            str(row[1])
            for row in connection.execute(text(f"PRAGMA table_info({table})")).fetchall()
        }


def _count(database: Database, table: str) -> int:
    """Return a row count for one table in the test database."""
    with database.engine.connect() as connection:
        return int(connection.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one())


def test_stage5_current_schema_migrate_remains_idempotent(tmp_path: Path) -> None:
    """Current local-community surface schemas should still migrate safely.

    System state: a fresh current-schema database has one local community,
    canonical post/comment rows, and their host Discord surfaces. Action: run
    `Database.migrate()` twice. Observable result: row counts stay stable,
    current additive columns are present, and canonical rows do not expose the
    obsolete direct Discord-id columns that Stage 5 no longer upgrades from.
    """
    database = build_database(tmp_path, "stage5-current-schema.db")
    _create_current_local_community_activity(database)

    before_counts = {
        table: _count(database, table)
        for table in (
            "local_communities",
            "local_community_threads",
            "local_community_messages",
            "local_community_thread_surfaces",
            "local_community_message_surfaces",
        )
    }

    database.migrate()
    database.migrate()

    after_counts = {
        table: _count(database, table)
        for table in before_counts
    }
    assert after_counts == before_counts
    assert "local_subscriber_id" in _columns(database, "local_community_thread_surfaces")
    assert "local_subscriber_id" in _columns(database, "local_community_message_surfaces")
    assert "discord_thread_id" not in _columns(database, "local_community_threads")
    assert "discord_starter_message_id" not in _columns(database, "local_community_threads")
    assert "discord_message_id" not in _columns(database, "local_community_messages")
    assert "parent_discord_message_id" not in _columns(database, "local_community_messages")


def test_stage5_removes_obsolete_stage1_table_upgrade_source() -> None:
    """The runtime DB migrations should no longer reference the old table name."""
    # This static guard makes the policy decision explicit: Stage 5 no longer
    # supports translating the pre-Stage-1 table into the current schema.
    db_source = Path("src/db/migrations.py").read_text()
    assert "local_community_followers" not in db_source


def test_stage5_removes_obsolete_stage2_rebuild_helpers() -> None:
    """Pre-surface canonical rebuild helpers should not remain on Database."""
    # The current schema stores Discord ids on surface rows.  Keeping these
    # helpers would silently preserve support for pre-surface canonical rows.
    obsolete_helper_names = [
        "_backfill_stage2_thread_surfaces",
        "_backfill_stage2_message_surfaces",
        "_rebuild_stage2_local_community_threads",
        "_rebuild_stage2_local_community_messages",
    ]
    for obsolete_helper_name in obsolete_helper_names:
        assert not hasattr(Database, obsolete_helper_name)
