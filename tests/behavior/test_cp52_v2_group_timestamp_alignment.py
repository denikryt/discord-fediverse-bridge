"""Offline-only real SQLite alignment from CP49 v2 to current v2 DDL.

Verify the source remains byte-identical and every existing ID, parent link,
receipt, delivery, claim cursor and original Create survives the table rebuild.
"""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
from pathlib import Path

import pytest

from src.db import Database
from src.db.backup import schema_entries_fingerprint
from src.db.migrations import read_database_state

TABLES = ('community_thread_groups', 'community_message_groups')
ROOT = 'https://owner.example/notes/8'
REPLY = 'https://owner.example/notes/8/replies/3'
GROUP = 'https://nodebb.example/category/7'


def _previous_schema() -> str:
    """Reconstruct only the two-column difference from the exact CP49 DDL."""
    sql = (Path(__file__).parents[2] / 'src/db/schema_versions/v2.sql').read_text()
    assert sql.count('\tview_source_updated_at DATETIME,\n') == 2
    return sql.replace('\tview_source_updated_at DATETIME,\n', '')


def _old_db(path: Path) -> None:
    """Seed a physical CP49 v2 with Group ancestry and proven/unknown claims."""
    with sqlite3.connect(path) as connection:
        connection.executescript(_previous_schema())
        connection.execute('PRAGMA user_version=2')
        connection.execute('''INSERT INTO community_thread_groups
            (id,community_actor_id,ap_object_id,source_owner_actor_id,
             inbound_create_snapshot_json,view_revision,view_state,
             display_object_json,view_updated_at,created_at)
            VALUES(17,?,?,?, ?,4,'live',?,?,?)''',
            (GROUP, ROOT, 'https://owner.example/actors/alice',
             '{"ap_id":"'+ROOT+'","kind":"post","body_markdown":"original"}',
             '{"ap_id":"'+ROOT+'","kind":"post","body_markdown":"updated"}',
             '2026-11-18 10:00:00', '2026-11-17 10:00:00'))
        connection.execute('''INSERT INTO community_message_groups
            (id,community_actor_id,thread_group_id,ap_object_id,source_owner_actor_id,
             inbound_create_snapshot_json,view_revision,view_state,
             display_object_json,view_updated_at,created_at)
            VALUES(23,?,17,?,?, ?,5,'deleted',?,?,?)''',
            (GROUP, REPLY, 'https://owner.example/actors/alice',
             '{"ap_id":"'+REPLY+'","kind":"comment","body_markdown":"original"}',
             '{"ap_id":"'+REPLY+'","kind":"comment","body_markdown":"edited"}',
             '2026-11-18 11:00:00', '2026-11-17 11:00:00'))
        connection.execute('''INSERT INTO community_thread_group_deliveries
            (id,thread_group_id,discord_channel_id,discord_thread_id,
             discord_starter_message_id,role)
            VALUES(29,17,10,110,110,'inbound')''')
        connection.execute('''INSERT INTO community_message_group_deliveries
            (id,message_group_id,discord_channel_id,discord_thread_id,discord_message_id,role)
            VALUES(31,23,10,110,111,'inbound')''')
        connection.execute('''INSERT INTO community_discord_target_claims
            (id,thread_group_id,discord_channel_id,kind,status,attempt_id,
             reserved_view_revision,applied_revision)
            VALUES(37,17,10,'post','completed','token-root',3,3)''')
        connection.execute('''INSERT INTO community_discord_target_claims
            (id,message_group_id,discord_channel_id,kind,status,attempt_id,
             reserved_view_revision,applied_revision)
            VALUES(38,23,10,'comment','completed','token-comment',4,4)''')
        connection.execute('''INSERT INTO community_discord_target_claims
            (id,message_group_id,discord_channel_id,kind,status,attempt_id,
             reserved_view_revision,applied_revision)
            VALUES(39,23,11,'comment','unknown','lost-ack',5,NULL)''')


def _entries(db):
    """Capture the full physical DDL accepted by startup."""
    return db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master "
                      "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name,tbl_name").fetchall()


def _rows(db, table):
    """Preserve byte-like SQL values without an ORM type conversion."""
    return db.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall()


def test_cp49_populated_v2_is_aligned_offline_with_all_rows_and_identities(tmp_path: Path) -> None:
    """The old v2 is rejected at startup, but its explicitly copied output is valid."""
    from scripts.align_dev_v2_group_view_timestamps import align

    source = tmp_path / 'CP49 old v2 ?# source.db'
    output = tmp_path / 'aligned current v2 ?# output.db'
    _old_db(source)
    initial_bytes = source.read_bytes()
    with sqlite3.connect(source) as old:
        original_tables = [name for (name,) in old.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        original_rows = {name: _rows(old, name) for name in original_tables}
        original_cols = {name: [r[1] for r in old.execute(f'PRAGMA table_info("{name}")')]
                         for name in TABLES}
    assert align(source, output) is True
    assert source.read_bytes() == initial_bytes
    with sqlite3.connect(output) as migrated:
        assert migrated.execute('PRAGMA user_version').fetchone()[0] == 2
        assert migrated.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert migrated.execute('PRAGMA foreign_key_check').fetchall() == []
        for name in original_tables:
            if name in TABLES:
                cols = ','.join('"'+col+'"' for col in original_cols[name])
                assert migrated.execute(f'SELECT {cols} FROM "{name}" ORDER BY rowid').fetchall() == original_rows[name]
                assert all(row[0] is None for row in migrated.execute(
                    f'SELECT view_source_updated_at FROM "{name}"'))
            else:
                assert _rows(migrated, name) == original_rows[name]
    # SQLAlchemy URL parsing treats '?' and '#' as syntax. Verify startup
    # on the same output bytes under a conventional database path.
    startup_copy = tmp_path / 'startup_verified.db'
    shutil.copyfile(output, startup_copy)
    db = Database(f'sqlite:///{startup_copy}')
    try:
        assert read_database_state(db.engine).profile == 'ready_v2'
        db.migrate()
    finally:
        db.close()
    with pytest.raises(ValueError, match='different absent file'):
        # Reusing the output as input would be read-only and already-current;
        # passing the same destination path must still be rejected.
        align(output, output)


def test_cp49_alignment_rejects_unknown_schema_or_version_and_keeps_source(tmp_path: Path) -> None:
    """A forged v2 header or extra index is not authority to rewrite a DB."""
    from scripts.align_dev_v2_group_view_timestamps import align

    source = tmp_path / 'old.db'
    dest = tmp_path / 'new.db'
    _old_db(source)
    with sqlite3.connect(source) as connection:
        connection.execute('CREATE INDEX extra_wrong_index ON community_thread_groups(view_revision)')
    before = hashlib.sha256(source.read_bytes()).digest()
    with pytest.raises(ValueError, match='schema|CP49'):
        align(source, dest)
    assert not dest.exists() and hashlib.sha256(source.read_bytes()).digest() == before
    with sqlite3.connect(source) as connection:
        connection.execute('DROP INDEX extra_wrong_index')
        connection.execute('PRAGMA user_version=3')
    before = source.read_bytes()
    with pytest.raises(ValueError, match='version|v2'):
        align(source, dest)
    assert not dest.exists() and source.read_bytes() == before


def test_current_v2_copies_without_rebuilding(tmp_path: Path) -> None:
    """Current physical v2 also works as a no-change explicit offline copy."""
    from scripts.align_dev_v2_group_view_timestamps import align

    source, dest = tmp_path/'v2.db', tmp_path/'copied.db'
    db = Database(f'sqlite:///{source}')
    db.migrate()
    db.close()
    assert align(source, dest) is False
    with sqlite3.connect(source) as old, sqlite3.connect(dest) as new:
        assert schema_entries_fingerprint(_entries(old)) == schema_entries_fingerprint(_entries(new))
        assert new.execute('PRAGMA user_version').fetchone()[0] == 2


def test_cli_requires_offline_confirmation_before_writing(tmp_path: Path) -> None:
    """An operator must explicitly acknowledge the stopped-writers boundary."""
    import subprocess
    import sys

    source, dest = tmp_path/'CP49 ?# input.db', tmp_path/'output.db'
    _old_db(source)
    args = [sys.executable, '-m', 'scripts.align_dev_v2_group_view_timestamps',
            '--source', str(source), '--output', str(dest)]
    refused = subprocess.run(args, capture_output=True, text=True, check=False)
    assert refused.returncode != 0 and not dest.exists()
    admitted = subprocess.run(args + ['--offline-confirmed'], capture_output=True,
                             text=True, check=False)
    assert admitted.returncode == 0, admitted.stderr
    assert dest.exists() and 'aligned' in admitted.stdout
