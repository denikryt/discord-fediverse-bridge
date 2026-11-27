"""Offline v2 assignment index alignment on a disposable SQLite snapshot."""
from __future__ import annotations

import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.align_dev_v2_assignments import align, _entries
from src.db.backup import schema_entries_fingerprint


class DevV2AssignmentAlignment(unittest.TestCase):
    """Verify snapshot-exact, lossless, conflict-failing offline development work."""

    def setUp(self):
        """Create the previous physical v2 snapshot with one real owner/Follow."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.source = self.root / 'old.db'
        self.output = self.root / 'aligned.db'
        snapshot = Path('src/db/schema_versions/v2.sql').read_text()
        self.latest = snapshot
        self.old = snapshot.replace(
            'CREATE UNIQUE INDEX uq_remote_person_assignment_follow_guild ON '
            'remote_person_channel_assignments (follow_id, discord_guild_id);\n', '')
        self.assertNotEqual(snapshot, self.old)
        with sqlite3.connect(self.source) as conn:
            conn.executescript(self.old)
            conn.execute('PRAGMA user_version=2')
            now = '2026-11-18 01:00:00'
            conn.execute('''INSERT INTO users
                (id, discord_user_id, activitypub_username, actor_url, inbox_url,
                 outbox_url, followers_url, public_key_pem, private_key_pem,
                 profile_version, created_at, updated_at)
                 VALUES (1,'1','alice','https://bridge.example/a','https://bridge.example/i',
                         'https://bridge.example/o','https://bridge.example/f','pub','priv',1,?,?)''', (now, now))
            conn.execute('''INSERT INTO remote_person_follows
                (id,user_id,remote_actor_id,remote_inbox_url,follow_activity_id,
                 generation,relationship_status,follow_dispatch_state,undo_dispatch_state,created_at,updated_at)
                VALUES (1,1,'https://remote.example/b','https://remote.example/i',
                        'https://bridge.example/follow/1',1,'accepted','sent','not_requested',?,?)''', (now, now))
            conn.execute('''INSERT INTO remote_person_channel_assignments
                (id,follow_id,discord_guild_id,discord_channel_id,created_at)
                VALUES(1,1,10,200,?)''', (now,))

    def test_index_alignment_produces_exact_v2_and_preserves_all_rows(self):
        """An explicit copy adds one index without a v3 or table rebuild."""
        self.assertTrue(align(self.source, self.output))
        with sqlite3.connect(self.source) as old, sqlite3.connect(self.output) as new:
            self.assertEqual(old.execute('SELECT * FROM remote_person_channel_assignments').fetchall(),
                             new.execute('SELECT * FROM remote_person_channel_assignments').fetchall())
            self.assertEqual(new.execute('PRAGMA user_version').fetchone()[0], 2)
            self.assertNotEqual(schema_entries_fingerprint(_entries(old)),
                                schema_entries_fingerprint(_entries(new)))
            self.assertEqual(new.execute('PRAGMA foreign_key_check').fetchall(), [])
            with sqlite3.connect(':memory:') as expected:
                expected.executescript(self.latest)
                self.assertEqual(schema_entries_fingerprint(_entries(new)),
                                 schema_entries_fingerprint(_entries(expected)))
        self.assertFalse(align(self.output, self.root / 'copied.db'))

    def test_manually_added_index_is_replaced_by_named_v2_index(self):
        """An equivalent manual guard is replaced with the frozen named index."""
        with sqlite3.connect(self.source) as conn:
            conn.execute('CREATE UNIQUE INDEX manual_follow_guild '
                         'ON remote_person_channel_assignments (follow_id, discord_guild_id)')
        self.assertTrue(align(self.source, self.output))
        with sqlite3.connect(self.output) as aligned, sqlite3.connect(':memory:') as expected:
            expected.executescript(self.latest)
            indexes = [row[0] for row in aligned.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND tbl_name='remote_person_channel_assignments'")]
            self.assertNotIn('manual_follow_guild', indexes)
            self.assertEqual(schema_entries_fingerprint(_entries(aligned)),
                             schema_entries_fingerprint(_entries(expected)))

    def test_named_index_alone_is_accepted_by_the_v2_schema_guard(self):
        """The documented one-index offline operation matches the exact snapshot."""
        with sqlite3.connect(self.source) as conn:
            conn.execute('CREATE UNIQUE INDEX uq_remote_person_assignment_follow_guild '
                         'ON remote_person_channel_assignments (follow_id, discord_guild_id)')
        self.assertFalse(align(self.source, self.output))
        with sqlite3.connect(self.output) as aligned, sqlite3.connect(':memory:') as expected:
            expected.executescript(self.latest)
            self.assertEqual(schema_entries_fingerprint(_entries(aligned)),
                             schema_entries_fingerprint(_entries(expected)))

    def test_reserved_uri_characters_in_source_path(self):
        """Literal '?' and '#' in a valid filename cannot become URI query data."""
        unusual = self.root / 'prior-v2-with-spaces ?#.db'
        self.source.rename(unusual)
        content = unusual.read_bytes()
        # Exercise the shipped CLI, not only its helper: argv and SQLite URI
        # parsing must preserve reserved characters in the actual filename.
        result = subprocess.run(
            [sys.executable, '-m', 'scripts.align_dev_v2_assignments',
             '--source', str(unusual), '--output', str(self.output), '--offline-confirmed'],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Aligned prior v2', result.stdout)
        self.assertEqual(unusual.read_bytes(), content)
        with sqlite3.connect(self.output) as aligned, sqlite3.connect(':memory:') as expected:
            expected.executescript(self.latest)
            self.assertEqual(schema_entries_fingerprint(_entries(aligned)),
                             schema_entries_fingerprint(_entries(expected)))

    def test_cp32_table_constraint_can_be_aligned_explicitly(self):
        """Recognized prior physical v2 table UNIQUE is rebuilt only offline."""
        cp32 = self.root / 'cp32.db'
        former = self.latest.replace(
            '\tUNIQUE (follow_id, discord_channel_id),\n',
            '\tUNIQUE (follow_id, discord_channel_id),\n'
            '\tCONSTRAINT uq_remote_person_assignment_follow_guild UNIQUE (follow_id, discord_guild_id),\n',
        ).replace('CREATE UNIQUE INDEX uq_remote_person_assignment_follow_guild ON '
                  'remote_person_channel_assignments (follow_id, discord_guild_id);\n', '')
        with sqlite3.connect(cp32) as conn:
            conn.executescript(former)
            conn.execute('PRAGMA user_version=2')
        self.assertTrue(align(cp32, self.output))
        with sqlite3.connect(self.output) as aligned, sqlite3.connect(':memory:') as expected:
            expected.executescript(self.latest)
            self.assertEqual(schema_entries_fingerprint(_entries(aligned)),
                             schema_entries_fingerprint(_entries(expected)))

    def test_partial_manual_index_is_not_an_equivalent_guard(self):
        """A partial index is not a substitute for guild-wide uniqueness."""
        with sqlite3.connect(self.source) as conn:
            conn.execute('CREATE UNIQUE INDEX manual_partial ON '
                         'remote_person_channel_assignments (follow_id, discord_guild_id) '
                         'WHERE follow_id=1')
        with self.assertRaisesRegex(ValueError, 'unrecognized extra assignment index'):
            align(self.source, self.output)
        self.assertFalse(self.output.exists())

    def test_conflicts_abort_without_creating_output(self):
        """Two channels of the same Follow/guild require manual owner choice."""
        now = '2026-11-18 01:00:00'
        with sqlite3.connect(self.source) as conn:
            conn.execute('''INSERT INTO remote_person_channel_assignments
                (id,follow_id,discord_guild_id,discord_channel_id,created_at)
                VALUES (2,1,10,201,?)''', (now,))
        with self.assertRaisesRegex(ValueError, 'conflicting follow/guild'):
            align(self.source, self.output)
        self.assertFalse(self.output.exists())
        with sqlite3.connect(self.source) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM remote_person_channel_assignments').fetchone()[0], 2)

    def test_unrecognized_schema_is_refused_before_copy(self):
        """Only the documented v2 physical difference is supported."""
        with sqlite3.connect(self.source) as conn:
            conn.execute('CREATE TABLE unexpected_fixture (id INTEGER)')
        with self.assertRaisesRegex(ValueError, 'outside the assignment table'):
            align(self.source, self.output)
        self.assertFalse(self.output.exists())


if __name__ == '__main__':
    unittest.main()
