"""Additive and compatibility schema migration helpers for bridge SQLite DBs.

Database owns the engine and calls this module from `Database.migrate()`. The
helpers preserve the previous additive migration behavior and include one narrow
SQLite table-rebuild migration for the local-community summary nullability
change, because SQLite cannot drop a NOT NULL constraint in place.
"""

from __future__ import annotations

from dataclasses import dataclass
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
    build_user_post_object_id,
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

    _migrate_stage4_published_objects(conn, origin, user_actor_map, community_actor_map)
    _migrate_stage4_message_mappings(conn, origin, user_actor_map, community_actor_map)
    _migrate_stage4_local_community_content(conn, origin, community_slug_by_id)
    _migrate_stage4_profile_update_sources(conn, origin)
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
) -> None:
    """Rewrite retained published object rows to typed actor/object/activity IDs."""
    if not _table_columns(conn, "published_activity_objects"):
        return
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
) -> None:
    """Rewrite canonical local-community rows while preserving surface rows."""
    thread_object_map: dict[str, str] = {}
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
            conn.execute(text("UPDATE local_community_messages SET ap_object_id=:object_id, ap_activity_id=:activity_id WHERE id=:id"), {"id": row["id"], "object_id": new_object, "activity_id": build_community_create_comment_activity_id(origin=origin, slug=slug, activity_id=activity_token)})
        for old, new in {**thread_object_map, **parent_map}.items():
            conn.execute(
                text(
                    "UPDATE local_community_messages SET parent_ap_object_id=:new "
                    "WHERE parent_ap_object_id=:old"
                ),
                {"old": old, "new": new},
            )


def _migrate_stage4_profile_update_sources(conn: Connection, origin: str) -> None:
    """Migrate retained profile Update activity ids to the typed user grammar."""
    source_columns = _table_columns(conn, "user_actor_outbound_sources")
    if not source_columns or not _table_columns(conn, "users"):
        return
    rows = conn.execute(text("SELECT s.id, s.user_id, s.source_kind, s.logical_version, u.activitypub_username FROM user_actor_outbound_sources s JOIN users u ON u.id=s.user_id")).mappings()
    for row in rows:
        if row["source_kind"] != "profile_update":
            continue
        activity_id = build_user_profile_update_activity_id(origin=origin, username=str(row["activitypub_username"]), profile_version_or_activity_id=row["logical_version"])
        conn.execute(text("UPDATE user_actor_outbound_sources SET activity_id=:activity_id WHERE id=:id"), {"id": row["id"], "activity_id": activity_id})


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
        ("user_actor_outbound_sources", ("activity_id",)),
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
                value = row[column]
                if not isinstance(value, str) or not _is_same_origin_url(value, origin):
                    continue
                path = urlsplit(value).path
                allowed = path.startswith("/actors/users/") or path.startswith("/actors/communities/") or path.startswith("/actors/services/")
                if not allowed and any(path.startswith(prefix) for prefix in retired):
                    raise RuntimeError(f"Stage 4 migration left retired local URL in {table}.{column} id={row['id']}: {value}")
                if path.startswith("/actors/") and not allowed:
                    raise RuntimeError(f"Stage 4 migration left untyped actor URL in {table}.{column} id={row['id']}: {value}")


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
