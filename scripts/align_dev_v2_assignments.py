"""Explicit, offline-only development v2 assignment-table alignment.

Align only the Follow-assignment index on the current Group-view v2 schema.
Old CP21/CP32/CP35 physical v2 files are not valid sources because they lack
Group-view columns. Never change the source or align on application startup.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
from pathlib import Path

from src.db.backup import schema_entries_fingerprint


TABLE = 'remote_person_channel_assignments'
INDEX = 'ix_remote_person_assignments_target'
NEW_INDEX = 'uq_remote_person_assignment_follow_guild'
# Exact CP32 development v2 table shape, accepted only by the offline tool.
PREVIOUS_CONSTRAINT = '\tCONSTRAINT uq_remote_person_assignment_follow_guild UNIQUE (follow_id, discord_guild_id),\n'
COLUMNS = ('id', 'follow_id', 'discord_guild_id', 'discord_channel_id', 'created_at')


def _entries(db: sqlite3.Connection) -> list[tuple[object, ...]]:
    """Read the exact physical table/index contract used by startup validation."""
    return db.execute("""SELECT type, name, tbl_name, sql FROM sqlite_master
        WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name, tbl_name""").fetchall()


def _reference() -> tuple[sqlite3.Connection, dict[str, str]]:
    """Construct the authoritative current v2 schema in an isolated database."""
    path = Path(__file__).resolve().parents[1] / 'src/db/schema_versions/v2.sql'
    db = sqlite3.connect(':memory:')
    db.executescript(path.read_text(encoding='utf-8'))
    ddl = {name: sql for kind, name, table, sql in _entries(db) if sql is not None}
    return db, ddl


def _guard_source(db: sqlite3.Connection, reference: sqlite3.Connection, ddl: dict[str, str]) -> bool:
    """Admit updated v2 or an otherwise exact index-only variant."""
    if db.execute('PRAGMA user_version').fetchone()[0] != 2:
        raise ValueError('only development SQLite user_version=2 is accepted')
    if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or db.execute('PRAGMA foreign_key_check').fetchone():
        raise ValueError('source SQLite integrity/foreign keys failed')
    entries = _entries(db)
    expected = _entries(reference)
    if schema_entries_fingerprint(entries) == schema_entries_fingerprint(expected):
        return False
    # Reject any unrelated physical table/index change before taking a copy.
    def unaffected(rows):
        return [entry for entry in rows if entry[1] != TABLE and entry[2] != TABLE]
    if schema_entries_fingerprint(unaffected(entries)) != schema_entries_fingerprint(unaffected(expected)):
        raise ValueError('source differs from v2 outside the assignment table')
    actual_table = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone()
    previous_ddl = ddl[TABLE].replace(
        '\tUNIQUE (follow_id, discord_channel_id),\n',
        '\tUNIQUE (follow_id, discord_channel_id),\n' + PREVIOUS_CONSTRAINT,
    )
    if actual_table is None or actual_table[0] not in (ddl[TABLE], previous_ddl):
        raise ValueError('assignment table is not a recognized development v2 shape')
    # A manually added unique index is redundant; allow only the exact known
    # (follow_id, guild_id) columns, not arbitrary extra index semantics.
    for kind, name, table, sql in entries:
        if table != TABLE or kind != 'index' or name == INDEX:
            continue
        # Only an exact, full unique index is redundant. A partial guard,
        # altered collation or expression may be unrelated source tampering.
        expected_manual = (f'CREATE UNIQUE INDEX {name} ON {TABLE} '
                           '(follow_id, discord_guild_id)')
        if sql != expected_manual:
            raise ValueError(f'unrecognized extra assignment index: {name}')
        quoted = name.replace('"', '""')
        columns = [r[2] for r in db.execute(f'PRAGMA index_info("{quoted}")')]
        if columns != ['follow_id', 'discord_guild_id']:
            raise ValueError(f'unrecognized extra assignment index: {name}')
    duplicates = db.execute(f'''SELECT follow_id, discord_guild_id, COUNT(*)
        FROM {TABLE} GROUP BY follow_id, discord_guild_id HAVING COUNT(*) > 1''').fetchall()
    if duplicates:
        raise ValueError(f'conflicting follow/guild assignments: {duplicates}')
    return True


def align(source: Path, output: Path) -> bool:
    """Write an independently checked aligned copy, preserving the input bytes."""
    source = source.resolve()
    output = output.resolve()
    if not source.is_file() or source == output or output.exists():
        raise ValueError('source must exist; output must be a different, absent file')
    partial = output.with_name(output.name + '.partial')
    if partial.exists():
        raise ValueError(f'remove a stale partial file manually: {partial}')
    reference, ddl = _reference()
    changed = False
    try:
        # Escape URI-special characters ('?' and '#') in filesystem paths.
        with reference, sqlite3.connect(source.as_uri() + '?mode=ro', uri=True) as original:
            changed = _guard_source(original, reference, ddl)
            # SQLite backup captures WAL content consistently. The source is
            # read-only here; the operator must keep all writers stopped.
            with sqlite3.connect(partial) as destination:
                original.backup(destination)
        os.chmod(partial, 0o600)
        with sqlite3.connect(partial) as destination:
            if changed:
                before = destination.execute(f'SELECT * FROM {TABLE} ORDER BY id').fetchall()
                destination.execute('PRAGMA foreign_keys=OFF')
                destination.execute('BEGIN IMMEDIATE')
                try:
                    original_ddl = destination.execute(
                        'SELECT sql FROM sqlite_master WHERE type="table" AND name=?', (TABLE,),
                    ).fetchone()[0]
                    if PREVIOUS_CONSTRAINT in original_ddl:
                        # CP32's table constraint is a different physical
                        # schema; only this recognized shape needs rebuilding.
                        destination.execute(f'ALTER TABLE {TABLE} RENAME TO __dev_old_person_assignments')
                        destination.execute(ddl[TABLE])
                        cols = ', '.join(COLUMNS)
                        destination.execute(f'INSERT INTO {TABLE} ({cols}) '
                                            f'SELECT {cols} FROM __dev_old_person_assignments')
                        destination.execute('DROP TABLE __dev_old_person_assignments')
                        destination.execute(ddl[INDEX])
                    # CP21 just needs one named unique index. Drop any exact
                    # column-equivalent manual indexes admitted by the guard.
                    for kind, name, table, sql in _entries(destination):
                        if table == TABLE and kind == 'index' and name not in (INDEX, NEW_INDEX):
                            destination.execute(f'DROP INDEX "{name}"')
                    if not any(name == NEW_INDEX for _, name, _, _ in _entries(destination)):
                        destination.execute(ddl[NEW_INDEX])
                    if destination.execute(f'SELECT * FROM {TABLE} ORDER BY id').fetchall() != before:
                        raise ValueError('assignment rows changed during v2 alignment')
                    destination.execute('COMMIT')
                except Exception:
                    destination.execute('ROLLBACK')
                    raise
                finally:
                    destination.execute('PRAGMA foreign_keys=ON')
            if schema_entries_fingerprint(_entries(destination)) != schema_entries_fingerprint(_entries(reference)):
                raise ValueError('aligned schema differs from fixed v2 snapshot')
            if destination.execute('PRAGMA user_version').fetchone()[0] != 2:
                raise ValueError('aligned database changed its v2 version')
            if destination.execute('PRAGMA foreign_key_check').fetchone():
                raise ValueError('aligned database has foreign key violations')
            if destination.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('aligned database failed integrity_check')
        partial.replace(output)
        return changed
    except Exception:
        partial.unlink(missing_ok=True)
        raise
    finally:
        reference.close()


def main() -> None:
    """Demand explicit operator assertion before producing a new dev database."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--offline-confirmed', action='store_true',
                        help='assert all application writers are stopped and source is backed up')
    args = parser.parse_args()
    if not args.offline_confirmed:
        parser.error('--offline-confirmed is required')
    print('Aligned prior v2' if align(args.source, args.output) else 'Current v2 copied unchanged')


if __name__ == '__main__':
    main()
