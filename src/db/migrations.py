"""Additive schema migration helpers for the bridge database.

Database owns the engine and calls this module from `Database.migrate()`. The
helpers here preserve the previous SQLite additive migration behavior and do
not create independent engines or session factories.
"""

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from ..models import Base, LocalCommunityMessageSurface, LocalCommunityThreadSurface


def migrate(engine: Engine) -> None:
    """Apply additive schema migrations that create_all cannot handle.

    create_all only creates missing tables — it never alters existing ones.
    Each entry is a (table, column, ALTER statement) triple. The column is
    checked via PRAGMA table_info before executing, making each migration
    idempotent. SQLite does not support ALTER TABLE ... ADD COLUMN IF NOT EXISTS.
    """
    # discord_guild_id was added to channel_community_subscriptions after
    # initial deployment; existing databases need the column added.
    migrations = [
        (
            "channel_community_subscriptions",
            "discord_guild_id",
            "ALTER TABLE channel_community_subscriptions ADD COLUMN discord_guild_id INTEGER",
        ),
    ]
    # Stage 2 adds explicit local-community surface tables. Creating them here
    # keeps interrupted deployments and migrate-only test fixtures aligned with
    # the schema that the runtime expects after the refactor.
    Base.metadata.create_all(
        engine,
        tables=[
            LocalCommunityThreadSurface.__table__,
            LocalCommunityMessageSurface.__table__,
        ],
    )
    with engine.connect() as conn:
        for table, column, stmt in migrations:
            # PRAGMA table_info returns one row per column; skip if already present.
            existing = _table_columns(conn, table)
            if column not in existing:
                conn.execute(text(stmt))
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
