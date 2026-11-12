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
