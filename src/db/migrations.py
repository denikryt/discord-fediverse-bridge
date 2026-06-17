"""Additive and compatibility schema migration helpers for bridge SQLite DBs.

Database owns the engine and calls this module from `Database.migrate()`. The
helpers preserve the previous additive migration behavior and include one narrow
SQLite table-rebuild migration for the local-community summary nullability
change, because SQLite cannot drop a NOT NULL constraint in place.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from ..models import (
    Base,
    BridgeActorKey,
    BridgePolicyEntry,
    CommunityActorBan,
    GuildInvitePublication,
    LocalCommunityMessageSurface,
    LocalCommunityThreadSurface,
    ManagementAuditEvent,
    ProfileMediaAsset,
    UserProfileField,
    UserActorFollower,
    UserActorFollowActivityId,
    UserActorOutboundSource,
    UserActorOutboundDelivery,
    UserActorOutboundDeliveryFollower,
    UserActorOutboundSourceMedia,
    UserActorContentMutation,
    PublishedActivityObject,
    PersonalFeedBinding,
    MessageMapping,
    PersonalFeedThreadMapping,
    InboundConversationObject,
    InboundDiscordDelivery,
)


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
        (
            "activitypub_event_receipts",
            "outcome",
            "ALTER TABLE activitypub_event_receipts ADD COLUMN outcome VARCHAR(64)",
        ),
        (
            "users",
            "display_name",
            "ALTER TABLE users ADD COLUMN display_name VARCHAR(30)",
        ),
        ("users", "summary", "ALTER TABLE users ADD COLUMN summary VARCHAR(500)"),
        (
            "users",
            "avatar_media_id",
            "ALTER TABLE users ADD COLUMN avatar_media_id INTEGER REFERENCES profile_media_assets(id) ON DELETE RESTRICT",
        ),
        (
            "users",
            "header_media_id",
            "ALTER TABLE users ADD COLUMN header_media_id INTEGER REFERENCES profile_media_assets(id) ON DELETE RESTRICT",
        ),
        (
            "users",
            "profile_version",
            "ALTER TABLE users ADD COLUMN profile_version INTEGER NOT NULL DEFAULT 0",
        ),
        (
            "published_activity_objects",
            "content_revision",
            "ALTER TABLE published_activity_objects ADD COLUMN content_revision INTEGER NOT NULL DEFAULT 1",
        ),
        (
            "published_activity_objects",
            "deleted_at",
            "ALTER TABLE published_activity_objects ADD COLUMN deleted_at DATETIME",
        ),
        (
            "published_activity_objects",
            "desired_kind",
            "ALTER TABLE published_activity_objects ADD COLUMN desired_kind VARCHAR(16) NOT NULL DEFAULT 'none'",
        ),
        (
            "published_activity_objects",
            "desired_title",
            "ALTER TABLE published_activity_objects ADD COLUMN desired_title VARCHAR(512)",
        ),
        (
            "published_activity_objects",
            "desired_body_markdown",
            "ALTER TABLE published_activity_objects ADD COLUMN desired_body_markdown TEXT",
        ),
        (
            "published_activity_objects",
            "desired_edited_timestamp",
            "ALTER TABLE published_activity_objects ADD COLUMN desired_edited_timestamp DATETIME",
        ),
        (
            "published_activity_objects",
            "desired_fingerprint",
            "ALTER TABLE published_activity_objects ADD COLUMN desired_fingerprint VARCHAR(64)",
        ),
        (
            "published_activity_objects",
            "discord_reconciliation_needed",
            "ALTER TABLE published_activity_objects ADD COLUMN discord_reconciliation_needed BOOLEAN NOT NULL DEFAULT 0",
        ),
        (
            "published_activity_objects",
            "discord_reconciliation_attempt_count",
            "ALTER TABLE published_activity_objects ADD COLUMN discord_reconciliation_attempt_count INTEGER NOT NULL DEFAULT 0",
        ),
        (
            "published_activity_objects",
            "discord_reconciliation_next_attempt_at",
            "ALTER TABLE published_activity_objects ADD COLUMN discord_reconciliation_next_attempt_at DATETIME",
        ),
        (
            "published_activity_objects",
            "discord_reconciliation_last_error",
            "ALTER TABLE published_activity_objects ADD COLUMN discord_reconciliation_last_error VARCHAR(512)",
        ),
        (
            "published_activity_objects",
            "conversation_root_object_id",
            "ALTER TABLE published_activity_objects ADD COLUMN conversation_root_object_id VARCHAR(512)",
        ),
    ]
    # Stage 2 adds explicit local-community surface tables. Creating them here
    # keeps interrupted deployments and migrate-only test fixtures aligned with
    # the schema that the runtime expects after the refactor.
    Base.metadata.create_all(
        engine,
        tables=[
            BridgeActorKey.__table__,
            BridgePolicyEntry.__table__,
            CommunityActorBan.__table__,
            LocalCommunityThreadSurface.__table__,
            LocalCommunityMessageSurface.__table__,
            ManagementAuditEvent.__table__,
            GuildInvitePublication.__table__,
            ProfileMediaAsset.__table__,
            UserProfileField.__table__,
            UserActorFollower.__table__,
            UserActorFollowActivityId.__table__,
            UserActorOutboundSource.__table__,
            UserActorOutboundDelivery.__table__,
            UserActorOutboundDeliveryFollower.__table__,
            UserActorOutboundSourceMedia.__table__,
            UserActorContentMutation.__table__,
            PersonalFeedBinding.__table__,
            PersonalFeedThreadMapping.__table__,
            InboundConversationObject.__table__,
            InboundDiscordDelivery.__table__,
        ],
    )
    with engine.connect() as conn:
        _migrate_published_activity_objects_stage5(conn)
        _migrate_message_mappings_stage7(conn)
        for table, column, stmt in migrations:
            # PRAGMA table_info returns one row per column; skip if already present.
            existing = _table_columns(conn, table)
            # Partial legacy fixtures may omit unrelated tables entirely. A
            # missing table has nothing to migrate and may be created later by
            # the normal metadata path.
            if not existing:
                continue
            if column not in existing:
                conn.execute(text(stmt))
        if "conversation_root_object_id" in _table_columns(
            conn, "published_activity_objects"
        ):
            conn.execute(
                text(
                    "UPDATE published_activity_objects SET conversation_root_object_id = "
                    "CASE WHEN kind = 'post' THEN object_id ELSE COALESCE(in_reply_to_object_id, object_id) END "
                    "WHERE conversation_root_object_id IS NULL"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_published_activity_objects_conversation_root_object_id "
                    "ON published_activity_objects (conversation_root_object_id)"
                )
            )
        _migrate_local_communities_summary_nullable(conn)
        _migrate_generalized_user_bans(conn)
        # Stage 3 stores the selected LocalSubscriber on subscriber surface rows.
        # Existing host-only surface rows keep NULL, so this additive migration is
        # safe to run before or after Stage 2 backfill.
        for table in (
            "local_community_thread_surfaces",
            "local_community_message_surfaces",
        ):
            columns = _table_columns(conn, table)
            if "local_subscriber_id" not in columns:
                conn.execute(
                    text(f"ALTER TABLE {table} ADD COLUMN local_subscriber_id INTEGER")
                )
        _verify_stage2_surface_invariants(conn)
        _migrate_profile_field_name_width(conn)
        _migrate_user_actor_outbound_sources_stage5(conn)
        _install_profile_media_triggers(conn)
        # Partial legacy fixtures may not contain authored-object storage yet.
        if _table_columns(conn, "published_activity_objects"):
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_published_objects_user_outbox "
                    "ON published_activity_objects "
                    "(actor_username, actor_url, deleted_at, published_at, id)"
                )
            )
        conn.commit()


def _migrate_published_activity_objects_stage5(conn: Connection) -> None:
    """Rebuild authored objects to the current typed publication-context schema."""
    columns = _table_columns(conn, "published_activity_objects")
    required = {column.name for column in PublishedActivityObject.__table__.columns}
    if not columns or required.issubset(columns):
        return

    conn.execute(text("PRAGMA foreign_keys=OFF"))
    conn.execute(text("PRAGMA legacy_alter_table=ON"))
    conn.execute(
        text(
            "ALTER TABLE published_activity_objects RENAME TO published_activity_objects_legacy"
        )
    )
    conn.execute(text("PRAGMA legacy_alter_table=OFF"))
    PublishedActivityObject.__table__.create(conn)
    optional = {
        "content_revision": "1",
        "deleted_at": "NULL",
        "desired_kind": "'none'",
        "desired_title": "NULL",
        "desired_body_markdown": "NULL",
        "desired_edited_timestamp": "NULL",
        "desired_fingerprint": "NULL",
        "discord_reconciliation_needed": "0",
        "discord_reconciliation_attempt_count": "0",
        "discord_reconciliation_next_attempt_at": "NULL",
        "discord_reconciliation_last_error": "NULL",
    }

    def expr(name: str) -> str:
        return name if name in columns else optional[name]

    conn.execute(
        text(f"""
        INSERT INTO published_activity_objects (
            id, actor_username, actor_url, publication_context, community_actor_url,
            activity_id, object_id, kind, title, body_markdown, in_reply_to_object_id,
            discord_guild_id, discord_channel_id, discord_message_id, content_revision,
            deleted_at, desired_kind, desired_title, desired_body_markdown,
            desired_edited_timestamp, desired_fingerprint, discord_reconciliation_needed,
            discord_reconciliation_attempt_count, discord_reconciliation_next_attempt_at,
            discord_reconciliation_last_error, published_at, created_at
        )
        SELECT id, actor_username, actor_url, 'community', community_actor_url,
               activity_id, object_id, kind, title, body_markdown, in_reply_to_object_id,
               NULL, discord_channel_id, discord_message_id, {expr("content_revision")},
               {expr("deleted_at")}, {expr("desired_kind")}, {expr("desired_title")},
               {expr("desired_body_markdown")}, {expr("desired_edited_timestamp")},
               {expr("desired_fingerprint")}, {expr("discord_reconciliation_needed")},
               {expr("discord_reconciliation_attempt_count")},
               {expr("discord_reconciliation_next_attempt_at")},
               {expr("discord_reconciliation_last_error")}, published_at, created_at
        FROM published_activity_objects_legacy
    """)
    )
    conn.execute(text("DROP TABLE published_activity_objects_legacy"))
    conn.execute(text("PRAGMA foreign_keys=ON"))


def _migrate_message_mappings_stage7(conn: Connection) -> None:
    """Rebuild mappings with typed context and durable Discord guild identity."""
    columns = _table_columns(conn, "message_mappings")
    required = {column.name for column in MessageMapping.__table__.columns}
    if not columns or required.issubset(columns):
        return
    conn.execute(text("PRAGMA foreign_keys=OFF"))
    conn.execute(text("ALTER TABLE message_mappings RENAME TO message_mappings_legacy"))
    MessageMapping.__table__.create(conn)
    conn.execute(
        text("""
        INSERT INTO message_mappings (
            id, source_platform, source_id, activity_id, object_id, actor_url,
            publication_context, community_actor_url, discord_guild_id,
            discord_channel_id, discord_message_id, created_at
        )
        SELECT id, source_platform, source_id, activity_id, object_id, actor_url,
               'community', community_actor_url, NULL, discord_channel_id,
               discord_message_id, created_at
        FROM message_mappings_legacy
    """)
    )
    conn.execute(text("DROP TABLE message_mappings_legacy"))
    conn.execute(text("PRAGMA foreign_keys=ON"))


def _migrate_user_actor_outbound_sources_stage5(conn: Connection) -> None:
    """Generalize Stage 4 profile sources without losing retry state."""
    columns = _table_columns(conn, "user_actor_outbound_sources")
    if not columns or "snapshot_json" in columns:
        return
    conn.execute(text("PRAGMA foreign_keys=OFF"))
    # SQLite normally rewrites child-table foreign-key SQL during RENAME even
    # while enforcement is disabled.  Preserve the Stage 4 child references to
    # the canonical table name while this parent table is rebuilt.
    conn.execute(text("PRAGMA legacy_alter_table=ON"))
    conn.execute(
        text(
            "ALTER TABLE user_actor_outbound_sources RENAME TO user_actor_outbound_sources_stage4"
        )
    )
    conn.execute(text("PRAGMA legacy_alter_table=OFF"))
    conn.execute(text("DROP INDEX IF EXISTS ix_user_actor_sources_retry"))
    UserActorOutboundSource.__table__.create(conn)
    conn.execute(
        text(
            """
        INSERT INTO user_actor_outbound_sources (
            id, user_id, source_kind, ordering_key, logical_version, activity_id,
            published_object_id, snapshot_json, activity_json, state,
            render_attempt_count, last_render_attempted_at, last_error,
            next_attempt_at, created_at, updated_at
        )
        SELECT id, user_id, source_kind, 'profile:' || user_id, logical_version,
               activity_id, NULL, profile_snapshot_json, activity_json, state,
               render_attempt_count, last_render_attempted_at, last_error,
               next_attempt_at, created_at, updated_at
        FROM user_actor_outbound_sources_stage4
        """
        )
    )
    conn.execute(text("DROP TABLE user_actor_outbound_sources_stage4"))
    conn.execute(text("PRAGMA foreign_keys=ON"))


def _migrate_generalized_user_bans(conn: Connection) -> None:
    """Rebuild legacy community-only ban rows into explicit scoped rows.

    SQLite cannot make ``local_community_id`` nullable or replace the legacy
    uniqueness constraint in place. Existing rows remain community-scoped and
    retain status, reason, actor identity, creator, and timestamps.
    """
    columns = _table_columns(conn, "community_actor_bans")
    if not columns or "scope" in columns:
        return
    conn.execute(
        text(
            "ALTER TABLE community_actor_bans RENAME TO community_actor_bans_legacy_scope"
        )
    )
    conn.execute(
        text(
            """
        CREATE TABLE community_actor_bans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scope VARCHAR(32) NOT NULL,
            scope_key VARCHAR(64) NOT NULL,
            local_community_id INTEGER,
            actor_handle VARCHAR(255) NOT NULL,
            actor_url VARCHAR(512),
            target_discord_user_id VARCHAR(64),
            status VARCHAR(32) NOT NULL,
            created_by_discord_user_id VARCHAR(64),
            reason VARCHAR(1024),
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            UNIQUE (scope, scope_key, actor_handle, status)
        )
        """
        )
    )
    conn.execute(
        text(
            """
        INSERT INTO community_actor_bans (
            id, scope, scope_key, local_community_id, actor_handle, actor_url,
            target_discord_user_id, status, created_by_discord_user_id, reason,
            created_at, updated_at
        )
        SELECT id, 'community', CAST(local_community_id AS TEXT),
               local_community_id, actor_handle, actor_url, NULL, status,
               created_by_discord_user_id, reason, created_at, updated_at
        FROM community_actor_bans_legacy_scope
        """
        )
    )
    conn.execute(text("DROP TABLE community_actor_bans_legacy_scope"))
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_community_actor_bans_target_discord_user_id "
            "ON community_actor_bans (target_discord_user_id)"
        )
    )


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
    conn.execute(
        text(
            "ALTER TABLE local_communities RENAME TO local_communities_legacy_summary_not_null"
        )
    )
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

    created_by_expr = (
        "created_by_discord_user_id"
        if "created_by_discord_user_id" in existing_columns
        else "NULL"
    )
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


def _migrate_profile_field_name_width(conn: Connection) -> None:
    """Rebuild legacy profile fields so metadata names expose VARCHAR(255)."""
    table_info = _table_info(conn, "user_profile_fields")
    if not table_info:
        return
    name_row = next((row for row in table_info if row[1] == "name"), None)
    if name_row is None or str(name_row[2]).upper() != "VARCHAR(30)":
        return
    # SQLite does not enforce VARCHAR widths, but schema parity is part of the
    # durable contract handed to later stages and external schema inspection.
    conn.execute(
        text(
            "ALTER TABLE user_profile_fields RENAME TO user_profile_fields_legacy_name_width"
        )
    )
    conn.execute(
        text("""
        CREATE TABLE user_profile_fields (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            position INTEGER NOT NULL,
            name VARCHAR(255) NOT NULL,
            value VARCHAR(255) NOT NULL,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            UNIQUE (user_id, position),
            CONSTRAINT ck_user_profile_field_position CHECK (position >= 0 AND position <= 3)
        )
    """)
    )
    conn.execute(
        text("""
        INSERT INTO user_profile_fields (id, user_id, position, name, value, created_at, updated_at)
        SELECT id, user_id, position, name, value, created_at, updated_at
        FROM user_profile_fields_legacy_name_width
    """)
    )
    conn.execute(text("DROP TABLE user_profile_fields_legacy_name_width"))


def _install_profile_media_triggers(conn: Connection) -> None:
    """Enforce slot-to-kind consistency for every SQLite write path."""
    if not _table_columns(conn, "users") or not _table_columns(
        conn, "profile_media_assets"
    ):
        return
    conn.execute(
        text("""
        CREATE TRIGGER IF NOT EXISTS users_avatar_media_kind_insert
        BEFORE INSERT ON users WHEN NEW.avatar_media_id IS NOT NULL
        BEGIN
          SELECT CASE WHEN (SELECT media_kind FROM profile_media_assets WHERE id = NEW.avatar_media_id) != 'avatar'
            THEN RAISE(ABORT, 'avatar_media_id must reference avatar media') END;
        END
    """)
    )
    conn.execute(
        text("""
        CREATE TRIGGER IF NOT EXISTS users_avatar_media_kind_update
        BEFORE UPDATE OF avatar_media_id ON users WHEN NEW.avatar_media_id IS NOT NULL
        BEGIN
          SELECT CASE WHEN (SELECT media_kind FROM profile_media_assets WHERE id = NEW.avatar_media_id) != 'avatar'
            THEN RAISE(ABORT, 'avatar_media_id must reference avatar media') END;
        END
    """)
    )
    conn.execute(
        text("""
        CREATE TRIGGER IF NOT EXISTS users_header_media_kind_insert
        BEFORE INSERT ON users WHEN NEW.header_media_id IS NOT NULL
        BEGIN
          SELECT CASE WHEN (SELECT media_kind FROM profile_media_assets WHERE id = NEW.header_media_id) != 'header'
            THEN RAISE(ABORT, 'header_media_id must reference header media') END;
        END
    """)
    )
    conn.execute(
        text("""
        CREATE TRIGGER IF NOT EXISTS users_header_media_kind_update
        BEFORE UPDATE OF header_media_id ON users WHEN NEW.header_media_id IS NOT NULL
        BEGIN
          SELECT CASE WHEN (SELECT media_kind FROM profile_media_assets WHERE id = NEW.header_media_id) != 'header'
            THEN RAISE(ABORT, 'header_media_id must reference header media') END;
        END
    """)
    )
