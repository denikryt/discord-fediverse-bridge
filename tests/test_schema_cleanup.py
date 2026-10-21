"""Regression checks for schema cleanup obsolete schema migration cleanup.

schema cleanup intentionally removes support for upgrading before current SQLite schema and
before legacy surface migration SQLite schemas.  These tests protect the current baseline instead:
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
        created_by_discord_user_id="123",
    )
    community = database.local_communities.get_local_community_by_slug("hackers")
    assert community is not None
    thread = database.local_community_content.create_local_community_thread(
        local_community_id=community.id,
        discord_thread_id=200,
        discord_starter_message_id=300,
        ap_activity_id="https://bridge.example/actors/users/alice/activities/create/posts/1",
        ap_object_id="https://bridge.example/actors/users/alice/objects/posts/1",
        direction="discord_to_ap",
        origin_kind="discord_local",
    )
    database.local_community_content.create_local_community_message(
        local_community_thread_id=thread.id,
        discord_message_id=301,
        ap_activity_id="https://bridge.example/actors/users/alice/activities/create/comments/1",
        ap_object_id="https://bridge.example/actors/users/alice/objects/comments/1",
        parent_ap_object_id="https://bridge.example/actors/users/alice/objects/posts/1",
        parent_discord_message_id=300,
        direction="discord_to_ap",
    )


def _columns(database: Database, table: str) -> set[str]:
    """Return SQLite column names for one test database table."""
    with database.engine.connect() as connection:
        return {
            str(row[1])
            for row in connection.execute(
                text(f"PRAGMA table_info({table})")
            ).fetchall()
        }


def _count(database: Database, table: str) -> int:
    """Return a row count for one table in the test database."""
    with database.engine.connect() as connection:
        return int(
            connection.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
        )


def test_current_schema_migrate_remains_idempotent(tmp_path: Path) -> None:
    """Current local-community surface schemas should still migrate safely.

    System state: a fresh current-schema database has one local community,
    canonical post/comment rows, and their host Discord surfaces. Action: run
    `Database.migrate()` twice. Observable result: row counts stay stable,
    current additive columns are present, and canonical rows do not expose the
    obsolete direct Discord-id columns that schema cleanup no longer upgrades from.
    """
    database = build_database(tmp_path, "schema cleanup-current-schema.db")
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

    after_counts = {table: _count(database, table) for table in before_counts}
    assert after_counts == before_counts
    assert "local_subscriber_id" in _columns(
        database, "local_community_thread_surfaces"
    )
    assert "local_subscriber_id" in _columns(
        database, "local_community_message_surfaces"
    )
    assert "discord_thread_id" not in _columns(database, "local_community_threads")
    assert "discord_starter_message_id" not in _columns(
        database, "local_community_threads"
    )
    assert "discord_message_id" not in _columns(database, "local_community_messages")
    assert "parent_discord_message_id" not in _columns(
        database, "local_community_messages"
    )


def test_legacy_follower_table_upgrade_source_is_absent() -> None:
    """The runtime DB migrations should no longer reference the old table name."""
    # This static guard makes the policy decision explicit: schema cleanup no longer
    # supports translating the before current SQLite schema table into the current schema.
    db_source = Path("src/db/migrations.py").read_text()
    assert "local_community_followers" not in db_source


def test_obsolete_canonical_rebuild_helpers_are_absent() -> None:
    """Pre-surface canonical rebuild helpers should not remain on Database."""
    # The current schema stores Discord ids on surface rows.  Keeping these
    # helpers would silently preserve support for pre-surface canonical rows.
    # Match obsolete helper responsibilities, regardless of historical names.
    forbidden = (
        ("_backfill_", "_thread_surfaces"),
        ("_backfill_", "_message_surfaces"),
        ("_rebuild_", "_local_community_threads"),
        ("_rebuild_", "_local_community_messages"),
    )
    for name in dir(Database):
        assert not any(name.startswith(prefix) and name.endswith(suffix)
                       for prefix, suffix in forbidden)
