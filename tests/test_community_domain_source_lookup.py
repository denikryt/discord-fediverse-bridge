"""Mode-specific canonical source lookups preserve immediate message containers."""

from __future__ import annotations
import shutil
import tempfile
import unittest
from pathlib import Path

from src.db.database import Database
from src.platform_types import MessageRef
from src.models import LocalCommunityThread, LocalCommunityMessage
from sqlalchemy.exc import IntegrityError

FIXTURE=Path(__file__).parent/'fixtures/release_v0_1_0_original.sqlite'


class DomainSourceLookupScenarios(unittest.TestCase):
    """One foreign thread must not be a source for another canonical reply."""

    def setUp(self) -> None:
        """Start from populated v0 so current owners and canonical refs are real."""
        from test_legacy_v0_to_v1_migration import _seed
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path=Path(tmp.name)/'db.sqlite'
        shutil.copyfile(FIXTURE,path)
        self.db=Database(f'sqlite:///{path}')
        self.addCleanup(self.db.close)
        _seed(self.db)
        self.db.migrate()

    def test_released_host_post_provenance_backfill_is_selective(self) -> None:
        """A known outbound host post gains a typed location, not arbitrary copies."""
        with self.db.session() as session:
            thread = session.get(LocalCommunityThread, 50)
            comment = session.get(LocalCommunityMessage, 51)
            self.assertEqual(thread.source_connector_id, 1)
            self.assertEqual(thread.source_external_thread_id, '2000')
            self.assertEqual(thread.source_external_starter_message_id, '2000')
            self.assertIsNotNone(thread.source_channel_surface_id)
            # The released comment row has no origin-kind proof: a subscriber
            # may have authored it and only later been mirrored to the host.
            self.assertIsNone(comment.source_connector_id)
            self.assertIsNone(comment.source_external_container_id)

    def test_subscriber_origin_does_not_inherit_host_mirror_as_source(self) -> None:
        """A host physical receipt cannot authorize a subscriber-authored post."""
        from test_legacy_v0_to_v1_migration import _seed
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path=Path(tmp.name)/'subscriber.sqlite'
        shutil.copyfile(FIXTURE,path)
        candidate=Database(f'sqlite:///{path}')
        self.addCleanup(candidate.close)
        _seed(candidate)
        with candidate.engine.begin() as conn:
            conn.exec_driver_sql("UPDATE local_community_threads SET direction='discord_to_ap', origin_kind='discord_local_subscriber' WHERE id=50")
        candidate.migrate()
        with candidate.session() as session:
            row=session.get(LocalCommunityThread,50)
            self.assertIsNone(row.source_connector_id)
            self.assertIsNone(row.source_channel_surface_id)
            self.assertIsNone(row.source_external_thread_id)

    def test_local_source_coordinates_have_unique_typed_identity(self) -> None:
        """A second canonical comment cannot claim the same physical message."""
        with self.db.immediate_session() as session:
            session.add(LocalCommunityMessage(
                local_community_thread_id=50,
                ap_activity_id='https://bridge.test/a/first-source',
                ap_object_id='https://bridge.test/o/first-source',
                direction='outbound', source_connector_id=1,
                source_external_container_id='thread-new',
                source_external_message_id='9999',
            ))
        with self.assertRaises(IntegrityError):
            with self.db.immediate_session() as session:
                session.add(LocalCommunityMessage(
                    local_community_thread_id=50,
                    ap_activity_id='https://bridge.test/a/colliding-source',
                    ap_object_id='https://bridge.test/o/colliding-source',
                    direction='outbound', source_connector_id=1,
                    source_external_container_id='thread-new',
                    source_external_message_id='9999',
                ))
        with self.assertRaises(IntegrityError):
            with self.db.immediate_session() as session:
                session.add(LocalCommunityMessage(
                    local_community_thread_id=50,
                    ap_activity_id='https://bridge.test/a/partial-source',
                    ap_object_id='https://bridge.test/o/partial-source',
                    direction='outbound', source_connector_id=1,
                    source_external_container_id='thread-new',
                    source_external_message_id=None,
                ))

    def test_remote_comment_source_requires_exact_thread(self) -> None:
        """A source comment with a known thread maps only to that thread."""
        row=self.db.discord_fanout_groups.create_message_group(
            community_actor_id='https://remote.test/c/news',thread_group_id=40,
            source_channel_id=902,source_thread_id=3000,source_message_id=1777,
            ap_activity_id='https://remote.test/a/new',ap_object_id='https://remote.test/o/new',
        )
        exact=MessageRef('discord-primary','3000','1777')
        self.assertEqual(self.db.discord_fanout_groups.get_message_group_by_source_ref(exact).id,row.id)
        self.assertIsNone(self.db.discord_fanout_groups.get_message_group_by_source_ref(MessageRef('discord-primary','another-thread','1777')))
        self.assertIsNone(self.db.discord_fanout_groups.get_message_group_by_source_ref(MessageRef('other','3000','1777')))

    def test_local_comment_source_is_not_a_mirror_lookup(self) -> None:
        """Only a verified immediate source identifies a canonical local comment."""
        with self.db.immediate_session() as session:
            session.add(LocalCommunityMessage(
                local_community_thread_id=50,ap_activity_id='https://bridge.test/a/new',
                ap_object_id='https://bridge.test/o/new',parent_ap_object_id=None,
                direction='outbound',source_connector_id=1,
                source_external_container_id='thread-source',source_external_message_id='1777',
            ))
        expected=MessageRef('discord-primary','thread-source','1777')
        self.assertEqual(self.db.local_community_content.get_local_message_by_source_ref(expected).ap_object_id,'https://bridge.test/o/new')
        self.assertIsNone(self.db.local_community_content.get_local_message_by_source_ref(MessageRef('discord-primary','another-thread','1777')))
        self.assertIsNone(self.db.local_community_content.get_local_message_by_source_ref(MessageRef('other','thread-source','1777')))


if __name__ == '__main__':
    unittest.main(verbosity=2)
