"""Additive and compatibility schema migration helpers for bridge SQLite DBs.

Database owns the engine and calls this module from `Database.migrate()`. The
helpers preserve the previous additive migration behavior and include one narrow
SQLite table-rebuild migration for the local-community summary nullability
change, because SQLite cannot drop a NOT NULL constraint in place.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from ..models import Base, CommunityActorBan, LocalCommunityMessageSurface, LocalCommunityThreadSurface, ManagementAuditEvent


def migrate(engine: Engine) -> None:
    """Apply schema migrations that `create_all()` cannot handle.

    `create_all()` creates missing tables but does not alter existing columns.
    Most migrations are additive column/table changes. The local-community
    summary migration is intentionally handled separately because changing
    nullable constraints in SQLite requires rebuilding the table.
    """
    migrations = [
        (
            "channel_community_subscriptions",
            "discord_guild_id",
            "ALTER TABLE channel_community_subscriptions ADD COLUMN discord_guild_id INTEGER",
        ),
        (
            "local_communities",
            "created_by_discord_user_id",
            "ALTER TABLE local_communities ADD COLUMN created_by_discord_user_id VARCHAR(64)",
        ),
    ]
    # Stage 2 adds explicit local-community surface tables. Creating them here
    # keeps interrupted deployments and migrate-only test fixtures aligned with
    # the schema that the runtime expects after the refactor.
    Base.metadata.create_all(
        engine,
        tables=[
            CommunityActorBan.__table__,
            LocalCommunityThreadSurface.__table__,
            LocalCommunityMessageSurface.__table__,
            ManagementAuditEvent.__table__,
        ],
    )
    with engine.connect() as conn:
        for table, column, stmt in migrations:
            # PRAGMA table_info returns one row per column; skip if already present.
            existing = _table_columns(conn, table)
            if column not in existing:
                conn.execute(text(stmt))
        _migrate_local_communities_summary_nullable(conn)
        # Stage 3 stores the selected LocalSubscriber on subscriber surface rows.
        # Existing host-only surface rows keep NULL, so this additive migration is
        # safe to run before or after Stage 2 backfill.
        for table in (
            "local_community_thread_surfaces",
            "local_community_message_surfaces",
        ):
            columns = _table_columns(conn, table)
            if "local_subscriber_id" not in columns:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN local_subscriber_id INTEGER"))
        _verify_stage2_surface_invariants(conn)
        conn.commit()


def _table_columns(conn: Connection, table: str) -> set[str]:
    """Return the current SQLite column names for one table."""
    rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    return {str(row[1]) for row in rows}


def _table_info(conn: Connection, table: str) -> list[object]:
    """Return raw SQLite table-info rows for one table."""
    return list(conn.execute(text(f"PRAGMA table_info({table})")).fetchall())


def _migrate_local_communities_summary_nullable(conn: Connection) -> None:
    """Rebuild `local_communities` when legacy schema has `summary NOT NULL`.

    SQLite cannot drop a NOT NULL constraint with `ALTER TABLE`. The rebuild is
    intentionally limited to this table and recreates the same identity
    constraints the SQLAlchemy model declares. Existing summary values are
    copied unchanged, while future rows may store NULL.
    """
    table_info = _table_info(conn, "local_communities")
    if not table_info:
        return
    summary_row = next((row for row in table_info if row[1] == "summary"), None)
    if summary_row is None or int(summary_row[3]) == 0:
        return

    existing_columns = {str(row[1]) for row in table_info}
    conn.execute(text("PRAGMA foreign_keys=OFF"))
    conn.execute(text("ALTER TABLE local_communities RENAME TO local_communities_legacy_summary_not_null"))
    conn.execute(
        text(
            """
            CREATE TABLE local_communities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_guild_id INTEGER NOT NULL,
                discord_forum_channel_id INTEGER NOT NULL,
                slug VARCHAR(255) NOT NULL,
                display_name VARCHAR(255) NOT NULL,
                summary VARCHAR,
                created_by_discord_user_id VARCHAR(64),
                actor_url VARCHAR(512) NOT NULL,
                inbox_url VARCHAR(512) NOT NULL,
                outbox_url VARCHAR(512) NOT NULL,
                followers_url VARCHAR(512) NOT NULL,
                public_key_pem VARCHAR NOT NULL,
                private_key_pem VARCHAR NOT NULL,
                status VARCHAR(32) NOT NULL,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                UNIQUE (discord_forum_channel_id),
                UNIQUE (slug),
                UNIQUE (actor_url)
            )
            """
        )
    )

    created_by_expr = "created_by_discord_user_id" if "created_by_discord_user_id" in existing_columns else "NULL"
    # Keep the column list explicit so future table changes do not get silently
    # dropped into this compatibility path without a deliberate migration plan.
    conn.execute(
        text(
            f"""
            INSERT INTO local_communities (
                id,
                discord_guild_id,
                discord_forum_channel_id,
                slug,
                display_name,
                summary,
                created_by_discord_user_id,
                actor_url,
                inbox_url,
                outbox_url,
                followers_url,
                public_key_pem,
                private_key_pem,
                status,
                created_at,
                updated_at
            )
            SELECT
                id,
                discord_guild_id,
                discord_forum_channel_id,
                slug,
                display_name,
                summary,
                {created_by_expr},
                actor_url,
                inbox_url,
                outbox_url,
                followers_url,
                public_key_pem,
                private_key_pem,
                status,
                created_at,
                updated_at
            FROM local_communities_legacy_summary_not_null
            """
        )
    )
    conn.execute(text("DROP TABLE local_communities_legacy_summary_not_null"))
    conn.execute(text("PRAGMA foreign_keys=ON"))


def _verify_stage2_surface_invariants(conn: Connection) -> None:
    """Fail loudly when Stage 2 host-surface ownership is ambiguous."""
    thread_violations = conn.execute(
        text(
            """
            SELECT thread.id
            FROM local_community_threads AS thread
            LEFT JOIN local_community_thread_surfaces AS surface
              ON surface.local_community_thread_id = thread.id
             AND surface.role = 'host'
            GROUP BY thread.id
            HAVING COUNT(surface.id) != 1
            """
        )
    ).fetchall()
    if thread_violations:
        raise RuntimeError(
            "Stage 2 migration requires exactly one host thread surface per canonical thread"
        )

    message_violations = conn.execute(
        text(
            """
            SELECT message.id
            FROM local_community_messages AS message
            LEFT JOIN local_community_message_surfaces AS surface
              ON surface.local_community_message_id = message.id
             AND surface.role = 'host'
            GROUP BY message.id
            HAVING COUNT(surface.id) != 1
            """
        )
    ).fetchall()
    if message_violations:
        raise RuntimeError(
            "Stage 2 migration requires exactly one host message surface per canonical message"
        )
