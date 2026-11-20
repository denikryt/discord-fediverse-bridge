"""Explicit offline CP49-to-current physical SQLite v2 alignment.

Only CP49's exact v2 DDL (or the current v2 DDL) is an admitted source.
Write a separate, verified output database while leaving its source untouched.
No application startup migration, version bump, or Actor/Group identity change.
"""

from __future__ import annotations

import argparse
from contextlib import closing
import os
from pathlib import Path
import sqlite3

from src.db.backup import schema_entries_fingerprint

_GROUP_TABLES = ('community_thread_groups', 'community_message_groups')
_NEW_COLUMN_DDL = '\tview_source_updated_at DATETIME,\n'
# Fingerprint of the actual CP49 v2 snapshot. This is a narrowly authorized
# one-time dev alignment, not a general same-version schema migration.
_CP49_SCHEMA_FINGERPRINT = 'a35e97ef4666d260368941de164490ff5fb0862d7e21b9e5e1dd3410b57b2dea'
_ENTRIES = ("SELECT type,name,tbl_name,sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name,tbl_name")


def _entries(db: sqlite3.Connection) -> list[tuple[object, ...]]:
    """Return the same complete physical catalog used by database startup."""
    return db.execute(_ENTRIES).fetchall()


def _reference_schemas() -> tuple[sqlite3.Connection, sqlite3.Connection]:
    """Build strict CP49/current fingerprints from the frozen current snapshot.

    CP49 differs in exactly two nullable columns. Avoid admitting historical
    CP35 physical v2 or an arbitrary same-version DDL with different indexes.
    """
    sql = (Path(__file__).resolve().parents[1] / 'src/db/schema_versions/v2.sql').read_text()
    if sql.count(_NEW_COLUMN_DDL) != len(_GROUP_TABLES):
        raise RuntimeError('unexpected current v2 Group-view snapshot')
    previous, current = sqlite3.connect(':memory:'), sqlite3.connect(':memory:')
    try:
        previous.executescript(sql.replace(_NEW_COLUMN_DDL, ''))
        current.executescript(sql)
        if schema_entries_fingerprint(_entries(previous)) != _CP49_SCHEMA_FINGERPRINT:
            raise RuntimeError('current v2 snapshot no longer derives the approved CP49 schema')
        return previous, current
    except Exception:
        previous.close()
        current.close()
        raise


def _check_database(db: sqlite3.Connection) -> None:
    """Reject corrupt source or output before it becomes a migration candidate."""
    if db.execute('PRAGMA user_version').fetchone()[0] != 2:
        raise ValueError('only development SQLite user_version=2 is accepted')
    if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
        raise ValueError('SQLite integrity_check failed')
    if db.execute('PRAGMA foreign_key_check').fetchone() is not None:
        raise ValueError('SQLite foreign_key_check failed')


def _rebuild_group_table(db: sqlite3.Connection, reference: sqlite3.Connection,
                         table: str) -> None:
    """Replace one table with exact DDL while preserving all previous columns.

    An ALTER ADD COLUMN changes the position of this column and fails the
    project's exact sqlite_master fingerprint guard. Use a guarded rebuild,
    retaining canonical IDs and original stored data, under one transaction.
    """
    old = f'__previous_{table}'
    db.execute(f'ALTER TABLE "{table}" RENAME TO "{old}"')
    index_names = db.execute(
        'SELECT name FROM sqlite_master WHERE type="index" AND tbl_name=? AND sql IS NOT NULL',
        (old,),
    ).fetchall()
    for (index_name,) in index_names:
        db.execute(f'DROP INDEX "{index_name}"')
    ddl = reference.execute('SELECT sql FROM sqlite_master WHERE type="table" AND name=?',
                            (table,)).fetchone()[0]
    db.execute(ddl)
    # Existing fields, including JSON/provenance and integer IDs, are copied
    # as stored SQLite values; the new trusted source clock remains SQL NULL.
    cols = [row[1] for row in db.execute(f'PRAGMA table_info("{old}")')]
    if 'view_source_updated_at' in cols:
        raise ValueError('source already has the updated Group timestamp column')
    column_list = ', '.join(f'"{column}"' for column in cols)
    db.execute(f'INSERT INTO "{table}" ({column_list}) '
               f'SELECT {column_list} FROM "{old}"')
    db.execute(f'DROP TABLE "{old}"')
    # Re-create only the reference table's named explicit indexes. The table
    # primary key and its SQLite implicit indexes were created with the DDL.
    for (index_sql,) in reference.execute(
        'SELECT sql FROM sqlite_master WHERE type="index" AND tbl_name=? '
        'AND sql IS NOT NULL ORDER BY name', (table,),
    ):
        db.execute(index_sql)


def align(source: Path, output: Path) -> bool:
    """Produce an exact-v2 copy; return True when CP49 DDL was rebuilt.

    This function deliberately never writes into the live source. The caller
    must stop all application writers, retain a complete independent backup,
    and explicitly choose a new output path before deploying that output.
    """
    source, output = source.resolve(), output.resolve()
    if not source.is_file() or source == output or output.exists():
        raise ValueError('source must exist and output must be a different absent file')
    partial = output.with_name(output.name + '.partial')
    if partial.exists():
        raise ValueError(f'stale partial output must be reviewed manually: {partial}')
    previous, current = _reference_schemas()
    changed = False
    try:
        # Path.as_uri escapes question marks and hashes before SQLite URI mode.
        with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as original:
            _check_database(original)
            fingerprint = schema_entries_fingerprint(_entries(original))
            if fingerprint == schema_entries_fingerprint(_entries(previous)):
                changed = True
            elif fingerprint != schema_entries_fingerprint(_entries(current)):
                raise ValueError('source is not an exact CP49/current v2 schema')
            # SQLite's backup API copies all committed WAL changes consistently.
            with closing(sqlite3.connect(partial)) as destination:
                original.backup(destination)
        os.chmod(partial, 0o600)
        with closing(sqlite3.connect(partial)) as destination:
            if changed:
                # FK checking resumes after the atomic rebuild. The legacy
                # rename rule prevents rewriting other tables' FK references
                # to the temporarily renamed source table.
                destination.execute('PRAGMA foreign_keys=OFF')
                destination.execute('PRAGMA legacy_alter_table=ON')
                destination.execute('BEGIN IMMEDIATE')
                try:
                    for table in _GROUP_TABLES:
                        _rebuild_group_table(destination, current, table)
                    destination.execute('COMMIT')
                except Exception:
                    destination.execute('ROLLBACK')
                    raise
                finally:
                    destination.execute('PRAGMA foreign_keys=ON')
                    destination.execute('PRAGMA legacy_alter_table=OFF')
            _check_database(destination)
            if schema_entries_fingerprint(_entries(destination)) != schema_entries_fingerprint(_entries(current)):
                raise ValueError('aligned physical schema differs from current v2 snapshot')
            if changed:
                for table in _GROUP_TABLES:
                    if destination.execute(
                        f'SELECT COUNT(*) FROM "{table}" WHERE view_source_updated_at IS NOT NULL'
                    ).fetchone()[0] != 0:
                        raise ValueError('historical source timestamps must remain NULL')
        partial.replace(output)
        return changed
    except Exception:
        partial.unlink(missing_ok=True)
        raise
    finally:
        previous.close()
        current.close()


def main() -> None:
    """Require explicit operator confirmation and print the output's status."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--offline-confirmed', action='store_true',
                        help='confirm all writers are stopped and a full source backup is retained')
    args = parser.parse_args()
    if not args.offline_confirmed:
        parser.error('--offline-confirmed is required')
    print('CP49 v2 aligned to current v2' if align(args.source, args.output)
          else 'Current v2 copied without changes')


if __name__ == '__main__':
    main()
