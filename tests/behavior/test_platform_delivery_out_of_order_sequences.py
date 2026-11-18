"""Cross-target reply ordering and unsubscribe decisions with real SQLite claims.

The provider adapter is the only fake. Reservations, materialization, durable
receipts, scope checks and coordinator restart use their production classes.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys

from sqlalchemy import select

from src.content_sync.platform_fanout import PlatformFanoutCoordinator
from src.models import (
    LocalCommunityMessage, LocalCommunityMessageSurface, LocalCommunityThreadSurface,
    LocalSubscriber, PlatformDeliveryIntent,
)
from src.platform_types import ContentDocument, DeliveryPresentation, MessageRef, ThreadRef
from .test_platform_message_fanout_scenarios import _ThreeForumFixture, _Registry


class OutOfOrderTwoTargetSequences(_ThreeForumFixture):
    """A and C have separate parents; a delayed C cannot borrow A's receipt."""

    def _prepare_nested(self) -> tuple[int, int, int, int]:
        """Create only the two root copies and an A parent, leaving C unconfirmed."""
        self.assertTrue(asyncio.run(self._deliver('A')))
        self.assertTrue(asyncio.run(self._deliver('C')))
        with self.database.immediate_session() as session:
            parent = LocalCommunityMessage(
                local_community_thread_id=self.canonical_id,
                ap_activity_id='https://remote.test/actors/alice/activities/parent',
                ap_object_id='https://remote.test/objects/parent', direction='inbound',
            )
            session.add(parent)
            session.flush()
            child = LocalCommunityMessage(
                local_community_thread_id=self.canonical_id,
                ap_activity_id='https://remote.test/actors/alice/activities/child',
                ap_object_id='https://remote.test/objects/child',
                parent_ap_object_id=parent.ap_object_id, direction='inbound',
                current_content_json=json.dumps({
                    'schemaVersion': 1, 'kind': 'comment',
                    'apObjectId': 'https://remote.test/objects/child',
                    'bodyMarkdown': 'child after parent',
                    'authorActorId': 'https://remote.test/u/alice',
                    'authorName': 'Alice', 'url': 'https://remote.test/objects/child',
                }),
            )
            session.add(child)
            session.flush()
            roots = {}
            for label in ('A', 'C'):
                roots[label] = session.scalar(select(LocalCommunityThreadSurface).where(
                    LocalCommunityThreadSurface.local_community_thread_id == self.canonical_id,
                    LocalCommunityThreadSurface.channel_surface_id == self.targets[label][2],
                ))
                self.assertIsNotNone(roots[label])
            session.add(LocalCommunityMessageSurface(
                local_community_message_id=parent.id,
                local_community_thread_surface_id=roots['A'].id,
                connector_id=roots['A'].connector_id,
                channel_surface_id=self.targets['A'][2],
                external_message_id='physical-parent-A', role='host',
            ))
            return parent.id, child.id, roots['A'].id, roots['C'].id

    def _blocked_c(self, *, child_id: int, parent_id: int) -> int:
        """Stage a duplicate child without manufacturing a root-parent fallback."""
        blocked = self.coordinator.stage_blocked(
            domain='local', kind='comment', canonical_id=child_id,
            forum=self.forums['C'], parent_canonical_id=parent_id,
        )
        self.assertIsNotNone(blocked)
        self.assertEqual(self.coordinator.stage_blocked(
            domain='local', kind='comment', canonical_id=child_id,
            forum=self.forums['C'], parent_canonical_id=parent_id,
        ), blocked)
        with self.database.session() as session:
            row = session.get(PlatformDeliveryIntent, blocked)
            self.assertEqual(row.state, 'blocked')
            self.assertIsNone(row.request_json)
        return blocked

    def test_child_in_c_waits_for_exact_parent_while_a_delivers_and_restart_recovers(self) -> None:
        """Two physical targets progress independently across duplicate and restart."""
        parent_id, child_id, a_root_id, c_root_id = self._prepare_nested()
        blocked = self._blocked_c(child_id=child_id, parent_id=parent_id)
        self.assertEqual(self.database.platform_delivery_intents.materialize_ready_blocked(), [])
        # A has a confirmed physical parent; its immediate reply must not wait
        # for C, nor use either forum's root as a substitute parent.
        self.assertTrue(asyncio.run(self.coordinator.stage_and_deliver(
            domain='local', kind='comment', canonical_id=child_id,
            forum=self.forums['A'], operation='message',
            destination=ThreadRef(self.forums['A'], 'created-100'),
            content=ContentDocument('child after parent'),
            presentation=DeliveryPresentation('bot'),
            reply_to=MessageRef('discord-primary', 'created-100', 'physical-parent-A'),
        )))
        self.assertEqual(self.adapter.comments, [('created-100', 'physical-parent-A')])
        # New proof appears in C's *own* root copy after the earlier duplicate.
        with self.database.immediate_session() as session:
            c_root = session.get(LocalCommunityThreadSurface, c_root_id)
            session.add(LocalCommunityMessageSurface(
                local_community_message_id=parent_id,
                local_community_thread_surface_id=c_root_id,
                connector_id=c_root.connector_id,
                channel_surface_id=self.targets['C'][2],
                external_message_id='physical-parent-C', role='local_subscriber',
            ))
        # The next coordinator has no in-memory create history. The DB's
        # immutable request and exact C parent are sufficient for one delivery.
        restarted = PlatformFanoutCoordinator(self.database, _Registry(self.adapter))
        delivered = asyncio.run(restarted.recover_pending())
        self.assertIn(blocked, delivered)
        self.assertEqual(asyncio.run(restarted.recover_pending()), [])
        self.assertEqual(self.adapter.comments, [
            ('created-100', 'physical-parent-A'), ('created-300', 'physical-parent-C'),
        ])
        with self.database.session() as session:
            row = session.get(PlatformDeliveryIntent, blocked)
            self.assertEqual(row.state, 'delivered')
            copies = session.scalars(select(LocalCommunityMessageSurface).where(
                LocalCommunityMessageSurface.local_community_message_id == child_id,
            )).all()
            self.assertEqual({(copy.local_community_thread_surface_id,
                               copy.external_parent_message_id) for copy in copies}, {
                (a_root_id, 'physical-parent-A'), (c_root_id, 'physical-parent-C'),
            })

    def test_unsubscribe_c_before_parent_receipt_cancels_its_pending_child(self) -> None:
        """Unsubscribing C fences its child, but preserves A and old root mappings."""
        parent_id, child_id, a_root_id, c_root_id = self._prepare_nested()
        blocked = self._blocked_c(child_id=child_id, parent_id=parent_id)
        # Revoke the current physical owner without erasing historical receipts.
        with self.database.immediate_session() as session:
            subscriber = session.get(LocalSubscriber, self.targets['C'][1])
            subscriber.status = 'inactive'
            c_root = session.get(LocalCommunityThreadSurface, c_root_id)
            session.add(LocalCommunityMessageSurface(
                local_community_message_id=parent_id,
                local_community_thread_surface_id=c_root_id,
                connector_id=c_root.connector_id,
                channel_surface_id=self.targets['C'][2],
                external_message_id='physical-parent-C', role='local_subscriber',
            ))
        restarted = PlatformFanoutCoordinator(self.database, _Registry(self.adapter))
        self.assertEqual(asyncio.run(restarted.recover_pending()), [])
        self.assertEqual(self.adapter.comments, [])
        with self.database.session() as session:
            # Exact terminal state may be cancelled or kept unclaimable, but
            # an unsubscribed destination must never produce a physical copy.
            row = session.get(PlatformDeliveryIntent, blocked)
            self.assertNotEqual(row.state, 'delivered')
            self.assertIsNotNone(session.get(LocalCommunityThreadSurface, a_root_id))
            self.assertIsNotNone(session.get(LocalCommunityThreadSurface, c_root_id))
            self.assertEqual(session.scalars(select(LocalCommunityMessageSurface).where(
                LocalCommunityMessageSurface.local_community_message_id == child_id,
            )).all(), [])

    def test_accepted_but_unacknowledged_create_is_not_reposted_by_new_process(self) -> None:
        """An unrelated healthy target delivers while a cold process parks C's lost ACK."""
        self.assertTrue(asyncio.run(self._deliver('A')))
        self.adapter.fail_forums.add('300')
        self.assertFalse(asyncio.run(self._deliver('C')))
        self.assertEqual(self.adapter.posts, ['100', '300'])
        root = Path(__file__).resolve().parents[2]
        # Launch a genuinely new interpreter/engine with only the same SQLite
        # path. Its external transport records *every* attempted recovery POST.
        # The previous coordinator, transport and process-local dedup are absent.
        script = """
import asyncio, json, sys
from sqlalchemy import select
from src.db import Database
from src.content_sync.platform_fanout import PlatformFanoutCoordinator
from src.models import PlatformDeliveryIntent, LocalCommunityThreadSurface, ChannelSurface
from tests.behavior.test_platform_message_fanout_scenarios import _ForumAdapter, _Registry

db=Database(sys.argv[1]); adapter=_ForumAdapter()
try:
    coordinator=PlatformFanoutCoordinator(db, _Registry(adapter))
    recovered=asyncio.run(coordinator.recover_pending())
    with db.session() as session:
        states=dict(session.execute(
            select(ChannelSurface.external_channel_id, PlatformDeliveryIntent.state)
            .join(PlatformDeliveryIntent, PlatformDeliveryIntent.channel_surface_id == ChannelSurface.id)
        ).all())
        copies=[row.external_thread_id for row in session.scalars(
            select(LocalCommunityThreadSurface)).all()]
    print(json.dumps({'recovered': recovered, 'posts': adapter.posts,
                      'states': states, 'copies': copies}))
finally:
    db.close()
"""
        completed = subprocess.run(
            [sys.executable, '-c', script, str(self.database.engine.url)],
            cwd=root, env={**os.environ, 'PYTHONPATH': str(root)},
            text=True, capture_output=True, timeout=30, check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout.strip())
        self.assertEqual(result['recovered'], [])
        self.assertEqual(result['posts'], [])
        self.assertEqual(result['states']['300'], 'unresolved')
        self.assertEqual(result['states']['100'], 'delivered')
        self.assertEqual(result['copies'], ['created-100'])
        self.assertEqual(self.adapter.posts, ['100', '300'])


if __name__ == '__main__':
    import unittest
    unittest.main()
