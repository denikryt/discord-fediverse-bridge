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

    def test_remote_post_delete_without_copies_is_monotonic_and_authenticated(self) -> None:
        """Canonical post tombstone survives zero targets and refuses a forged actor."""
        actor = 'https://remote.test/u/actor'
        community = 'https://remote.test/c/group'
        object_id = 'https://remote.test/post/zero'
        fields = dict(community_actor_id=community, source_channel_id=None,
                      source_thread_id=None, source_starter_message_id=None,
                      ap_activity_id='https://remote.test/create/zero', ap_object_id=object_id)
        original = self.db.discord_fanout_groups.create_thread_group(
            **fields, current_content_json=json.dumps({
                'schemaVersion': 1, 'kind': 'post', 'apObjectId': object_id,
                'authorActorId': actor, 'bodyMarkdown': 'Original',
            }))
        with self.assertRaises(ValueError):
            self.db.discord_fanout_groups.mark_inbound_deleted_if_live(
                kind='post', ap_object_id=object_id, community_actor_id=community,
                actor_id='https://wrong.test/actor')
        assert self.db.discord_fanout_groups.mark_inbound_deleted_if_live(
            kind='post', ap_object_id=object_id, community_actor_id=community,
            actor_id=actor)
        assert not self.db.discord_fanout_groups.mark_inbound_deleted_if_live(
            kind='post', ap_object_id=object_id, community_actor_id=community,
            actor_id=actor)
        from src.models import CommunityThreadGroup
        with self.db.session() as session:
            row = session.get(CommunityThreadGroup, original.id)
            assert row.deleted_at is not None and row.content_revision == 1
        with self.assertRaises(ValueError):
            self.db.discord_fanout_groups.create_thread_group(
                **fields, current_content_json=original.current_content_json)

    def test_remote_comment_delete_with_zero_targets_preserves_parent(self) -> None:
        """A child-only remote Delete cannot tombstone its canonical parent."""
        actor = 'https://remote.test/u/actor'
        community = 'https://remote.test/c/group'
        root = self.db.discord_fanout_groups.create_thread_group(
            community_actor_id=community, source_channel_id=None,
            source_thread_id=None, source_starter_message_id=None,
            ap_activity_id='https://remote.test/create/root', ap_object_id='https://remote.test/root')
        comment_id = 'https://remote.test/comment/zero'
        child = self.db.discord_fanout_groups.create_message_group(
            community_actor_id=community, thread_group_id=root.id, source_channel_id=None,
            source_thread_id=None, source_message_id=None,
            ap_activity_id='https://remote.test/create/comment', ap_object_id=comment_id,
            current_content_json=json.dumps({
                'schemaVersion': 1, 'kind': 'comment', 'apObjectId': comment_id,
                'authorActorId': actor, 'bodyMarkdown': 'Comment',
            }))
        assert self.db.discord_fanout_groups.mark_inbound_deleted_if_live(
            kind='comment', ap_object_id=comment_id,
            community_actor_id=community, actor_id=actor)
        from src.models import CommunityMessageGroup, CommunityThreadGroup
        with self.db.session() as session:
            assert session.get(CommunityMessageGroup, child.id).deleted_at is not None
            assert session.get(CommunityMessageGroup, child.id).content_revision == 1
            assert session.get(CommunityThreadGroup, root.id).deleted_at is None

    def test_remote_update_persists_without_copies_and_rejects_stale_revision(self) -> None:
        """Only a provably newer author Update replaces the canonical snapshot."""
        from datetime import datetime, timedelta, timezone
        actor = 'https://remote.test/u/actor'
        community = 'https://remote.test/c/group'
        ap_id = 'https://remote.test/post/update'
        initial = {'schemaVersion': 1, 'kind': 'post', 'apObjectId': ap_id,
                   'authorActorId': actor, 'bodyMarkdown': 'initial'}
        row = self.db.discord_fanout_groups.create_thread_group(
            community_actor_id=community, source_channel_id=None,
            source_thread_id=None, source_starter_message_id=None,
            ap_activity_id='https://remote.test/create/update', ap_object_id=ap_id,
            current_content_json=json.dumps(initial))
        t0 = datetime(2026, 10, 23, 10, 0, tzinfo=timezone.utc)
        fields = dict(kind='post', ap_object_id=ap_id,
                      community_actor_id=community, actor_id=actor,
                      new_content_json=json.dumps(initial | {'bodyMarkdown': 'new'}),
                      updated_at=t0, source_activity_id='https://remote.test/update/1')
        assert self.db.discord_fanout_groups.admit_authenticated_update(**fields)
        assert not self.db.discord_fanout_groups.admit_authenticated_update(**fields)
        for changed in (
            {'actor_id': 'https://forged.test/u/other'},
            {'updated_at': t0 - timedelta(seconds=1)},
            {'updated_at': datetime(2026, 10, 23, 11, 0)},
            {'updated_at': None},
            {'source_activity_id': None},
            {'new_content_json': json.dumps(initial | {'bodyMarkdown': 'other'})},
        ):
            with self.assertRaises(ValueError):
                self.db.discord_fanout_groups.admit_authenticated_update(**(fields | changed))
        from src.models import CommunityThreadGroup
        with self.db.session() as session:
            current = session.get(CommunityThreadGroup, row.id)
            assert current.content_revision == 1
            assert json.loads(current.current_content_json)['bodyMarkdown'] == 'new'
