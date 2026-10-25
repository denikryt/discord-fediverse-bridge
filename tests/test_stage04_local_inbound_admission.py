"""Canonical Local Community inbound AP admission has no send-before-save gap."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from sqlalchemy import select

from src.models import LocalCommunityThreadSurface, LocalCommunityMessageSurface
from test_local_receipt_persistence import world


def snapshot(kind: str, object_id: str, body: str) -> str:
    """Construct a typed complete AP snapshot for a deterministic test source."""
    return json.dumps({'schemaVersion': 1, 'kind': kind, 'apObjectId': object_id,
                       'bodyMarkdown': body, 'authorActorId': 'https://remote.test/u/bob',
                       'authorName': 'Bob'})


class LocalInboundCanonicalAdmissionScenarios(unittest.TestCase):
    """Positive and negative SQLite scenarios with independent physical receipts."""

    def setUp(self) -> None:
        """Open fresh real SQLite v1 database with registered forum host."""
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db, self.community_id = world(Path(temp.name))
        self.addCleanup(self.db.close)

    def post(self, **overrides):
        """Admit one specific AP post identity with no physical delivery."""
        kwargs = dict(local_community_id=self.community_id,
                      ap_activity_id='https://remote.test/create/post',
                      ap_object_id='https://remote.test/post',
                      current_content_json=snapshot('post', 'https://remote.test/post', 'original'))
        return self.db.local_community_content.admit_inbound_post(**(kwargs | overrides))

    def test_post_canonical_precedes_any_physical_receipt(self) -> None:
        """No Discord host row is needed for canonical AP identity or body."""
        first = self.post()
        self.assertEqual(first.id, self.post().id)
        with self.db.session() as session:
            self.assertIsNone(session.scalar(select(LocalCommunityThreadSurface)))
        self.assertEqual(json.loads(first.current_content_json)['bodyMarkdown'], 'original')

    def test_post_conflicts_and_deleted_replays_rejected(self) -> None:
        """Changed actor/source payload or canonical Delete cannot mutate a prior Create."""
        post = self.post()
        for changed in (
            {'current_content_json': snapshot('post', 'https://remote.test/post', 'forged')},
            {'ap_activity_id': 'https://remote.test/create/other'},
            {'local_community_id': self.community_id + 999},
            {'current_content_json': snapshot('comment', 'https://remote.test/post', 'original')},
        ):
            with self.assertRaises(ValueError):
                self.post(**changed)
        from datetime import datetime, timezone
        from src.models import LocalCommunityThread
        with self.db.immediate_session() as session:
            session.get(LocalCommunityThread, post.id).deleted_at = datetime.now(timezone.utc)
        with self.assertRaises(ValueError):
            self.post()

    def test_comment_canonical_does_not_require_host_receipt(self) -> None:
        """Parent canonical post and reply are stored without any Discord writes."""
        thread = self.post()
        kwargs = dict(local_community_thread_id=thread.id,
                      ap_activity_id='https://remote.test/create/comment',
                      ap_object_id='https://remote.test/comment',
                      parent_ap_object_id=thread.ap_object_id,
                      current_content_json=snapshot('comment', 'https://remote.test/comment', 'reply'))
        row = self.db.local_community_content.admit_inbound_comment(**kwargs)
        self.assertEqual(row.id, self.db.local_community_content.admit_inbound_comment(**kwargs).id)
        with self.db.session() as session:
            self.assertIsNone(session.scalar(select(LocalCommunityMessageSurface)))
        with self.assertRaises(ValueError):
            self.db.local_community_content.admit_inbound_comment(
                **(kwargs | {'current_content_json': snapshot('comment', 'https://remote.test/comment', 'forged')}))

    def test_comment_requires_exact_live_same_thread_nested_parent(self) -> None:
        """Unknown, foreign and deleted AP nested parents cannot be flattened."""
        thread = self.post()
        kwargs = dict(local_community_thread_id=thread.id,
                      ap_activity_id='https://remote.test/create/nested',
                      ap_object_id='https://remote.test/nested',
                      parent_ap_object_id='https://remote.test/missing',
                      current_content_json=snapshot('comment', 'https://remote.test/nested', 'nested'))
        with self.assertRaises(ValueError):
            self.db.local_community_content.admit_inbound_comment(**kwargs)
        foreign = self.db.local_community_content.admit_inbound_post(
            local_community_id=self.community_id,
            ap_activity_id='https://remote.test/create/foreign',
            ap_object_id='https://remote.test/foreign',
            current_content_json=snapshot('post', 'https://remote.test/foreign', 'foreign'))
        foreign_reply = self.db.local_community_content.admit_inbound_comment(
            local_community_thread_id=foreign.id,
            ap_activity_id='https://remote.test/create/foreign-reply',
            ap_object_id='https://remote.test/foreign-reply',
            parent_ap_object_id=foreign.ap_object_id,
            current_content_json=snapshot('comment', 'https://remote.test/foreign-reply', 'original'))
        with self.assertRaises(ValueError):
            self.db.local_community_content.admit_inbound_comment(
                **(kwargs | {'parent_ap_object_id': foreign_reply.ap_object_id}))
        from datetime import datetime, timezone
        from src.models import LocalCommunityMessage
        with self.db.immediate_session() as session:
            session.get(LocalCommunityMessage, foreign_reply.id).deleted_at = datetime.now(timezone.utc)
        with self.assertRaises(ValueError):
            self.db.local_community_content.admit_inbound_comment(
                **(kwargs | {'parent_ap_object_id': foreign_reply.ap_object_id}))


if __name__ == '__main__':
    unittest.main()
