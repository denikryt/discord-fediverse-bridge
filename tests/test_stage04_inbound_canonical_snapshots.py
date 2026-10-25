"""Inbound AP content is durably retained before any physical create attempt."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.db import Database


class InboundCanonicalSnapshotScenarios(unittest.TestCase):
    """Persist a replay-fenced payload even when no Discord forum is subscribed."""

    def setUp(self) -> None:
        """Use a fresh full SQLite v1 database without any delivery rows."""
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db = Database(f'sqlite:///{Path(temp.name) / "bridge.sqlite"}')
        self.addCleanup(self.db.close)
        self.db.migrate()

    def test_inbound_post_without_targets_preserves_snapshot(self) -> None:
        """An AP Create must survive a restart with all required render fields."""
        snapshot = json.dumps({'schemaVersion': 1, 'kind': 'post', 'title': 'Title',
                               'bodyMarkdown': 'Body', 'authorActorId': 'https://remote.test/u/actor',
                               'authorName': 'Actor', 'url': 'https://remote.test/p/1',
                               'apObjectId': 'https://remote.test/o/1'}, sort_keys=True)
        group = self.db.discord_fanout_groups.create_thread_group(
            community_actor_id='https://remote.test/c/group', source_channel_id=None,
            source_thread_id=None, source_starter_message_id=None,
            ap_activity_id='https://remote.test/a/create/1', ap_object_id='https://remote.test/o/1',
            current_content_json=snapshot,
        )
        with self.db.session() as session:
            from src.models import CommunityThreadGroup, CommunityThreadGroupDelivery
            assert json.loads(session.get(CommunityThreadGroup, group.id).current_content_json) == json.loads(snapshot)
            assert not session.query(CommunityThreadGroupDelivery).count()

    def test_replayed_ap_create_must_not_replace_original_body(self) -> None:
        """A replay with different payload is a conflict, not a second Create."""
        fields = dict(community_actor_id='https://remote.test/c/group', source_channel_id=None,
                      source_thread_id=None, source_starter_message_id=None,
                      ap_activity_id='https://remote.test/a/create/1', ap_object_id='https://remote.test/o/1')
        first = self.db.discord_fanout_groups.create_thread_group(**fields, current_content_json=json.dumps({'schemaVersion': 1, 'kind': 'post', 'apObjectId': fields['ap_object_id'], 'bodyMarkdown': 'first'}))
        with self.assertRaises(ValueError):
            self.db.discord_fanout_groups.create_thread_group(**fields, current_content_json=json.dumps({'schemaVersion': 1, 'kind': 'post', 'apObjectId': fields['ap_object_id'], 'bodyMarkdown': 'second'}))
        with self.db.session() as session:
            from src.models import CommunityThreadGroup
            assert json.loads(session.get(CommunityThreadGroup, first.id).current_content_json)['bodyMarkdown'] == 'first'

    def test_comment_snapshot_stays_owned_by_original_ap_thread(self) -> None:
        """A comment must retain content even if its target delivery has not sent."""
        thread = self.db.discord_fanout_groups.create_thread_group(
            community_actor_id='https://remote.test/c/group', source_channel_id=None,
            source_thread_id=None, source_starter_message_id=None,
            ap_activity_id='https://remote.test/a/create/1', ap_object_id='https://remote.test/o/1')
        fields = dict(community_actor_id='https://remote.test/c/group', thread_group_id=thread.id,
                      source_channel_id=None, source_thread_id=None, source_message_id=None,
                      ap_activity_id='https://remote.test/a/create/2', ap_object_id='https://remote.test/o/2')
        comment = self.db.discord_fanout_groups.create_message_group(
            **fields, current_content_json=json.dumps({'schemaVersion': 1, 'kind': 'comment', 'apObjectId': fields['ap_object_id'], 'bodyMarkdown': 'original'}))
        with self.assertRaises(ValueError):
            self.db.discord_fanout_groups.create_message_group(
                **fields, current_content_json=json.dumps({'schemaVersion': 1, 'kind': 'comment', 'apObjectId': fields['ap_object_id'], 'bodyMarkdown': 'changed'}))
        with self.db.session() as session:
            from src.models import CommunityMessageGroup
            assert json.loads(session.get(CommunityMessageGroup, comment.id).current_content_json)['bodyMarkdown'] == 'original'


if __name__ == '__main__':
    unittest.main()
