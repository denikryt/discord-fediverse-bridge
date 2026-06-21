"""Additive and compatibility schema migration helpers for bridge SQLite DBs.

Database owns the engine and calls this module from `Database.migrate()`. The
helpers preserve the previous additive migration behavior and include one narrow
SQLite table-rebuild migration for the local-community summary nullability
change, because SQLite cannot drop a NOT NULL constraint in place.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from urllib.parse import urlsplit

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from ..activitypub_identity_urls import (
    LocalActorKind,
    build_actor_address,
    build_community_announce_comment_activity_id,
    build_community_announce_post_activity_id,
    build_community_comment_object_id,
    build_community_create_comment_activity_id,
    build_community_create_post_activity_id,
    build_community_post_object_id,
    build_user_comment_object_id,
    build_user_create_comment_activity_id,
    build_user_create_post_activity_id,
    build_user_delete_comment_activity_id,
    build_user_delete_post_activity_id,
    build_user_post_object_id,
    build_user_update_comment_activity_id,
    build_user_update_post_activity_id,
    build_user_profile_update_activity_id,
)
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


@dataclass(frozen=True, slots=True)
class Stage4IdentityMigrationConfig:
    """Non-secret identity settings required by the Stage 4 URL cutover."""

    public_origin: str
    service_actor_identifier: str


def migrate(engine: Engine, stage4_identity: Stage4IdentityMigrationConfig | None = None) -> None:
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
        _migrate_stage4_typed_activitypub_identity(conn, stage4_identity)
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


def _migrate_stage4_typed_activitypub_identity(
    conn: Connection, config: Stage4IdentityMigrationConfig | None
) -> None:
    """Convert retained same-origin local ActivityPub identity to typed URLs.

    The migration deliberately receives only non-secret identity configuration.
    It rewrites local application rows, purges stale remote relationship state,
    and then checks the retained local columns before the surrounding transaction
    commits so startup cannot finish in a mixed namespace state.
    """
    if config is None:
        # Fresh-schema tests may run migration without runtime settings before
        # any local identity exists. Populated local identity rows require the
        # narrow non-secret config so service key ownership cannot be inferred
        # from stale /actors/{identifier} rows after FEDIFY_ACTOR_IDENTIFIER
        # changes.
        for table in ("users", "local_communities", "bridge_actor_keys"):
            if not _table_columns(conn, table):
                continue
            count = int(conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar() or 0)
            if count:
                raise RuntimeError(
                    "Stage 4 identity migration requires public_origin and "
                    "service_actor_identifier when local identity rows exist"
                )
        return

    origin = _normalize_stage4_origin(config.public_origin)
    service = build_actor_address(
        origin=origin,
        kind=LocalActorKind.SERVICE,
        identifier=config.service_actor_identifier,
    )

    user_rows = list(
        conn.execute(
            text("SELECT id, activitypub_username, actor_url FROM users")
        ).mappings()
    ) if _table_columns(conn, "users") else []
    user_actor_map: dict[str, str] = {}
    for row in user_rows:
        username = str(row["activitypub_username"])
        address = build_actor_address(
            origin=origin, kind=LocalActorKind.USER, identifier=username
        )
        old_actor = row["actor_url"]
        if isinstance(old_actor, str):
            user_actor_map[old_actor] = address.actor_url
        conn.execute(
            text(
                "UPDATE users SET actor_url=:actor_url, inbox_url=:inbox_url, "
                "outbox_url=:outbox_url, followers_url=:followers_url WHERE id=:id"
            ),
            {
                "id": row["id"],
                "actor_url": address.actor_url,
                "inbox_url": address.inbox_url,
                "outbox_url": address.outbox_url,
                "followers_url": address.followers_url,
            },
        )

    community_rows = list(
        conn.execute(
            text("SELECT id, slug, actor_url FROM local_communities")
        ).mappings()
    ) if _table_columns(conn, "local_communities") else []
    community_actor_map: dict[str, str] = {}
    community_slug_by_id: dict[int, str] = {}
    for row in community_rows:
        slug = str(row["slug"])
        community_slug_by_id[int(row["id"])] = slug
        address = build_actor_address(
            origin=origin, kind=LocalActorKind.COMMUNITY, identifier=slug
        )
        old_actor = row["actor_url"]
        if isinstance(old_actor, str):
            community_actor_map[old_actor] = address.actor_url
        conn.execute(
            text(
                "UPDATE local_communities SET actor_url=:actor_url, inbox_url=:inbox_url, "
                "outbox_url=:outbox_url, followers_url=:followers_url WHERE id=:id"
            ),
            {
                "id": row["id"],
                "actor_url": address.actor_url,
                "inbox_url": address.inbox_url,
                "outbox_url": address.outbox_url,
                "followers_url": address.followers_url,
            },
        )

    if _table_columns(conn, "bridge_actor_keys"):
        conn.execute(
            text("UPDATE bridge_actor_keys SET actor_url=:actor_url, key_id=:key_id"),
            {"actor_url": service.actor_url, "key_id": service.public_key_url},
        )

    object_id_map = _migrate_stage4_published_objects(conn, origin, user_actor_map, community_actor_map)
    _migrate_stage4_message_mappings(conn, origin, user_actor_map, community_actor_map)
    object_id_map.update(_migrate_stage4_local_community_content(conn, origin, community_slug_by_id))
    _migrate_stage4_user_actor_outbound_sources(conn, origin, object_id_map, user_actor_map, community_actor_map)
    _migrate_stage4_content_mutations(conn, origin, object_id_map, user_actor_map, community_actor_map)
    _migrate_stage4_local_community_relay_deliveries(conn, origin, community_slug_by_id)
    _purge_stage4_remote_relationship_state(conn)
    _verify_stage4_identity_invariants(conn, origin)


def _normalize_stage4_origin(origin: str) -> str:
    """Normalize the public ActivityPub origin used by SQL migration helpers."""
    parsed = urlsplit(origin)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError("Stage 4 migration public_origin must be an http(s) origin")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise RuntimeError("Stage 4 migration public_origin must be origin-only")
    if parsed.path not in {"", "/"}:
        raise RuntimeError("Stage 4 migration public_origin must not contain a path")
    host = parsed.hostname
    if host is None:
        raise RuntimeError("Stage 4 migration public_origin must include a host")
    port = f":{parsed.port}" if parsed.port is not None else ""
    return f"{parsed.scheme}://{host}{port}"


def _is_same_origin_url(value: str | None, origin: str) -> bool:
    """Return whether one persisted URL belongs to this bridge origin."""
    if not value:
        return False
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    host = parsed.hostname
    if host is None:
        return False
    port = f":{parsed.port}" if parsed.port is not None else ""
    return f"{parsed.scheme}://{host}{port}" == origin


def _legacy_tail(value: str, origin: str) -> str | None:
    """Return the path tail for a same-origin URL or None for remote values."""
    if not _is_same_origin_url(value, origin):
        return None
    return urlsplit(value).path.strip("/")


def _terminal_id(value: str, origin: str) -> str:
    """Extract the terminal stable id from a retained local AP URL."""
    tail = _legacy_tail(value, origin)
    if tail is None:
        return value.rsplit("/", 1)[-1]
    terminal = tail.rsplit("/", 1)[-1]
    if not terminal:
        raise RuntimeError(f"Cannot migrate local URL without terminal id: {value}")
    return terminal


def _migrate_stage4_published_objects(
    conn: Connection,
    origin: str,
    user_actor_map: dict[str, str],
    community_actor_map: dict[str, str],
) -> dict[str, str]:
    """Rewrite retained published object rows to typed actor/object/activity IDs."""
    if not _table_columns(conn, "published_activity_objects"):
        return {}
    rows = list(conn.execute(text("SELECT * FROM published_activity_objects")).mappings())
    object_id_map: dict[str, str] = {}
    updates: list[tuple[int, dict[str, object]]] = []
    for row in rows:
        kind = row["kind"]
        old_object = row["object_id"]
        old_activity = row["activity_id"]
        publication_context = row.get("publication_context") or "community"
        values: dict[str, object] = {"id": row["id"]}
        if publication_context == "personal_feed":
            username = str(row["actor_username"])
            object_token = _terminal_id(str(old_object), origin)
            activity_token = _terminal_id(str(old_activity), origin)
            if kind == "comment":
                new_object = build_user_comment_object_id(origin=origin, username=username, object_id=object_token)
                new_activity = build_user_create_comment_activity_id(origin=origin, username=username, activity_id=activity_token)
            else:
                new_object = build_user_post_object_id(origin=origin, username=username, object_id=object_token)
                new_activity = build_user_create_post_activity_id(origin=origin, username=username, activity_id=activity_token)
            address = build_actor_address(origin=origin, kind=LocalActorKind.USER, identifier=username)
            values.update(actor_url=address.actor_url, object_id=new_object, activity_id=new_activity)
        elif row["community_actor_url"] in community_actor_map or _legacy_tail(str(row["community_actor_url"] or ""), origin):
            slug = _slug_from_actor_url(str(row["community_actor_url"] or row["actor_url"]), origin)
            if slug is None:
                continue
            object_token = _terminal_id(str(old_object), origin)
            activity_token = _terminal_id(str(old_activity), origin)
            if kind == "comment":
                new_object = build_community_comment_object_id(origin=origin, slug=slug, object_id=object_token)
                new_activity = build_community_create_comment_activity_id(origin=origin, slug=slug, activity_id=activity_token)
            else:
                new_object = build_community_post_object_id(origin=origin, slug=slug, object_id=object_token)
                new_activity = build_community_create_post_activity_id(origin=origin, slug=slug, activity_id=activity_token)
            address = build_actor_address(origin=origin, kind=LocalActorKind.COMMUNITY, identifier=slug)
            values.update(actor_url=address.actor_url, community_actor_url=address.actor_url, object_id=new_object, activity_id=new_activity)
        else:
            continue
        object_id_map[str(old_object)] = str(values["object_id"])
        updates.append((int(row["id"]), values))
    for _row_id, values in updates:
        conn.execute(text("UPDATE published_activity_objects SET actor_url=COALESCE(:actor_url, actor_url), community_actor_url=:community_actor_url, activity_id=:activity_id, object_id=:object_id WHERE id=:id"), {
            "id": values["id"],
            "actor_url": values.get("actor_url"),
            "community_actor_url": values.get("community_actor_url"),
            "activity_id": values["activity_id"],
            "object_id": values["object_id"],
        })
    for old, new in object_id_map.items():
        conn.execute(text("UPDATE published_activity_objects SET in_reply_to_object_id=:new WHERE in_reply_to_object_id=:old"), {"old": old, "new": new})
        conn.execute(text("UPDATE published_activity_objects SET conversation_root_object_id=:new WHERE conversation_root_object_id=:old"), {"old": old, "new": new})
    return object_id_map


def _migrate_stage4_message_mappings(
    conn: Connection,
    origin: str,
    user_actor_map: dict[str, str],
    community_actor_map: dict[str, str],
) -> None:
    """Rewrite local message mappings without changing Discord mapping columns."""
    if not _table_columns(conn, "message_mappings"):
        return
    rows = list(conn.execute(text("SELECT * FROM message_mappings")).mappings())
    for row in rows:
        context = row.get("publication_context") or "community"
        object_token = _terminal_id(str(row["object_id"]), origin)
        activity_token = _terminal_id(str(row["activity_id"]), origin)
        if context == "personal_feed":
            username = _username_from_actor_url(str(row["actor_url"]), origin) or str(row["actor_url"]).rstrip('/').rsplit('/', 1)[-1]
            kind = "comments" if "/comment" in str(row["object_id"]) else "posts"
            object_id = build_user_comment_object_id(origin=origin, username=username, object_id=object_token) if kind == "comments" else build_user_post_object_id(origin=origin, username=username, object_id=object_token)
            activity_id = build_user_create_comment_activity_id(origin=origin, username=username, activity_id=activity_token) if kind == "comments" else build_user_create_post_activity_id(origin=origin, username=username, activity_id=activity_token)
            actor_url = build_actor_address(origin=origin, kind=LocalActorKind.USER, identifier=username).actor_url
            community_actor_url = None
        else:
            slug = _slug_from_actor_url(str(row["community_actor_url"] or row["actor_url"]), origin)
            if slug is None:
                continue
            kind = "comments" if "/comment" in str(row["object_id"]) else "posts"
            object_id = build_community_comment_object_id(origin=origin, slug=slug, object_id=object_token) if kind == "comments" else build_community_post_object_id(origin=origin, slug=slug, object_id=object_token)
            activity_id = build_community_create_comment_activity_id(origin=origin, slug=slug, activity_id=activity_token) if kind == "comments" else build_community_create_post_activity_id(origin=origin, slug=slug, activity_id=activity_token)
            actor_url = build_actor_address(origin=origin, kind=LocalActorKind.COMMUNITY, identifier=slug).actor_url
            community_actor_url = actor_url
        conn.execute(text("UPDATE message_mappings SET actor_url=:actor_url, community_actor_url=:community_actor_url, activity_id=:activity_id, object_id=:object_id WHERE id=:id"), {"id": row["id"], "actor_url": actor_url, "community_actor_url": community_actor_url, "activity_id": activity_id, "object_id": object_id})


def _migrate_stage4_local_community_content(
    conn: Connection, origin: str, community_slug_by_id: dict[int, str]
) -> dict[str, str]:
    """Rewrite canonical local-community rows while preserving surface rows."""
    thread_object_map: dict[str, str] = {}
    all_object_map: dict[str, str] = {}
    if _table_columns(conn, "local_community_threads"):
        for row in conn.execute(text("SELECT * FROM local_community_threads")).mappings():
            if row.get("origin_kind") not in {"discord", "local", "local_discord"} and row.get("direction") not in {"discord_to_fediverse", "outbound", "local"}:
                continue
            slug = community_slug_by_id.get(int(row["local_community_id"]))
            if slug is None:
                continue
            old_object = str(row["ap_object_id"])
            object_token = _terminal_id(old_object, origin)
            activity_token = _terminal_id(str(row["ap_activity_id"]), origin)
            new_object = build_community_post_object_id(
                origin=origin, slug=slug, object_id=object_token
            )
            thread_object_map[old_object] = new_object
            all_object_map[old_object] = new_object
            conn.execute(
                text(
                    "UPDATE local_community_threads SET ap_object_id=:object_id, "
                    "ap_activity_id=:activity_id WHERE id=:id"
                ),
                {
                    "id": row["id"],
                    "object_id": new_object,
                    "activity_id": build_community_create_post_activity_id(
                        origin=origin, slug=slug, activity_id=activity_token
                    ),
                },
            )
    message_columns = _table_columns(conn, "local_community_messages")
    thread_columns = _table_columns(conn, "local_community_threads")
    if {"local_community_thread_id", "ap_activity_id", "ap_object_id"}.issubset(message_columns) and {"id", "local_community_id"}.issubset(thread_columns):
        parent_map: dict[str, str] = {}
        rows = list(
            conn.execute(
                text(
                    "SELECT m.*, t.local_community_id FROM local_community_messages m "
                    "JOIN local_community_threads t ON t.id=m.local_community_thread_id"
                )
            ).mappings()
        )
        for row in rows:
            if row.get("direction") not in {"discord_to_fediverse", "outbound", "local"}:
                continue
            slug = community_slug_by_id.get(int(row["local_community_id"]))
            if slug is None:
                continue
            old_object = str(row["ap_object_id"])
            object_token = _terminal_id(old_object, origin)
            activity_token = _terminal_id(str(row["ap_activity_id"]), origin)
            new_object = build_community_comment_object_id(origin=origin, slug=slug, object_id=object_token)
            parent_map[old_object] = new_object
            all_object_map[old_object] = new_object
            conn.execute(text("UPDATE local_community_messages SET ap_object_id=:object_id, ap_activity_id=:activity_id WHERE id=:id"), {"id": row["id"], "object_id": new_object, "activity_id": build_community_create_comment_activity_id(origin=origin, slug=slug, activity_id=activity_token)})
        for old, new in {**thread_object_map, **parent_map}.items():
            conn.execute(
                text(
                    "UPDATE local_community_messages SET parent_ap_object_id=:new "
                    "WHERE parent_ap_object_id=:old"
                ),
                {"old": old, "new": new},
            )
    return all_object_map


def _migrate_stage4_user_actor_outbound_sources(
    conn: Connection,
    origin: str,
    object_id_map: dict[str, str],
    user_actor_map: dict[str, str],
    community_actor_map: dict[str, str],
) -> None:
    """Migrate retained User Actor delivery sources and cached AP JSON."""
    source_columns = _table_columns(conn, "user_actor_outbound_sources")
    if not source_columns or not _table_columns(conn, "users"):
        return
    rows = list(
        conn.execute(
            text(
                "SELECT s.*, u.activitypub_username, u.actor_url AS user_actor_url, "
                "p.object_id AS published_object_ap_id, p.activity_id AS published_activity_id, "
                "p.kind AS published_kind, p.community_actor_url AS published_community_actor_url, "
                "p.conversation_root_object_id AS published_conversation_root_object_id, "
                "p.in_reply_to_object_id AS published_in_reply_to_object_id "
                "FROM user_actor_outbound_sources s "
                "JOIN users u ON u.id=s.user_id "
                "LEFT JOIN published_activity_objects p ON p.id=s.published_object_id"
            )
        ).mappings()
    )
    for row in rows:
        username = str(row["activitypub_username"])
        source_kind = str(row["source_kind"])
        old_activity_id = str(row["activity_id"])
        if source_kind == "profile_update":
            new_activity_id = build_user_profile_update_activity_id(
                origin=origin,
                username=username,
                profile_version_or_activity_id=row["logical_version"],
            )
        else:
            token = _terminal_id(old_activity_id, origin)
            object_kind = "comments" if str(row.get("published_kind")) == "comment" else "posts"
            if source_kind == "content_delete":
                new_activity_id = (
                    build_user_delete_comment_activity_id(origin=origin, username=username, activity_id=token)
                    if object_kind == "comments"
                    else build_user_delete_post_activity_id(origin=origin, username=username, activity_id=token)
                )
            elif source_kind == "content_update":
                new_activity_id = (
                    build_user_update_comment_activity_id(origin=origin, username=username, activity_id=token)
                    if object_kind == "comments"
                    else build_user_update_post_activity_id(origin=origin, username=username, activity_id=token)
                )
            else:
                new_activity_id = (
                    build_user_create_comment_activity_id(origin=origin, username=username, activity_id=token)
                    if object_kind == "comments"
                    else build_user_create_post_activity_id(origin=origin, username=username, activity_id=token)
                )
        typed_user_actor = build_actor_address(
            origin=origin, kind=LocalActorKind.USER, identifier=username
        ).actor_url
        replacements = dict(object_id_map)
        replacements[old_activity_id] = new_activity_id
        replacements[str(row["user_actor_url"])] = typed_user_actor
        if row.get("published_object_ap_id") is not None:
            # The joined published row has already been migrated in this transaction.
            replacements[str(row["published_object_ap_id"])] = str(row["published_object_ap_id"])
        if row.get("published_activity_id") is not None:
            replacements[str(row["published_activity_id"])] = str(row["published_activity_id"])
        for maybe_local in (
            row.get("published_community_actor_url"),
            row.get("published_conversation_root_object_id"),
            row.get("published_in_reply_to_object_id"),
        ):
            if isinstance(maybe_local, str) and maybe_local in object_id_map:
                replacements[maybe_local] = object_id_map[maybe_local]
        snapshot_json = _migrate_stage4_json_column(
            row.get("snapshot_json"), origin, replacements, user_actor_map, community_actor_map
        )
        activity_json = _migrate_stage4_json_column(
            row.get("activity_json"), origin, replacements, user_actor_map, community_actor_map
        )
        conn.execute(
            text(
                "UPDATE user_actor_outbound_sources SET activity_id=:activity_id, "
                "snapshot_json=:snapshot_json, activity_json=:activity_json WHERE id=:id"
            ),
            {
                "id": row["id"],
                "activity_id": new_activity_id,
                "snapshot_json": snapshot_json,
                "activity_json": activity_json,
            },
        )


def _migrate_stage4_json_column(
    raw_json: object,
    origin: str,
    replacements: dict[str, str],
    user_actor_map: dict[str, str],
    community_actor_map: dict[str, str],
) -> object:
    parsed = _json_load_or_none(raw_json)
    if parsed is None:
        return raw_json
    migrated = _migrate_stage4_json_value(
        parsed, origin, replacements, user_actor_map, community_actor_map
    )
    return json.dumps(migrated, separators=(",", ":"), sort_keys=True)


def _migrate_stage4_content_mutations(
    conn: Connection,
    origin: str,
    object_id_map: dict[str, str],
    user_actor_map: dict[str, str],
    community_actor_map: dict[str, str],
) -> None:
    """Rewrite retained user content mutation rows and cached JSON payloads."""
    columns = _table_columns(conn, "user_actor_content_mutations")
    if not columns or not _table_columns(conn, "users") or not _table_columns(conn, "published_activity_objects"):
        return
    required = {"id", "published_object_id", "user_id", "kind", "operation_id", "activity_id", "request_json"}
    if not required.issubset(columns):
        return
    rows = list(
        conn.execute(
            text(
                "SELECT m.*, u.activitypub_username, u.actor_url AS user_actor_url, "
                "p.object_id AS published_object_ap_id, p.kind AS published_kind, "
                "p.community_actor_url AS published_community_actor_url, "
                "p.conversation_root_object_id AS published_conversation_root_object_id, "
                "p.in_reply_to_object_id AS published_in_reply_to_object_id "
                "FROM user_actor_content_mutations m "
                "JOIN users u ON u.id=m.user_id "
                "JOIN published_activity_objects p ON p.id=m.published_object_id"
            )
        ).mappings()
    )
    for row in rows:
        username = str(row["activitypub_username"])
        object_kind = "comments" if str(row["published_kind"]) == "comment" or "/comments/" in str(row["published_object_ap_id"]) else "posts"
        typed_user_actor = build_actor_address(
            origin=origin, kind=LocalActorKind.USER, identifier=username
        ).actor_url
        replacements = dict(object_id_map)
        replacements[str(row["user_actor_url"])] = typed_user_actor
        request = _json_load_or_none(row["request_json"])
        if isinstance(request, dict):
            request["apObjectId"] = str(row["published_object_ap_id"])
            if request.get("communityActorUrl"):
                request["communityActorUrl"] = _migrate_stage4_json_local_url(
                    str(request["communityActorUrl"]), origin, replacements, user_actor_map, community_actor_map
                )
            for key, replacement in (
                ("conversationRootObjectId", row["published_conversation_root_object_id"]),
                ("inReplyToObjectId", row["published_in_reply_to_object_id"]),
            ):
                if request.get(key) and replacement:
                    request[key] = str(replacement)
                elif request.get(key):
                    request[key] = _migrate_stage4_json_local_url(
                        str(request[key]), origin, replacements, user_actor_map, community_actor_map
                    )
            operation_id = _stage4_content_mutation_operation_id(
                kind=str(row["kind"]),
                expected_revision=int(row["expected_revision"]),
                next_revision=int(row["next_revision"]),
                request=request,
            )
        else:
            operation_id = str(row["operation_id"])
        if row["kind"] == "content_delete":
            activity_id = (
                build_user_delete_comment_activity_id(origin=origin, username=username, activity_id=operation_id)
                if object_kind == "comments"
                else build_user_delete_post_activity_id(origin=origin, username=username, activity_id=operation_id)
            )
        else:
            activity_id = (
                build_user_update_comment_activity_id(origin=origin, username=username, activity_id=operation_id)
                if object_kind == "comments"
                else build_user_update_post_activity_id(origin=origin, username=username, activity_id=operation_id)
            )
        replacements[str(row["activity_id"])] = activity_id
        if isinstance(request, dict):
            request["operationId"] = operation_id
            request["activityId"] = activity_id
            request_json = json.dumps(request, separators=(",", ":"), sort_keys=True)
        else:
            request_json = row["request_json"]
        canonical_json = row.get("canonical_activity_json") if "canonical_activity_json" in row else None
        if canonical_json is not None:
            canonical = _json_load_or_none(canonical_json)
            if canonical is not None:
                canonical = _migrate_stage4_json_value(
                    canonical, origin, replacements, user_actor_map, community_actor_map
                )
                canonical_json = json.dumps(canonical, separators=(",", ":"), sort_keys=True)
        conn.execute(
            text(
                "UPDATE user_actor_content_mutations SET operation_id=:operation_id, "
                "activity_id=:activity_id, request_json=:request_json, "
                "canonical_activity_json=:canonical_activity_json WHERE id=:id"
            ),
            {
                "id": row["id"],
                "operation_id": operation_id,
                "activity_id": activity_id,
                "request_json": request_json,
                "canonical_activity_json": canonical_json,
            },
        )


def _stage4_content_mutation_operation_id(
    *, kind: str, expected_revision: int, next_revision: int, request: dict[str, object]
) -> str:
    """Recompute the deterministic mutation operation id after local URL migration."""
    identity_request = {
        "actorUsername": request.get("actorUsername"),
        "publicationContext": request.get("publicationContext"),
        "communityActorUrl": request.get("communityActorUrl"),
        "conversationRootObjectId": request.get("conversationRootObjectId"),
        "apObjectId": request.get("apObjectId"),
        "kind": request.get("kind") if kind == "content_update" else None,
        "title": request.get("title") if kind == "content_update" else None,
        "bodyMarkdown": request.get("bodyMarkdown") if kind == "content_update" else None,
        "inReplyToObjectId": request.get("inReplyToObjectId") if kind == "content_update" else None,
        "targetInboxUrls": request.get("targetInboxUrls"),
        "mutationTimestamp": request.get("mutationTimestamp"),
    }
    identity = json.dumps(
        {
            "objectId": request.get("apObjectId"),
            "kind": kind,
            "expectedRevision": expected_revision,
            "nextRevision": next_revision,
            "request": identity_request,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(identity.encode()).hexdigest()


def _migrate_stage4_local_community_relay_deliveries(
    conn: Connection, origin: str, community_slug_by_id: dict[int, str]
) -> None:
    """Migrate retained local-community relay Announce ids to typed grammar."""
    columns = _table_columns(conn, "local_community_relay_deliveries")
    if not columns or "relay_activity_id" not in columns:
        return
    required = {"id", "local_community_id", "object_kind", "relay_activity_id"}
    if not required.issubset(columns):
        return
    rows = list(
        conn.execute(
            text(
                "SELECT id, local_community_id, object_kind, relay_activity_id "
                "FROM local_community_relay_deliveries WHERE relay_activity_id IS NOT NULL"
            )
        ).mappings()
    )
    for row in rows:
        old_id = str(row["relay_activity_id"])
        if not _is_same_origin_url(old_id, origin):
            continue
        slug = community_slug_by_id.get(int(row["local_community_id"]))
        if slug is None:
            conn.execute(text("UPDATE local_community_relay_deliveries SET relay_activity_id=NULL WHERE id=:id"), {"id": row["id"]})
            continue
        token = _terminal_id(old_id, origin)
        object_kind = str(row["object_kind"])
        new_id = (
            build_community_announce_comment_activity_id(origin=origin, slug=slug, activity_id=token)
            if object_kind == "comment"
            else build_community_announce_post_activity_id(origin=origin, slug=slug, activity_id=token)
        )
        conn.execute(
            text("UPDATE local_community_relay_deliveries SET relay_activity_id=:relay_activity_id WHERE id=:id"),
            {"id": row["id"], "relay_activity_id": new_id},
        )


def _json_load_or_none(value: object) -> object | None:
    if not isinstance(value, str):
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def _migrate_stage4_json_value(
    value: object,
    origin: str,
    replacements: dict[str, str],
    user_actor_map: dict[str, str],
    community_actor_map: dict[str, str],
) -> object:
    if isinstance(value, str):
        return _migrate_stage4_json_local_url(value, origin, replacements, user_actor_map, community_actor_map)
    if isinstance(value, list):
        return [_migrate_stage4_json_value(item, origin, replacements, user_actor_map, community_actor_map) for item in value]
    if isinstance(value, dict):
        return {key: _migrate_stage4_json_value(item, origin, replacements, user_actor_map, community_actor_map) for key, item in value.items()}
    return value


def _migrate_stage4_json_local_url(
    value: str,
    origin: str,
    replacements: dict[str, str],
    user_actor_map: dict[str, str],
    community_actor_map: dict[str, str],
) -> str:
    if value in replacements:
        return replacements[value]
    if value in user_actor_map:
        return user_actor_map[value]
    if value in community_actor_map:
        return community_actor_map[value]
    if not _is_same_origin_url(value, origin):
        return value
    username = _username_from_actor_url(value, origin)
    if username is not None:
        return build_actor_address(origin=origin, kind=LocalActorKind.USER, identifier=username).actor_url
    slug = _slug_from_actor_url(value, origin)
    if slug is not None:
        return build_actor_address(origin=origin, kind=LocalActorKind.COMMUNITY, identifier=slug).actor_url
    actor_child = _stage4_actor_child_url(value, origin)
    if actor_child is not None:
        return actor_child
    return value


def _stage4_actor_child_url(value: str, origin: str) -> str | None:
    """Rewrite legacy actor-owned inbox/outbox/followers URLs in cached JSON."""
    tail = _legacy_tail(value, origin)
    if tail is None:
        return None
    parts = tail.split("/")
    if not parts or parts[-1] not in {"inbox", "outbox", "followers"}:
        return None
    child = parts[-1]
    if len(parts) == 3 and parts[0] == "actors":
        address = build_actor_address(origin=origin, kind=LocalActorKind.USER, identifier=parts[1])
    elif len(parts) == 3 and parts[0] in {"communities", "c"}:
        address = build_actor_address(origin=origin, kind=LocalActorKind.COMMUNITY, identifier=parts[1])
    elif len(parts) == 4 and parts[0] == "actors" and parts[1] == "users":
        address = build_actor_address(origin=origin, kind=LocalActorKind.USER, identifier=parts[2])
    elif len(parts) == 4 and parts[0] == "actors" and parts[1] == "communities":
        address = build_actor_address(origin=origin, kind=LocalActorKind.COMMUNITY, identifier=parts[2])
    elif len(parts) == 4 and parts[0] == "actors" and parts[1] == "services":
        address = build_actor_address(origin=origin, kind=LocalActorKind.SERVICE, identifier=parts[2])
    else:
        return None
    return getattr(address, f"{child}_url")

def _purge_stage4_remote_relationship_state(conn: Connection) -> None:
    """Remove old local-actor relationship state that cannot be reused safely."""
    for table in ("user_actor_follow_activity_ids", "user_actor_followers", "remote_subscribers", "bridge_actor_follows"):
        if _table_columns(conn, table):
            conn.execute(text(f"DELETE FROM {table}"))
    subscription_columns = _table_columns(conn, "channel_community_subscriptions")
    if {"follow_activity_id", "status"}.issubset(subscription_columns):
        conn.execute(
            text(
                "UPDATE channel_community_subscriptions "
                "SET follow_activity_id=NULL, status='pending'"
            )
        )


def _verify_stage4_identity_invariants(conn: Connection, origin: str) -> None:
    """Fail before commit if retained local rows still expose retired URL roots."""
    retired = ("/communities/", "/c/", "/users/", "/activities/", "/actors/")
    checks = [
        ("users", ("actor_url", "inbox_url", "outbox_url", "followers_url")),
        ("local_communities", ("actor_url", "inbox_url", "outbox_url", "followers_url")),
        ("bridge_actor_keys", ("actor_url", "key_id")),
        ("published_activity_objects", ("actor_url", "community_actor_url", "activity_id", "object_id", "in_reply_to_object_id", "conversation_root_object_id")),
        ("message_mappings", ("actor_url", "community_actor_url", "activity_id", "object_id")),
        ("local_community_threads", ("ap_activity_id", "ap_object_id")),
        ("local_community_messages", ("ap_activity_id", "ap_object_id", "parent_ap_object_id")),
        ("user_actor_outbound_sources", ("activity_id", "snapshot_json", "activity_json")),
        ("user_actor_content_mutations", ("activity_id", "request_json", "canonical_activity_json")),
        ("local_community_relay_deliveries", ("relay_activity_id",)),
    ]
    for table, columns in checks:
        existing = _table_columns(conn, table)
        if not existing:
            continue
        wanted = [column for column in columns if column in existing]
        if not wanted:
            continue
        rows = conn.execute(text(f"SELECT id, {', '.join(wanted)} FROM {table}")).mappings()
        for row in rows:
            for column in wanted:
                raw_value = row[column]
                values = [raw_value]
                if column.endswith("_json") and isinstance(raw_value, str):
                    parsed = _json_load_or_none(raw_value)
                    if parsed is not None:
                        values = list(_iter_stage4_json_strings(parsed))
                for value in values:
                    if not isinstance(value, str) or not _is_same_origin_url(value, origin):
                        continue
                    path = urlsplit(value).path
                    allowed = path.startswith("/actors/users/") or path.startswith("/actors/communities/") or path.startswith("/actors/services/")
                    if not allowed and any(path.startswith(prefix) for prefix in retired):
                        raise RuntimeError(f"Stage 4 migration left retired local URL in {table}.{column} id={row['id']}: {value}")
                    if path.startswith("/actors/") and not allowed:
                        raise RuntimeError(f"Stage 4 migration left untyped actor URL in {table}.{column} id={row['id']}: {value}")


def _iter_stage4_json_strings(value: object):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _iter_stage4_json_strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _iter_stage4_json_strings(item)


def _slug_from_actor_url(value: str, origin: str) -> str | None:
    """Extract a local community slug from legacy or typed community actor URLs."""
    tail = _legacy_tail(value, origin)
    if tail is None:
        return None
    parts = tail.split("/")
    if len(parts) == 2 and parts[0] in {"communities", "c"}:
        return parts[1]
    if len(parts) == 3 and parts[0] == "actors" and parts[1] == "communities":
        return parts[2]
    return None


def _username_from_actor_url(value: str, origin: str) -> str | None:
    """Extract a local username from legacy or typed user actor URLs."""
    tail = _legacy_tail(value, origin)
    if tail is None:
        return None
    parts = tail.split("/")
    if len(parts) == 2 and parts[0] == "actors":
        return parts[1]
    if len(parts) == 3 and parts[0] == "actors" and parts[1] == "users":
        return parts[2]
    return None
