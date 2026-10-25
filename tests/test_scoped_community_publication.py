"""An authored ActivityPub graph preserves exact typed physical source identity."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from src.db.database import Database
from src.platform_types import MessageRef
from src.content_sync.persistence import (
    PublicationConflictError,
    persist_publish_artifacts,
    inspect_publish_artifacts,
)


FIXTURE = Path(__file__).parent / 'fixtures/release_v0_1_0_original.sqlite'


class ExactPublicationSourceScenarios(unittest.TestCase):
    """Different physical threads must not be mistaken for a duplicate author."""

    def setUp(self) -> None:
        """Use populated genuine released-v0 source after the current-v1 upgrade."""
        from test_legacy_v0_to_v1_migration import _seed

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        file = Path(self.temp.name) / 'db.sqlite'
        shutil.copyfile(FIXTURE, file)
        self.db = Database(f'sqlite:///{file}')
        self.addCleanup(self.db.close)
        _seed(self.db)
        self.db.migrate()
        # Both source comments must have their own verified physical root.
        # Non-numeric Discord thread IDs are invalid; a fake second connector
        # would belong to the connector-neutral identity tests instead.
        for thread, starter in ((501,601),(502,602)):
            group=self.db.discord_fanout_groups.create_thread_group(
                community_actor_id='https://remote.test/c/news',
                source_channel_id=None,source_thread_id=None,
                source_starter_message_id=None,
                ap_activity_id=f'https://remote.test/a/parent/{thread}',
                ap_object_id=f'https://remote.test/o/parent/{thread}',
            )
            self.db.discord_fanout_groups.add_thread_delivery(
                thread_group_id=group.id,discord_channel_id=902,
                discord_thread_id=thread,discord_starter_message_id=starter,
                role='inbound',
            )

    @staticmethod
    def payload(thread: str, index: int) -> dict:
        """One verified Discord reply source, rendered as its own AP object."""
        object_id=f'https://bridge.test/actors/users/alice/objects/comments/new-{index}'
        activity_id=f'https://bridge.test/actors/users/alice/activities/create/comments/new-{index}'
        return dict(
            source_id='777', source_message=MessageRef('discord-primary', thread, '777'),
            actor_username='alice',actor_url='https://bridge.test/actors/users/alice',
            community_actor_url='https://remote.test/c/news',
            activity_id=activity_id,object_id=object_id,
            activity_json={'id':activity_id,'type':'Create','actor':'https://bridge.test/actors/users/alice'},
            kind='comment',title=None,body_markdown='hello',
            in_reply_to_object_id=f'https://remote.test/o/parent/{thread}',discord_channel_id=902,discord_message_id=777,
        )

    def test_sibling_threads_reuse_same_numeric_message_without_publication_collision(self) -> None:
        """Two posts with same source message ID but different containers coexist."""
        result_a = persist_publish_artifacts(self.db, **self.payload('501',1))
        self.assertTrue(result_a.created)
        result_b = persist_publish_artifacts(self.db, **self.payload('502',2))
        self.assertTrue(result_b.created)
        self.assertNotEqual(result_a.source_id,result_b.source_id)
        self.assertFalse(persist_publish_artifacts(self.db,**self.payload('501',1)).created)
        self.assertFalse(inspect_publish_artifacts(self.db,**{
            k:v for k,v in self.payload('502',2).items()
            if k in {'source_id','source_message','actor_url','community_actor_url','activity_id','object_id','in_reply_to_object_id','discord_channel_id','discord_message_id'}
        },publication_context='community',discord_guild_id=None,kind='comment').created)
        with self.assertRaises(PublicationConflictError):
            persist_publish_artifacts(self.db,**(self.payload('502',2)|{'source_message': MessageRef('discord-primary','501','777')}))
        with self.db.engine.connect() as conn:
            rows=conn.exec_driver_sql("SELECT source_external_container_id FROM message_mappings WHERE source_id='777' ORDER BY source_external_container_id").scalars().all()
            self.assertEqual(rows,['501','502'])

    def test_unscoped_legacy_lookup_refuses_multi_container_ambiguity(self) -> None:
        """An old numeric-only lookup can never pick a random sibling thread."""
        persist_publish_artifacts(self.db, **self.payload('501',1))
        persist_publish_artifacts(self.db, **self.payload('502',2))
        with self.assertRaises(PublicationConflictError):
            inspect_publish_artifacts(self.db,**{
                k:v for k,v in self.payload('501',1).items()
                if k in {'source_id','actor_url','community_actor_url','activity_id','object_id','in_reply_to_object_id','discord_channel_id','discord_message_id'}
            },publication_context='community',discord_guild_id=None,kind='comment')


if __name__=='__main__':
    unittest.main(verbosity=2)
