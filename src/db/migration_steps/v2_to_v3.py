"""SQLite v2→v3: retain canonical Group owners and typed target claims.

The four existing tables are rebuilt under the migration owner's single writer
transaction so their exact physical DDL equals fresh ORM-created v3. Old v1/v2
snapshots remain byte-for-byte frozen and old IDs/claims survive unchanged.
"""

from __future__ import annotations

import json

from sqlalchemy.engine import Connection

from ...models import Base

_CHANGED = (
    "activitypub_event_receipts",
    "community_thread_groups",
    "community_message_groups",
    "community_discord_target_claims",
)


def _rebuild_additive_columns(conn: Connection, name: str) -> None:
    """Add nullable/defaulted view and cursor fields without changing legacy IDs.

    A physical table rebuild, rather than ALTER ADD COLUMN, makes exact schema
    inspection independent of whether the database began as v2 or was created
    directly from v3 metadata. FK suspension is owned by the migration runner.
    """
    previous = f"_v2_{name}"
    conn.exec_driver_sql(f'ALTER TABLE "{name}" RENAME TO "{previous}"')
    # Explicit indexes keep their original names after RENAME, so release
    # those names before the current ORM emits the exact target DDL.
    indexes = conn.exec_driver_sql(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",
        (previous,),
    )
    index_names = [name for (name,) in indexes.all()]
    indexes.close()
    for index_name in index_names:
        conn.exec_driver_sql(f'DROP INDEX "{index_name}"')
    table = Base.metadata.tables[name]
    table.create(conn)
    old_columns = {
        row[1] for row in conn.exec_driver_sql(f'PRAGMA table_info("{previous}")')
    }
    new_columns = {column.name for column in table.columns}
    if not old_columns.issubset(new_columns):
        raise RuntimeError(f"v2→v3 would discard a column in {name}")
    fields = ", ".join(f'"{column.name}"' for column in table.columns if column.name in old_columns)
    conn.exec_driver_sql(f'INSERT INTO "{name}" ({fields}) SELECT {fields} FROM "{previous}"')
    conn.exec_driver_sql(f'DROP TABLE "{previous}"')


def _seed_verified_original_views(conn: Connection, *, kind: str) -> None:
    """Use only retained first-Create provenance to seed remote Group displays.

    Legacy local-source rows and unproven snapshots have no Group mutation
    authority. They remain without a mutable display projection in v3.
    """
    if kind == "post":
        table, condition = "community_thread_groups", "source_channel_id IS NULL AND source_thread_id IS NULL"
    else:
        table, condition = "community_message_groups", "source_channel_id IS NULL AND source_message_id IS NULL"
    records = conn.exec_driver_sql(
        f"SELECT id,ap_object_id,source_owner_actor_id,inbound_create_snapshot_json "
        f"FROM {table} WHERE {condition} AND inbound_create_snapshot_json IS NOT NULL"
    ).all()
    for group_id, object_id, owner, serialized in records:
        try:
            original = json.loads(serialized)
        except (TypeError, ValueError):
            continue
        if (not owner or not isinstance(original, dict)
                or original.get("ap_id") != object_id or original.get("kind") != kind):
            continue
        # Copy the original JSON verbatim; it is source data, never a new
        # synthesized ActivityPub Note. Group-only edits can later replace the
        # display fields while leaving the immutable original untouched.
        conn.exec_driver_sql(
            f"UPDATE {table} SET display_object_json=? WHERE id=?",
            (serialized, group_id),
        )


def _backfill_proven_community_deliveries(conn: Connection, *, kind: str) -> None:
    """Adopt historical bot-owned inbound Discord IDs, never unknown attempts.

    A source/mirror delivery is insufficient proof of an inbound Group POST.
    Existing claimed/unknown attempts remain untouched even if older content
    lacks a reconciliation marker; they require explicit manual resolution.
    """
    if kind == "post":
        group, delivery, fk = "community_thread_groups", "community_thread_group_deliveries", "thread_group_id"
        condition = "g.source_channel_id IS NULL AND g.source_thread_id IS NULL"
    else:
        group, delivery, fk = "community_message_groups", "community_message_group_deliveries", "message_group_id"
        condition = "g.source_channel_id IS NULL AND g.source_message_id IS NULL"
    rows = conn.exec_driver_sql(
        f"SELECT d.{fk}, d.discord_channel_id FROM {delivery} d "
        f"JOIN {group} g ON g.id=d.{fk} "
        f"LEFT JOIN community_discord_target_claims c ON "
        f"c.{fk}=d.{fk} AND c.discord_channel_id=d.discord_channel_id "
        f"WHERE d.role='inbound' AND {condition} AND c.id IS NULL "
        f"AND g.display_object_json IS NOT NULL"
    ).all()
    for group_id, channel_id in rows:
        conn.exec_driver_sql(
            "INSERT INTO community_discord_target_claims "
            "(thread_group_id,message_group_id,discord_channel_id,kind,status,"
            " reserved_view_revision,applied_revision) VALUES (?,?,?,?,?,?,?)",
            (group_id if kind == "post" else None,
             group_id if kind == "comment" else None,
             channel_id, kind, "completed", 0, 0),
        )


def upgrade_v2_to_v3(conn: Connection, _profile: str, _config: object) -> None:
    """Build exact v3 DDL while preserving provenance and ambiguous claims.

    The caller holds BEGIN IMMEDIATE with FK checking temporarily suspended;
    the migration owner validates the resulting snapshot, integrity and all
    foreign keys before stamping user_version=3 atomically.
    """
    if conn.exec_driver_sql("PRAGMA foreign_keys").scalar_one() != 0:
        raise RuntimeError("v2→v3 table rebuild requires FK suspension")
    try:
        conn.exec_driver_sql("PRAGMA legacy_alter_table=ON")
        for table in _CHANGED:
            _rebuild_additive_columns(conn, table)
    finally:
        conn.exec_driver_sql("PRAGMA legacy_alter_table=OFF")
    _seed_verified_original_views(conn, kind="post")
    _seed_verified_original_views(conn, kind="comment")
    _backfill_proven_community_deliveries(conn, kind="post")
    _backfill_proven_community_deliveries(conn, kind="comment")
