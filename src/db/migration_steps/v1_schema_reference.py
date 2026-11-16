"""Recreate the immutable v1 physical DDL during legacy v0 transfers.

The live ORM now models v2 and therefore cannot be used to produce a v1
intermediate. This helper reads the frozen v1 snapshot, never the v2 ORM,
so the existing historical transfer still produces the exact admitted v1 DDL.
"""

from __future__ import annotations

from pathlib import Path
from contextlib import closing
import sqlite3

from sqlalchemy.engine import Connection


def original_v1_entries() -> list[tuple[str, str, str, str]]:
    """Return physical DDL from the independently frozen SQLite v1 fixture."""
    path = Path(__file__).resolve().parent.parent / "schema_versions" / "v1.sql"
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.executescript(path.read_text(encoding="utf-8"))
        return list(connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' AND sql IS NOT NULL "
            "ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'index' THEN 1 ELSE 2 END, name"
        ))


def original_v1_columns(name: str) -> tuple[str, ...]:
    """Return frozen target columns for a historically rebuilt table."""
    path = Path(__file__).resolve().parent.parent / "schema_versions" / "v1.sql"
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.executescript(path.read_text(encoding="utf-8"))
        return tuple(row[1] for row in connection.execute(f'PRAGMA table_info("{name}")'))


def create_missing_v1_objects(conn: Connection, *, include_triggers: bool = False, include_indexes: bool = True) -> None:
    """Create absent v1 tables/indexes with byte-identical snapshot SQL.

    Historical v0 rows are retained in place. Rebuilt tables are recreated
    separately so their original data are copied before obsolete DDL is lost.
    """
    existing = {row[0] for row in conn.exec_driver_sql(
        "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
    )}
    for kind, name, _table, statement in original_v1_entries():
        if (kind == "trigger" and not include_triggers) or (kind == "index" and not include_indexes):
            continue
        if name not in existing:
            conn.exec_driver_sql(statement)
            existing.add(name)
