"""Atomic v1→v2 SQLite migration for user-owned remote Person feeds.

SQLite cannot ALTER existing CHECK constraints. Both local publication tables
are rebuilt while FK enforcement is temporarily suspended by the *caller*
and all other row identities and external FK declarations remain unchanged.
"""

from __future__ import annotations

from sqlalchemy.engine import Connection

from ...models import Base

_REBUILT_TABLES = ("message_mappings", "published_activity_objects")
_REMOTE_TABLES = (
    "remote_person_follows", "remote_person_channel_assignments",
    "remote_person_posts", "remote_person_post_deliveries",
    "remote_person_comment_placements",
)


def _rebuild_check_constrained_table(conn: Connection, name: str) -> None:
    """Recreate exact v2 DDL and copy the original columns, PKs and values."""
    legacy = f"_v1_{name}"
    conn.exec_driver_sql(f"ALTER TABLE {name} RENAME TO {legacy}")
    # Named indexes on the renamed table otherwise occupy their old names.
    for (index_name,) in conn.exec_driver_sql(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=? "
        "AND sql IS NOT NULL", (legacy,)
    ).all():
        conn.exec_driver_sql(f'DROP INDEX "{index_name}"')

    target = Base.metadata.tables[name]
    target.create(conn)
    fields = tuple(column.name for column in target.columns)
    source_fields = {
        row[1] for row in conn.exec_driver_sql(f'PRAGMA table_info("{legacy}")')
    }
    if set(fields) != source_fields:
        raise RuntimeError(f"v1→v2 incompatible columns for {name}")
    names = ", ".join(f'"{field}"' for field in fields)
    conn.exec_driver_sql(
        f"INSERT INTO {name} ({names}) SELECT {names} FROM {legacy}"
    )
    conn.exec_driver_sql(f"DROP TABLE {legacy}")


def upgrade_v1_to_v2(conn: Connection, _profile: str, _config: object) -> None:
    """Extend constrained publication identities and create five domain tables.

    The migration owner guarantees BEGIN IMMEDIATE and foreign_keys=OFF.
    Any SQL error rolls the entire transaction back before version stamping.
    """
    if conn.exec_driver_sql("PRAGMA foreign_keys").scalar_one() != 0:
        raise RuntimeError("v1→v2 constrained rebuild requires FK suspension")
    try:
        conn.exec_driver_sql("PRAGMA legacy_alter_table=ON")
        for name in _REBUILT_TABLES:
            _rebuild_check_constrained_table(conn, name)
    finally:
        conn.exec_driver_sql("PRAGMA legacy_alter_table=OFF")
    # Newly added tables have no old rows and are created only after the
    # referenced constrained tables have been restored under their old names.
    Base.metadata.create_all(
        conn, tables=[Base.metadata.tables[name] for name in _REMOTE_TABLES]
    )
