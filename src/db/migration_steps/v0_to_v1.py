"""Explicit release-v0 table/data copy before the final v1 invariant additions.

The tagged release and known-main schema differ by three rebuilt tables and
thirteen new tables. This historical transfer keeps source IDs and bytes in
SQLite, without loading user data or media into an unbounded Python buffer.
The later CP05 step adds v1 domain columns, keys, indexes and admission.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ...models import Base
from ..migrations import _install_profile_media_triggers


_REBUILT_COLUMNS = {
    "users": (
        "id", "discord_user_id", "activitypub_username", "actor_url", "inbox_url",
        "outbox_url", "followers_url", "public_key_pem", "private_key_pem",
        "created_at", "updated_at",
    ),
    "message_mappings": (
        "id", "source_platform", "source_id", "activity_id", "object_id", "actor_url",
        "community_actor_url", "discord_channel_id", "discord_message_id", "created_at",
    ),
    "published_activity_objects": (
        "id", "actor_username", "actor_url", "activity_id", "object_id", "kind",
        "title", "body_markdown", "in_reply_to_object_id", "community_actor_url",
        "discord_channel_id", "discord_message_id", "published_at", "created_at",
    ),
}


def transfer_released_v0_tables(conn: Connection) -> None:
    """Rebuild only three evolved tables and create 13 new, preserving all source rows.

    The caller must hold SQLite BEGIN IMMEDIATE with FK OFF. ``legacy_alter_table``
    prevents SQLite ALTER RENAME from rewriting foreign keys on untouched tables.
    Both settings are verified before any structural mutation.
    """
    if conn.exec_driver_sql("PRAGMA foreign_keys").scalar_one() != 0:
        raise RuntimeError("release transfer requires foreign_keys=OFF before BEGIN")
    if conn.exec_driver_sql("PRAGMA user_version").scalar_one() != 0:
        raise RuntimeError("release transfer requires legacy SQLite user_version=0")
    for table in _REBUILT_COLUMNS:
        original = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})").all()}
        if not set(_REBUILT_COLUMNS[table]).issubset(original):
            raise RuntimeError(f"unsupported release-v0 columns for {table}")

    try:
        conn.exec_driver_sql("PRAGMA legacy_alter_table=ON")
        for table in _REBUILT_COLUMNS:
            conn.exec_driver_sql(f"ALTER TABLE {table} RENAME TO _release_v0_{table}")
        conn.exec_driver_sql("PRAGMA legacy_alter_table=OFF")
        # New main ORM is valid historical transition target; CP05 upgrades it
        # further to the frozen v1 schema before the runner may stamp v1.
        Base.metadata.create_all(conn)
        conn.execute(text("""
            INSERT INTO users (id,discord_user_id,activitypub_username,actor_url,
                inbox_url,outbox_url,followers_url,public_key_pem,private_key_pem,
                profile_version,created_at,updated_at)
            SELECT id,discord_user_id,activitypub_username,actor_url,
                inbox_url,outbox_url,followers_url,public_key_pem,private_key_pem,
                0,created_at,updated_at FROM _release_v0_users
        """))
        conn.execute(text("""
            INSERT INTO message_mappings (id,source_platform,source_id,activity_id,
                object_id,actor_url,publication_context,community_actor_url,
                discord_channel_id,discord_message_id,created_at)
            SELECT id,source_platform,source_id,activity_id,object_id,actor_url,
                'community',community_actor_url,discord_channel_id,
                discord_message_id,created_at FROM _release_v0_message_mappings
        """))
        conn.execute(text("""
            INSERT INTO published_activity_objects (id,actor_username,actor_url,
                publication_context,community_actor_url,activity_id,object_id,kind,
                title,body_markdown,in_reply_to_object_id,discord_channel_id,
                discord_message_id,content_revision,conversation_root_object_id,
                desired_kind,discord_reconciliation_needed,
                discord_reconciliation_attempt_count,published_at,created_at)
            SELECT id,actor_username,actor_url,'community',community_actor_url,
                activity_id,object_id,kind,title,body_markdown,in_reply_to_object_id,
                discord_channel_id,discord_message_id,1,
                CASE WHEN kind='post' THEN object_id ELSE COALESCE(in_reply_to_object_id,object_id) END,
                'none',0,0,published_at,created_at
            FROM _release_v0_published_activity_objects
        """))
        for table in reversed(tuple(_REBUILT_COLUMNS)):
            conn.exec_driver_sql(f"DROP TABLE _release_v0_{table}")
        _install_profile_media_triggers(conn)
    finally:
        # This is connection-local and does not alter the surrounding
        # transaction or toggle foreign_keys during BEGIN.
        conn.exec_driver_sql("PRAGMA legacy_alter_table=OFF")


# The only tables that need a physical rebuild rather than an additive ALTER:
# global uniqueness must become tenant-scoped, or new CHECK/FK constraints must
# be enforced by SQLite itself. New columns on the other existing tables are
# added without replacing their original source data/PKs.
_V1_REBUILDS = (
    "activitypub_event_receipts",
    "bridge_actor_follows",
    "community_thread_groups",
    "community_message_groups",
    "local_community_relay_source_activities",
    "local_community_relay_deliveries",
    "local_community_threads",
    "local_community_messages",
    # The direct Group delivery path needs a durable unresolved outcome; a
    # known-main CHECK constraint must be rebuilt to admit that state.
    "user_actor_outbound_deliveries",
)

_V1_NEW_VALUES = {
    "bridge_actor_follows": {
        # Network discovery is not a migration proof of remote software.
        "remote_software_profile": "'unknown'",
        # An existing accepted Follow was historically submitted to transport.
        "follow_dispatch_state": (
            "CASE WHEN follow_activity_id IS NULL THEN 'prepared' "
            "WHEN status='accepted' THEN 'sent' ELSE 'sending' END"
        ),
        "cancel_requested": "0",
    },
    "local_community_messages": {
        "local_community_id": "(SELECT local_community_id FROM local_community_threads "
                              "WHERE id = _v0_local_community_messages.local_community_thread_id)",
    },
}


def preflight_v1_collisions(conn: Connection) -> None:
    """Reject ambiguous historical canonical identities before any data/DDL write.

    All predicates use original v0 source fields, not migrated or guessed owner
    attributes. SQLite retains the entire source if one conflict is detected.
    """
    checks = (
        ("duplicate remote thread group", """
            SELECT 1 FROM community_thread_groups
            WHERE ap_object_id IS NOT NULL AND source_channel_id IS NULL AND source_thread_id IS NULL
            GROUP BY community_actor_id,ap_object_id HAVING count(*) > 1 LIMIT 1
        """),
        ("duplicate remote message group", """
            SELECT 1 FROM community_message_groups
            WHERE ap_object_id IS NOT NULL AND source_channel_id IS NULL AND source_message_id IS NULL
            GROUP BY community_actor_id,ap_object_id HAVING count(*) > 1 LIMIT 1
        """),
        ("duplicate Discord reply source", """
            SELECT 1 FROM community_message_groups WHERE source_message_id IS NOT NULL
            GROUP BY source_message_id HAVING count(*) > 1 LIMIT 1
        """),
        ("duplicate local relay Create", """
            SELECT 1 FROM local_community_relay_source_activities WHERE operation='create'
            GROUP BY local_community_id,source_object_ap_id HAVING count(*) > 1 LIMIT 1
        """),
        ("duplicate Follow generation", """
            SELECT 1 FROM bridge_actor_follows WHERE follow_activity_id IS NOT NULL
            GROUP BY follow_activity_id HAVING count(*) > 1 LIMIT 1
        """),
        ("local message has missing canonical parent", """
            SELECT 1 FROM local_community_messages m LEFT JOIN local_community_threads t
            ON t.id=m.local_community_thread_id WHERE t.id IS NULL LIMIT 1
        """),
        ("remote reply belongs to a different Group", """
            SELECT 1 FROM community_message_groups m
            LEFT JOIN community_thread_groups t ON t.id=m.thread_group_id
            WHERE t.id IS NULL OR t.community_actor_id != m.community_actor_id LIMIT 1
        """),
        ("remote reply parent belongs to another Group or root", """
            SELECT 1 FROM community_message_groups m
            JOIN community_message_groups p ON p.id=m.parent_message_group_id
            WHERE m.community_actor_id!=p.community_actor_id
               OR m.thread_group_id!=p.thread_group_id LIMIT 1
        """),
        ("local reply parent belongs to another tenant or thread", """
            SELECT 1 FROM local_community_messages m
            JOIN local_community_threads t ON t.id=m.local_community_thread_id
            WHERE m.parent_ap_object_id IS NOT NULL
              AND m.parent_ap_object_id != t.ap_object_id
              AND EXISTS(SELECT 1 FROM local_community_messages p
                WHERE p.ap_object_id=m.parent_ap_object_id
                  AND p.local_community_thread_id!=m.local_community_thread_id)
            LIMIT 1
        """),
        ("local thread has duplicate tenant object", """
            SELECT 1 FROM local_community_threads
            GROUP BY local_community_id,ap_object_id HAVING count(*)>1 LIMIT 1
        """),
        ("local message has duplicate tenant object", """
            SELECT 1 FROM local_community_messages m
            JOIN local_community_threads t ON t.id=m.local_community_thread_id
            GROUP BY t.local_community_id,m.ap_object_id HAVING count(*)>1 LIMIT 1
        """),
        ("local message surface parent mismatch", """
            SELECT 1 FROM local_community_message_surfaces s
            JOIN local_community_messages m ON m.id=s.local_community_message_id
            JOIN local_community_thread_surfaces t ON t.id=s.local_community_thread_surface_id
            WHERE t.local_community_thread_id!=m.local_community_thread_id LIMIT 1
        """),
        ("conflicting generic Discord echo provenance", """
            SELECT 1 FROM message_mappings mapping
            WHERE mapping.source_platform='discord' AND (
                EXISTS(SELECT 1 FROM local_community_threads t
                    WHERE t.ap_object_id=mapping.object_id AND t.direction='ap_to_discord')
                OR EXISTS(SELECT 1 FROM local_community_messages m
                    WHERE m.ap_object_id=mapping.object_id AND m.direction='ap_to_discord')
            ) LIMIT 1
        """),
    )
    for reason, query in checks:
        if conn.exec_driver_sql(query).first() is not None:
            raise RuntimeError(f"Cannot upgrade SQLite v0: {reason}; inspect verified backup")


def _rebuild_v1_table(conn: Connection, table_name: str) -> None:
    """Rebuild one old table using exact v1 ORM DDL, preserving every v0 column.

    The writer already owns BEGIN IMMEDIATE, foreign_keys=OFF; the old table
    stays available under a temporary name until rows have been copied into
    the target constraints. Any mismatch raises, rolling back both versions.
    """
    legacy = f"_v0_{table_name}"
    conn.exec_driver_sql(f"ALTER TABLE {table_name} RENAME TO {legacy}")
    # Explicit indexes keep global names after SQLite table rename. Drop only
    # indexes owned by this temporary table before target.create recreates
    # the exact v1 indexes. SQLite's autoindexes remain owned by the source.
    explicit_indexes = list(conn.exec_driver_sql(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=? "
        "AND sql IS NOT NULL", (legacy,)
    ))
    for (index_name,) in explicit_indexes:
        conn.exec_driver_sql(f'DROP INDEX "{index_name}"')
    target = Base.metadata.tables[table_name]
    target.create(conn)
    old_columns = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({legacy})")}
    names = [column.name for column in target.columns]
    expressions = []
    for name in names:
        if name in old_columns:
            expressions.append(f'"{name}"')
        else:
            expressions.append(_V1_NEW_VALUES.get(table_name, {}).get(name, "NULL"))
    columns = ", ".join(f'"{name}"' for name in names)
    select = ", ".join(expressions)
    conn.exec_driver_sql(
        f"INSERT INTO {table_name} ({columns}) SELECT {select} FROM {legacy}"
    )
    conn.exec_driver_sql(f"DROP TABLE {legacy}")


def upgrade_v0_to_v1(
    conn: Connection, source_profile: str, config: object
) -> None:
    """Upgrade one admitted release/main v0 to the complete v1 physical schema.

    This operation is valid only inside the migration owner's transaction.
    It refuses both mixed identity evidence and old canonical collisions;
    no remote actor/person is fabricated from the latest incoming envelope.
    """
    from ..main_v0_admission import inspect_known_main_identities
    from ..migrations import _migrate_stage4_typed_activitypub_identity

    if source_profile not in ("released_v0", "known_main_v0"):
        raise RuntimeError("unsupported historical SQLite source profile")
    if conn.exec_driver_sql("PRAGMA foreign_keys").scalar_one() != 0:
        raise RuntimeError("historical source rebuild requires foreign_keys=OFF before BEGIN")
    preflight_v1_collisions(conn)
    # Identity evidence is examined before the first structural change.
    evidence = inspect_known_main_identities(conn, config)
    if source_profile == "released_v0":
        transfer_released_v0_tables(conn)
    if evidence.state == "legacy":
        _migrate_stage4_typed_activitypub_identity(conn, config)

    # SQLite has no DROP CONSTRAINT for the former global AP UNIQUE keys.
    # legacy_alter_table avoids rebinding existing external references to the
    # temporary table; after the copy both FK targets retain their old names.
    try:
        conn.exec_driver_sql("PRAGMA legacy_alter_table=ON")
        for table in _V1_REBUILDS:
            _rebuild_v1_table(conn, table)
    finally:
        conn.exec_driver_sql("PRAGMA legacy_alter_table=OFF")

    # `released_v0` copied new target tables during main compatibility transfer;
    # the known-main source needs the one concrete FK-owned claim table here.
    Base.metadata.create_all(conn)
    # Preserve source writer's profile-media checks, independently of whether
    # its profile-media table existed at v0 release time.
    _install_profile_media_triggers(conn)
