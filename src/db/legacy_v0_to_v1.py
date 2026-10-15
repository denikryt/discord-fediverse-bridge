"""Direct released-v0 to ORM-v1 conversion; no intermediate business schema.

The caller owns the transaction, FK suspension, header and commit. Historical
SQL is loaded only in a disposable reference database. Live transfer uses native
SQL staging/copy; Python evidence is limited to identity/relationship metadata.
"""

from __future__ import annotations
from contextlib import contextmanager
from datetime import datetime, timezone
from importlib.resources import files
from sqlalchemy import create_engine
from sqlalchemy.engine import Connection
from ..models import Base
from .legacy_identity_cutover import (
    IDENTITY_FIELDS,
    plan_identity_cutover,
    apply_identity_cutover,
    validate_current_identity,
)
from .legacy_v0_sql import COPY_SQL, RELATIONSHIP_CHECKS, CHANNEL_INVENTORY


@contextmanager
def release_reference():
    """Read the pinned SQL fact in a separate disposable database, never on the live connection."""
    engine = create_engine("sqlite://")
    try:
        with engine.connect() as reference:
            # executescript is safe here only: this engine contains no application data/transaction.
            reference.connection.driver_connection.executescript(
                files("src.db").joinpath("legacy_v0.sql").read_text()
            )
            yield reference
    finally:
        engine.dispose()


def release_unique_columns():
    """Expose pinned uniqueness for identity collision planning without a second historical manifest."""
    from . import migrations

    with release_reference() as reference:
        return {
            name: tuple(
                tuple(
                    row[2]
                    for row in reference.exec_driver_sql(
                        f'PRAGMA index_info("{index[1]}")'
                    )
                )
                for index in reference.exec_driver_sql(f'PRAGMA index_list("{name}")')
                if index[2]
            )
            for name in migrations._user_tables(reference)
        }


def capture_source_evidence(conn):
    """Read only identity and relationship metadata, omitting key material, bodies and timestamps."""
    result = {}
    unique = release_unique_columns()
    for name in sorted(unique):
        fields = {r[1] for r in conn.exec_driver_sql(f'PRAGMA table_info("{name}")')}
        if not fields & IDENTITY_FIELDS:
            # Untouched audit/policy/cache data stays in SQL staging, not duplicate Python rows.
            result[name] = []
            continue
        needed = {
            "id",
            "slug",
            "activitypub_username",
            "kind",
            "object_kind",
            "source_platform",
            "status",
            "channel_type",
            "source_activity_json",
        } | IDENTITY_FIELDS
        needed |= {field for group in unique[name] for field in group}
        columns = sorted(
            field for field in fields if field in needed or field.endswith("_id")
        )
        result[name] = [
            dict(r)
            for r in conn.exec_driver_sql(
                f'SELECT {",".join(columns)} FROM main."{name}" ORDER BY id'
            ).mappings()
        ]
    return result


def _validate_physical_identifiers(conn, names):
    """Validate SQLite values before numeric comparisons or CAST-based channel joins."""
    for name in names:
        info = conn.exec_driver_sql(f'PRAGMA main.table_info("{name}")').all()
        for row in info:
            field = row[1]
            physical = field.startswith("discord_") or field in {
                "source_channel_id",
                "source_thread_id",
                "source_message_id",
                "source_starter_message_id",
                "destination_channel_id",
                "destination_thread_id",
                "root_discord_channel_id",
                "root_discord_message_id",
                "thread_id",
                "parent_discord_message_id",
            }
            if row[2] in {"INT", "INTEGER"} and physical:
                # Affinity permits REAL/TEXT in integer columns; do not round or reinterpret snowflakes.
                bad = conn.exec_driver_sql(
                    f'SELECT id FROM main."{name}" WHERE "{field}" IS NOT NULL AND (typeof("{field}") != \'integer\' OR "{field}" <= 0) LIMIT 1'
                ).first()
                if bad:
                    raise RuntimeError(
                        f"Invalid legacy Discord location: {name} row {bad[0]} column {field}"
                    )


def validate_released_v0(conn):
    """Reject unknown schema/data before staging; return narrow immutable protocol evidence."""
    from . import migrations

    if conn.exec_driver_sql("PRAGMA user_version").scalar_one() != 0:
        raise RuntimeError("Released legacy source requires version 0")
    with release_reference() as reference:
        names = migrations._user_tables(reference)
        if (
            migrations._user_tables(conn) != names
            or conn.exec_driver_sql(
                "SELECT 1 FROM sqlite_master WHERE type='view'"
            ).first()
        ):
            raise RuntimeError("Unsupported released v0.1.0 schema inventory")
        for name in sorted(names):
            if migrations._table_manifest(conn, name) != migrations._table_manifest(
                reference, name
            ):
                raise RuntimeError(f"Unsupported released v0.1.0 schema: {name}")
    migrations._validate_sqlite_integrity(conn, "Released v0")
    _validate_physical_identifiers(conn, names)
    _validate_legacy_relationships(conn)
    return capture_source_evidence(conn)


def _validate_legacy_relationships(conn):
    """Check released non-FK links and routing identities directly before staging."""
    for message, sql in RELATIONSHIP_CHECKS.items():
        # Root traversal visits each reachable group once. After missing-parent
        # rejection, any unreachable group belongs to a cycle or its descendants.
        violation = conn.exec_driver_sql(sql).first()
        if violation:
            raise RuntimeError(f"{message}: {violation}")


# Explicit source/target transfer rules; these are SQL statements, not another target DDL.


def _stage_source(conn, names):
    """Capture every source table in transactional TEMP before removing any main table."""
    for name in names:
        conn.exec_driver_sql(
            f'CREATE TEMP TABLE "__legacy_{name}" AS SELECT * FROM main."{name}"'
        )
        if (
            conn.exec_driver_sql(
                f'SELECT count(*) FROM temp."__legacy_{name}"'
            ).scalar_one()
            != conn.exec_driver_sql(f'SELECT count(*) FROM main."{name}"').scalar_one()
        ):
            raise RuntimeError(f"Incomplete source staging: {name}")
    for name in names:
        conn.exec_driver_sql(f'DROP TABLE main."{name}"')


def _create_channel_inventory(conn):
    """Merge persisted physical location evidence using SQLite, rejecting contradictory kinds/guilds."""
    conn.exec_driver_sql(CHANNEL_INVENTORY)
    if conn.exec_driver_sql(
        "SELECT channel FROM temp.__channel_inventory WHERE guild_count>1 OR kind_count>1 LIMIT 1"
    ).first():
        raise RuntimeError("Conflicting guilds/channel kinds in historical locations")
    conn.exec_driver_sql(
        "INSERT INTO main.platform_connectors(connector_key,platform,instance_origin) VALUES('discord-primary','discord',NULL)"
    )
    # Fresh target has exactly this one connector; stable numeric channel ordering preserves surface IDs.
    conn.exec_driver_sql(
        """
        INSERT INTO main.platform_channel_surfaces(connector_id,external_channel_id,external_guild_id,kind,created_at,updated_at)
        SELECT 1, CAST(channel AS TEXT), CAST(guild AS TEXT), kind, :now, :now
        FROM temp.__channel_inventory ORDER BY channel
    """,
        {"now": datetime.now(timezone.utc).isoformat()},
    )
    conn.exec_driver_sql("""
        CREATE TEMP TABLE __thread_locations AS
        SELECT thread_id, MIN(channel_id) channel_id, COUNT(DISTINCT channel_id) count
        FROM (
            SELECT discord_thread_id thread_id, discord_forum_channel_id channel_id FROM temp.__legacy_local_community_thread_surfaces
            UNION ALL SELECT discord_thread_id, discord_channel_id FROM temp.__legacy_community_thread_group_deliveries
            UNION ALL SELECT discord_forum_thread_id, discord_forum_channel_id FROM temp.__legacy_post_links
        ) GROUP BY thread_id
    """)
    if conn.exec_driver_sql(
        "SELECT thread_id FROM temp.__thread_locations WHERE count != 1 LIMIT 1"
    ).first():
        raise RuntimeError("Ambiguous/orphan legacy thread channel")


def _copy_source_rows(conn, names):
    """Transfer preserved source columns natively, using explicit SQL for changed families."""
    for name in names:
        if name in COPY_SQL:
            conn.exec_driver_sql(COPY_SQL[name])
        else:
            source = [
                r[1]
                for r in conn.exec_driver_sql(
                    f'PRAGMA temp.table_info("__legacy_{name}")'
                )
            ]
            target = set(Base.metadata.tables[name].c.keys())
            if not set(source) <= target:
                raise RuntimeError(f"Unaccounted released columns: {name}")
            columns = ",".join('"' + column + '"' for column in source)
            conn.exec_driver_sql(
                f'INSERT INTO main."{name}" ({columns}) SELECT {columns} FROM temp."__legacy_{name}"'
            )


def _claim_channels(conn):
    """Install typed domain ownership after owners and unclaimed physical channels exist."""
    for table, column, channel in [
        ("local_communities", "local_community_id", "discord_forum_channel_id"),
        ("local_subscribers", "local_subscriber_id", "discord_channel_id"),
        (
            "channel_community_subscriptions",
            "remote_subscription_id",
            "discord_channel_id",
        ),
    ]:
        conn.exec_driver_sql(
            f'UPDATE main.platform_channel_surfaces SET "{column}"=(SELECT l.id FROM temp."__legacy_{table}" l WHERE CAST(l."{channel}" AS TEXT)=external_channel_id) WHERE connector_id=1 AND external_channel_id IN (SELECT CAST("{channel}" AS TEXT) FROM temp."__legacy_{table}")'
        )


def _assert_preserved(conn, names, original, expected):
    """Apply only approved protocol deltas to staged evidence, then compare all original columns in SQL."""
    for name, rows in original.items():
        targets = {row["id"]: row for row in expected[name]}
        for old in rows:
            new = targets.get(old["id"])
            if new is None:
                if name != "remote_subscribers":
                    raise RuntimeError("Unapproved source deletion")
                conn.exec_driver_sql(
                    "DELETE FROM temp.__legacy_remote_subscribers WHERE id=?",
                    (old["id"],),
                )
                continue
            delta = {
                field: value for field, value in new.items() if value != old[field]
            }
            if delta:
                columns = ",".join('"' + field + '"=?' for field in delta)
                conn.exec_driver_sql(
                    f'UPDATE temp."__legacy_{name}" SET {columns} WHERE id=?',
                    (*delta.values(), old["id"]),
                )
    for name in names:
        fields = [
            r[1]
            for r in conn.exec_driver_sql(f'PRAGMA temp.table_info("__legacy_{name}")')
        ]
        if name == "local_communities":
            # Actor host projections moved to a typed physical claim, not a replacement actor field.
            fields = [
                field
                for field in fields
                if field not in {"discord_guild_id", "discord_forum_channel_id"}
            ]
            if conn.exec_driver_sql(
                """SELECT l.id FROM temp.__legacy_local_communities l LEFT JOIN main.platform_channel_surfaces s ON s.local_community_id=l.id
                WHERE s.external_channel_id IS NOT CAST(l.discord_forum_channel_id AS TEXT) OR s.external_guild_id IS NOT CAST(l.discord_guild_id AS TEXT) LIMIT 1"""
            ).first():
                raise RuntimeError("Legacy host location changed")
        columns = ",".join('"' + field + '"' for field in fields)
        source = f'SELECT {columns} FROM temp."__legacy_{name}"'
        target = f'SELECT {columns} FROM main."{name}"'
        for left, right in [(source, target), (target, source)]:
            if conn.exec_driver_sql(
                f"SELECT 1 FROM ({left} EXCEPT {right}) LIMIT 1"
            ).first():
                raise RuntimeError(f"Legacy value/row changed: {name}")
    for name in (
        set(Base.metadata.tables)
        - set(names)
        - {"platform_connectors", "platform_channel_surfaces"}
    ):
        if conn.exec_driver_sql(f'SELECT count(*) FROM main."{name}"').scalar_one():
            raise RuntimeError(f"Invented legacy feature rows: {name}")


def upgrade_released_v0_to_v1(conn: Connection, identity=None):
    """Replace the released schema directly inside the runner's existing FK-suspended transaction."""
    from . import migrations, schema

    if conn.exec_driver_sql("PRAGMA foreign_keys").scalar_one() != 0:
        raise RuntimeError(
            "Direct source replacement requires pre-transaction FK suspension"
        )
    original = validate_released_v0(conn)
    expected, urls = plan_identity_cutover(original, identity)
    names = sorted(original)
    _stage_source(conn, names)
    schema.create_all(conn)
    _create_channel_inventory(conn)
    _copy_source_rows(conn, names)
    _claim_channels(conn)
    apply_identity_cutover(conn, original, expected, urls)
    migrations._install_current_schema_objects(conn)
    _assert_preserved(conn, names, original, expected)
    migrations._validate_current_v1_schema(conn)
    validate_current_identity(conn, identity)
    for name in names:
        conn.exec_driver_sql(f'DROP TABLE temp."__legacy_{name}"')
    conn.exec_driver_sql("DROP TABLE temp.__channel_inventory")
    conn.exec_driver_sql("DROP TABLE temp.__thread_locations")
