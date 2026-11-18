"""Cross-target reply ordering and unsubscribe decisions with real SQLite claims.

The provider adapter is the only fake. Reservations, materialization, durable
receipts, scope checks and coordinator restart use their production classes.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import json
import os
import signal
import time
from pathlib import Path
import subprocess
import unittest
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

    @unittest.skipUnless(os.name == 'posix', 'the crash barrier requires POSIX SIGKILL')
    def test_crash_inside_accepted_create_before_ack_never_reposts(self) -> None:
        """A killed worker leaves its committed claim uncertain, not retryable."""
        self.assertTrue(asyncio.run(self._deliver('A')))
        self.assertEqual(self.adapter.posts, ['100'])
        root = Path(__file__).resolve().parents[2]
        db_path = str(self.database.engine.url)
        side_effect = Path(self.database.engine.url.database).with_suffix('.physical.jsonl')
        prepublish = side_effect.with_suffix('.staged')
        release = side_effect.with_suffix('.release')
        # The subprocess executes the real coordinator and a committed
        # dispatch claim. Its controlled provider publishes the fsynced Create
        # atomically, then withholds the ACK. SIGKILL interrupts that in-flight
        # operation, not a later recovery attempt.
        child_script = """
import asyncio, json, os, sys, tempfile
from pathlib import Path
from src.db import Database
from src.content_sync.platform_fanout import PlatformFanoutCoordinator
from src.platform_types import ChannelRef, ContentDocument, DeliveryPresentation
from tests.behavior.test_platform_message_fanout_scenarios import _Registry

class AcceptedThenKilled:
    '''Record an accepted external Create and deliberately withhold its ACK.'''

    async def create_forum_thread(self, forum, content, *, title, presentation, attempt):
        '''Publish a complete fsynced effect before withholding the SDK ACK.'''
        effect = Path(sys.argv[4])
        # The active acceptance marker must not exist until its complete
        # payload has been fsynced; opening the temporary file is not proof.
        with tempfile.NamedTemporaryFile(
            mode='w', encoding='utf-8', dir=effect.parent,
            prefix=effect.name + '.', suffix='.pending', delete=False,
        ) as physical:
            # The parent deliberately pauses the provider between opening and
            # writing, reproducing the scheduler window from F3.
            with open(sys.argv[5], 'w', encoding='utf-8') as staged:
                staged.write('ready')
                staged.flush()
                os.fsync(staged.fileno())
            while not os.path.exists(sys.argv[6]):
                await asyncio.sleep(0.01)
            physical.write(json.dumps({'attempt':attempt.key,
                'forum':forum.external_channel_id}) + '\\n')
            physical.flush()
            os.fsync(physical.fileno())
        os.replace(physical.name, effect)
        await asyncio.Event().wait()  # Provider accepted Create; acknowledgement withheld.

db=Database(sys.argv[1]); forum=ChannelRef('discord-primary', sys.argv[2], 'forum', '1')
coordinator=PlatformFanoutCoordinator(db, _Registry(AcceptedThenKilled()))
asyncio.run(coordinator.stage_and_deliver(domain='local', kind='post',
    canonical_id=int(sys.argv[3]), forum=forum, operation='forum_thread',
    destination=forum, content=ContentDocument('human post from B'),
    presentation=DeliveryPresentation('bot'), title='B source'))
sys.exit(91)  # Unreachable unless the controlled provider returned an ACK.
"""
        child = subprocess.Popen(
            [sys.executable, '-c', child_script, db_path, '300',
             str(self.canonical_id), str(side_effect), str(prepublish), str(release)],
            cwd=root, env={**os.environ, 'PYTHONPATH': str(root)},
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            deadline = time.monotonic() + 15
            # The provider is deliberately suspended after opening a temporary
            # effect file. The final acceptance marker must still be absent.
            while time.monotonic() < deadline:
                self.assertIsNone(child.poll(), 'provider exited before reaching publication')
                if prepublish.exists() and prepublish.read_text(encoding='utf-8') == 'ready':
                    break
                time.sleep(0.01)
            self.assertTrue(prepublish.exists(), 'provider never reached prepublication barrier')
            self.assertEqual(prepublish.read_text(encoding='utf-8'), 'ready')
            self.assertIsNone(child.poll(), 'provider exited before physical Create')
            self.assertFalse(side_effect.exists(), 'an incomplete physical record was published')
            release.write_text('go', encoding='utf-8')
            # Only a complete, atomically published record may trigger SIGKILL.
            while not side_effect.exists() and time.monotonic() < deadline:
                self.assertIsNone(child.poll(), 'provider exited before accepting Create')
                time.sleep(0.01)
            self.assertTrue(side_effect.exists(), 'provider never published Create')
            accepted_before_kill = [json.loads(line) for line in side_effect.read_text().splitlines()]
            self.assertEqual(len(accepted_before_kill), 1)
            self.assertEqual(accepted_before_kill[0]['forum'], '300')
            self.assertEqual(list(side_effect.parent.glob(side_effect.name + '.*.pending')), [])
            self.assertIsNone(child.poll(), 'the provider should still be awaiting ACK')
            # Kill *from another process* during the unresolved SDK call.
            child.kill()
            _stdout, stderr = child.communicate(timeout=10)
            self.assertEqual(child.returncode, -signal.SIGKILL, stderr)
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=10)
            prepublish.unlink(missing_ok=True)
            release.unlink(missing_ok=True)
        accepted = [json.loads(line) for line in side_effect.read_text().splitlines()]
        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0]['forum'], '300')
        with self.database.session() as session:
            rows = session.scalars(select(PlatformDeliveryIntent)).all()
            states = {row.channel_surface_id: row.state for row in rows}
            self.assertEqual(states[self.targets['A'][2]], 'delivered')
            self.assertEqual(states[self.targets['C'][2]], 'dispatching')
            c = next(row for row in rows if row.channel_surface_id == self.targets['C'][2])
            self.assertIsNotNone(c.dispatch_token)
            self.assertEqual(c.attempt_key, accepted[0]['attempt'])

        # The original process no longer has an adapter cache or execution
        # history. A new Python interpreter must not infer a missing POST.
        restart_script = """
import asyncio, json, sys
from sqlalchemy import select
from src.db import Database
from src.content_sync.platform_fanout import PlatformFanoutCoordinator
from src.models import PlatformDeliveryIntent, ChannelSurface
from tests.behavior.test_platform_message_fanout_scenarios import _ForumAdapter, _Registry

db=Database(sys.argv[1]); adapter=_ForumAdapter()
try:
    recovered=asyncio.run(PlatformFanoutCoordinator(db, _Registry(adapter)).recover_pending())
    with db.session() as session:
        states=dict(session.execute(select(ChannelSurface.external_channel_id,
            PlatformDeliveryIntent.state).join(PlatformDeliveryIntent,
            PlatformDeliveryIntent.channel_surface_id == ChannelSurface.id)).all())
    print(json.dumps({'recovered':recovered, 'posts':adapter.posts, 'states':states}))
finally:
    db.close()
"""
        def restarted_state() -> dict:
            """Read one cold process recovery decision and its physical effects."""
            completed = subprocess.run(
                [sys.executable, '-c', restart_script, db_path],
                cwd=root, env={**os.environ, 'PYTHONPATH': str(root)},
                text=True, capture_output=True, timeout=25, check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            return json.loads(completed.stdout.strip())

        first = restarted_state()
        self.assertEqual(first['posts'], [])
        self.assertEqual(first['recovered'], [])
        self.assertEqual(first['states'], {'100': 'delivered', '300': 'dispatching'})
        # Expiry is not evidence of failed delivery: force only the clock
        # boundary and run a second fresh process to prove the durable fence.
        with self.database.immediate_session() as session:
            c = session.scalar(select(PlatformDeliveryIntent).where(
                PlatformDeliveryIntent.channel_surface_id == self.targets['C'][2],
            ))
            c.lease_until = datetime.now(UTC) - timedelta(seconds=1)
        second = restarted_state()
        self.assertEqual(second['posts'], [])
        self.assertEqual(second['recovered'], [])
        self.assertEqual(second['states'], {'100': 'delivered', '300': 'unresolved'})
        self.assertEqual(len(side_effect.read_text().splitlines()), 1)
        self.assertEqual(self.adapter.posts, ['100'])


if __name__ == '__main__':
    import unittest
    unittest.main()
