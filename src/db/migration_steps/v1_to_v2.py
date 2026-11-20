"""Atomic v1→v2 migration for remote Person feeds and Community Group views.

Existing publication and Group tables are rebuilt to current ORM DDL with
source IDs preserved under the migration runner's single writer transaction.
"""

from __future__ import annotations

import json

from sqlalchemy.engine import Connection

from ...models import Base

_REBUILT_TABLES = ("message_mappings", "published_activity_objects")
_GROUP_VIEW_TABLES = (
    "activitypub_event_receipts",
    "community_thread_groups",
    "community_message_groups",
    "community_discord_target_claims",
)
_REMOTE_TABLES = (
    "remote_person_follows", "remote_person_channel_assignments",
    "remote_person_posts", "remote_person_post_deliveries",
    "remote_person_comment_placements",
)


def _rebuild_table(
        conn: Connection, name: str, *, allow_added_columns: bool = False) -> None:
    """Rebuild one v1 table using exact current v2 ORM DDL, preserving source IDs.

    Constrained publication tables require an identical set of columns.
    Group/receipt/claim tables may gain columns with declared v2 defaults or
    NULL values; every legacy column still has to survive. The migration
    runner owns FK suspension, and the caller owns legacy_alter_table mode.
    """
    previous = f"_v1_{name}"
    conn.exec_driver_sql(f'ALTER TABLE "{name}" RENAME TO "{previous}"')
    # Renaming leaves explicit index names occupied by the old table. Free
    # them before SQLAlchemy creates the new table with its canonical indexes.
    indexes = conn.exec_driver_sql(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=? "
        "AND sql IS NOT NULL", (previous,),
    )
    index_names = [index_name for (index_name,) in indexes.all()]
    indexes.close()
    for index_name in index_names:
        conn.exec_driver_sql(f'DROP INDEX "{index_name}"')

    target = Base.metadata.tables[name]
    target.create(conn)
    old_columns = {
        row[1] for row in conn.exec_driver_sql(f'PRAGMA table_info("{previous}")')
    }
    new_columns = tuple(column.name for column in target.columns)
    if allow_added_columns:
        if not old_columns.issubset(new_columns):
            raise RuntimeError(f"v1→v2 would discard a column in {name}")
        copied = tuple(field for field in new_columns if field in old_columns)
    else:
        if set(new_columns) != old_columns:
            raise RuntimeError(f"v1→v2 incompatible columns for {name}")
        copied = new_columns
    # Explicitly copy only proven v1 fields, letting the v2 DDL initialize
    # newly added Group-view columns. Do not synthesize provenance or IDs.
    fields = ", ".join(f'"{field}"' for field in copied)
    conn.exec_driver_sql(
        f'INSERT INTO "{name}" ({fields}) SELECT {fields} FROM "{previous}"'
    )
    conn.exec_driver_sql(f'DROP TABLE "{previous}"')


def _seed_verified_original_views(conn: Connection, *, kind: str) -> None:
    """Use only retained first-Create provenance to seed remote Group displays.

    Legacy local-source rows and unproven snapshots have no Group mutation
    authority. They remain without a mutable display projection in updated v2.
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



def _seed_completed_claim_cursors(conn: Connection, *, kind: str) -> None:
    """Adopt a known Discord ID for a preexisting completed inbound claim.

    Pre-v2 claims have no revision cursor; a completed claim with its own
    matching inbound delivery proves revision zero was already materialized.
    No such inference is made for claimed/unknown/failed attempts.
    """
    if kind == "post":
        group, delivery, fk = "community_thread_groups", "community_thread_group_deliveries", "thread_group_id"
        condition = "g.source_channel_id IS NULL AND g.source_thread_id IS NULL"
    else:
        group, delivery, fk = "community_message_groups", "community_message_group_deliveries", "message_group_id"
        condition = "g.source_channel_id IS NULL AND g.source_message_id IS NULL"
    # A saved typed inbound delivery is the authority for a completed
    # create; a bare status flag is not sufficient after a lost ACK.
    conn.exec_driver_sql(
        "UPDATE community_discord_target_claims SET applied_revision=0, "
        "reserved_view_revision=0 WHERE status='completed' AND applied_revision IS NULL "
        f"AND EXISTS (SELECT 1 FROM {delivery} d "
        f"JOIN {group} g ON g.id=d.{fk} "
        f"WHERE d.{fk}=community_discord_target_claims.{fk} "
        "AND d.discord_channel_id=community_discord_target_claims.discord_channel_id "
        f"AND d.role='inbound' AND {condition} "
        "AND g.display_object_json IS NOT NULL)"
    )


def upgrade_v1_to_v2(conn: Connection, _profile: str, _config: object) -> None:
    """Create the full updated v2 schema while retaining source rows and claims.

    The migration owner guarantees BEGIN IMMEDIATE and foreign_keys=OFF.
    Any SQL error rolls the entire transaction back before version stamping.
    """
    if conn.exec_driver_sql("PRAGMA foreign_keys").scalar_one() != 0:
        raise RuntimeError("v1→v2 constrained rebuild requires FK suspension")
    try:
        conn.exec_driver_sql("PRAGMA legacy_alter_table=ON")
        for name in _REBUILT_TABLES:
            _rebuild_table(conn, name)
    finally:
        conn.exec_driver_sql("PRAGMA legacy_alter_table=OFF")
    # Community Group and claim rows predate remote Person feeds. Rebuild
    # them within the *same* v1→v2 transaction, retaining all original keys.
    for name in _GROUP_VIEW_TABLES:
        _rebuild_table(conn, name, allow_added_columns=True)
    # Newly added tables have no old rows and are created only after the
    # referenced constrained tables have been restored under their old names.
    Base.metadata.create_all(
        conn, tables=[Base.metadata.tables[name] for name in _REMOTE_TABLES]
    )

    # Backfill only immutable proven Create snapshots. Existing inbound
    # completed claims with a saved Discord ID also need a revision cursor.
    for kind in ("post", "comment"):
        _seed_verified_original_views(conn, kind=kind)
        _seed_completed_claim_cursors(conn, kind=kind)
        _backfill_proven_community_deliveries(conn, kind=kind)
