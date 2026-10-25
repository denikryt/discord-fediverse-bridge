"""Checkpoint 63: restart-safe physical Create recovery and negative receipts.

These tests retain real SQLite state while replacing only Discord HTTP/SDK IO
with a deterministic adapter. They never treat a missing response as unsent.
"""
from __future__ import annotations

import asyncio
import json
import unittest

from sqlalchemy import select

from src.content_sync.platform_fanout import PlatformFanoutCoordinator
from src.db.repositories.platform_delivery_intents import decode_request
from src.models import (
    ChannelSurface, LocalCommunityThreadSurface, LocalCommunityMessage,
    LocalCommunityMessageSurface, PlatformDeliveryIntent,
)
from src.platform_types import (
    ChannelRef, ContentDocument, CreateAttempt, CreatedThread,
    DeliveryPresentation, MessageRef, ThreadRef, PlatformOperationError,
)
from .test_platform_message_fanout_scenarios import _ThreeForumFixture, _Registry


class PhysicalDeliveryRestartScenarios(_ThreeForumFixture):
    """Persisted owner and attempt identity must survive interrupted workers."""

    def _pending(self, label='C') -> int:
        """Commit one complete immutable Create without touching the adapter."""
        forum = self.forums[label]
        owner_kind, owner_id, surface_id = self.targets[label]
        key = (f'fanout:v1:local:post:{self.canonical_id}:'
               f'discord-primary:{surface_id}')
        attempt = CreateAttempt(key, 'discord-primary', 'forum_thread', forum)
        row = self.database.platform_delivery_intents.reserve(
            domain='local', kind='post', canonical_id=self.canonical_id,
            forum=forum, channel_surface_id=surface_id,
            owner_kind=owner_kind, owner_id=owner_id, attempt=attempt,
            content=ContentDocument('human post from B'),
            presentation=DeliveryPresentation('bot'), title='B source',
        )
        return row.id

    def test_nested_comment_wake_rehydrates_only_after_same_target_parent_receipt(self):
        """Restart materializes an exact parent-linked immutable comment body."""
        # The root thread itself is a previously proven bot create in forum A.
        self.assertTrue(asyncio.run(self._deliver('A')))
        owner_kind, owner_id, surface_id = self.targets['A']
        with self.database.immediate_session() as session:
            root = session.scalar(select(LocalCommunityThreadSurface).where(
                LocalCommunityThreadSurface.local_community_thread_id == self.canonical_id,
                LocalCommunityThreadSurface.channel_surface_id == surface_id,
            ))
            parent = LocalCommunityMessage(
                local_community_thread_id=self.canonical_id,
                ap_activity_id='https://remote.test/a/nested-parent',
                ap_object_id='https://remote.test/o/nested-parent',
                direction='inbound',
            )
            session.add(parent)
            session.flush()
            child = LocalCommunityMessage(
                local_community_thread_id=self.canonical_id,
                ap_activity_id='https://remote.test/a/nested-child',
                ap_object_id='https://remote.test/o/nested-child',
                parent_ap_object_id=parent.ap_object_id,
                direction='inbound',
                current_content_json=json.dumps({
                    'schemaVersion': 1, 'kind': 'comment',
                    'apObjectId': 'https://remote.test/o/nested-child',
                    'bodyMarkdown': 'hello nested',
                    'authorActorId': 'https://remote.test/u/author',
                    'authorName': 'Author', 'url': 'https://remote.test/o/nested-child',
                }),
            )
            session.add(child)
            session.flush()
            child_id, parent_id, root_id = child.id, parent.id, root.id
            connector_id = root.connector_id
            physical_thread = root.external_thread_id
        blocked = self.database.platform_delivery_intents.reserve(
            domain='local', kind='comment', canonical_id=child_id,
            forum=self.forums['A'], channel_surface_id=surface_id,
            owner_kind=owner_kind, owner_id=owner_id,
            blocked_parent_canonical_id=parent_id,
        )
        self.assertEqual(blocked.state, 'blocked')
        self.assertEqual(self.database.platform_delivery_intents.materialize_ready_blocked(), [])
        with self.database.immediate_session() as session:
            session.add(LocalCommunityMessageSurface(
                local_community_message_id=parent_id,
                local_community_thread_surface_id=root_id,
                connector_id=connector_id, channel_surface_id=surface_id,
                external_message_id='actual-parent-445', role='host',
            ))
        self.assertEqual(self.database.platform_delivery_intents.materialize_ready_blocked(), [blocked.id])
        with self.database.session() as session:
            materialized = session.get(PlatformDeliveryIntent, blocked.id)
            self.assertEqual(materialized.state, 'pending')
            attempt, content, presentation, title, reply_to = decode_request(materialized.request_json)
            self.assertEqual(attempt.operation, 'message')
            self.assertEqual(content.body, '`Author@remote.test`\n\nhello nested')
            self.assertEqual(reply_to, MessageRef('discord-primary', physical_thread, 'actual-parent-445'))
        # Never regenerate a second immutable request after successful wake.
        self.assertEqual(self.database.platform_delivery_intents.materialize_ready_blocked(), [])

    def test_reboot_drains_pending_only_once(self):
        """An unsent pending intent can be delivered after the first worker died."""
        ident = self._pending()
        with self.database.session() as session:
            self.assertEqual(session.get(PlatformDeliveryIntent, ident).state,
                             'pending')
        worker = PlatformFanoutCoordinator(self.database, _Registry(self.adapter))
        self.assertEqual(asyncio.run(worker.recover_pending()), [ident])
        self.assertEqual(asyncio.run(worker.recover_pending()), [])
        self.assertEqual(self.adapter.posts, ['300'])

    def test_uncertain_previous_post_is_sticky_across_worker_restart(self):
        """A response lost after POST never authorizes a second send."""
        self.adapter.fail_forums.add('300')
        self.assertFalse(asyncio.run(self._deliver('C')))
        first_calls = list(self.adapter.posts)
        coordinator = PlatformFanoutCoordinator(self.database, _Registry(self.adapter))
        self.assertEqual(asyncio.run(coordinator.recover_pending()), [])
        self.assertEqual(self.adapter.posts, first_calls)
        with self.database.session() as session:
            intent = session.scalar(select(PlatformDeliveryIntent).where(
                PlatformDeliveryIntent.channel_surface_id == self.targets['C'][2]))
            self.assertEqual(intent.state, 'unresolved')

    def test_wrong_container_receipt_cannot_create_a_copy_or_retry(self):
        """A syntactically valid receipt in a foreign forum is not delivery proof."""
        ident = self._pending('A')
        claim = self.database.platform_delivery_intents.claim(ident)
        unrelated = ChannelRef('discord-primary', '999', 'forum', '1')
        wrong = CreatedThread(ThreadRef(unrelated, 'created-999'),
                              MessageRef('discord-primary', 'created-999', 'foreign'),
                              'bot')
        with self.assertRaises(PlatformOperationError):
            self.database.platform_delivery_intents.commit_receipt(
                ident, token=claim.token, receipt=wrong,
            )
        with self.database.session() as session:
            self.assertEqual(session.scalars(select(LocalCommunityThreadSurface)).all(), [])
            self.assertEqual(session.get(PlatformDeliveryIntent, ident).state,
                             'dispatching')
        self.assertTrue(self.database.platform_delivery_intents.mark_unresolved(
            ident, token=claim.token, reason='invalid_receipt'))
        self.assertEqual(asyncio.run(self.coordinator.recover_pending()), [])
        self.assertEqual(self.adapter.posts, [])

    def test_reassigned_forum_returns_operator_orphan_with_both_ids(self):
        """A verified thread created before target rebind cannot belong to new owner."""
        ident = self._pending('C')
        claim = self.database.platform_delivery_intents.claim(ident)
        with self.database.immediate_session() as session:
            surface = session.get(ChannelSurface, self.targets['C'][2])
            surface.local_subscriber_id = None
        receipt = CreatedThread(
            ThreadRef(self.forums['C'], 'created-300'),
            MessageRef('discord-primary', 'created-300', 'starter-created-300'),
            'bot',
        )
        self.assertTrue(self.database.platform_delivery_intents.commit_receipt(
            ident, token=claim.token, receipt=receipt,
        ))
        self.assertEqual(asyncio.run(self.coordinator.recover_pending()), [])
        inventory = self.database.platform_delivery_intents.inspect_orphans()
        self.assertEqual(len(inventory), 1)
        self.assertEqual(inventory[0]['intent_id'], ident)
        self.assertIn('created-300', str(inventory[0]['receipt']))
        self.assertIn('starter-created-300', str(inventory[0]['receipt']))
        with self.database.session() as session:
            self.assertEqual(session.scalars(select(LocalCommunityThreadSurface)).all(), [])
        self.assertEqual(self.adapter.posts, [])


if __name__ == '__main__':
    unittest.main()
