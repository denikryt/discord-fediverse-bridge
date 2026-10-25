"""Exact AP reply-parent placement on current-v1 SQLite physical surfaces."""
from __future__ import annotations

import unittest

from sqlalchemy import select

from src.content_sync.parent_mapping import MissingParent, resolve_target_parent
from src.models import (LocalCommunityMessage, LocalCommunityMessageSurface,
                        LocalCommunityThreadSurface, CommunityThreadGroup,
                        CommunityThreadGroupDelivery, CommunityMessageGroup,
                        CommunityMessageGroupDelivery, ChannelSurface)
from src.platform_types import ChannelRef, MessageRef, ThreadRef, CreatedThread
import test_community_delivery_intents as _intent_scenarios


class ParentMappingScenarios(unittest.TestCase):
    """Reuse a real SQLite host claim without duplicating intent tests."""

    setUp = _intent_scenarios.PhysicalCreateIntentScenarios.setUp
    _repo = _intent_scenarios.PhysicalCreateIntentScenarios._repo
    _prepare = _intent_scenarios.PhysicalCreateIntentScenarios._prepare

    def _create_verified_root(self):
        """Commit an actual root thread/starter pair to a concrete forum."""
        intent_id = self._prepare()
        claim = self._repo().claim(intent_id)
        self.assertTrue(self._repo().commit_receipt(
            intent_id, token=claim.token,
            receipt=CreatedThread(ThreadRef(self.forum, '987'),
                                  MessageRef('discord-primary', '987', '654'), 'bot')))
        return ThreadRef(self.forum, '987')

    def test_root_uses_exact_starter_and_rejects_wrong_thread(self):
        """A root comment links to a starter in the target immediate container."""
        target = self._create_verified_root()
        with self.db.session() as session:
            reply = resolve_target_parent(
                session, domain='local', canonical_thread_id=self.canonical,
                target=target, target_surface_id=self.surface,
                canonical_parent_message_id=None)
            self.assertEqual(reply, MessageRef('discord-primary', '987', '654'))
            with self.assertRaises(ValueError):
                resolve_target_parent(
                    session, domain='local', canonical_thread_id=self.canonical,
                    target=ThreadRef(self.forum, 'foreign-thread'),
                    target_surface_id=self.surface, canonical_parent_message_id=None)

    def test_nested_known_missing_parent_waits_and_late_receipt_resolves(self):
        """An absent physical parent is a blocking dependency, not root fallback."""
        target = self._create_verified_root()
        with self.db.immediate_session() as session:
            row = LocalCommunityMessage(
                local_community_thread_id=self.canonical,
                ap_activity_id='https://bridge.test/a/reply',
                ap_object_id='https://bridge.test/o/reply', direction='inbound')
            session.add(row); session.flush(); parent_id = row.id
        with self.db.session() as session:
            self.assertEqual(resolve_target_parent(
                session, domain='local', canonical_thread_id=self.canonical,
                target=target, target_surface_id=self.surface,
                canonical_parent_message_id=parent_id),
                MissingParent(parent_id, 'target_comment_receipt_missing'))
        with self.db.immediate_session() as session:
            thread_surface = session.scalar(select(LocalCommunityThreadSurface).where(
                LocalCommunityThreadSurface.local_community_thread_id == self.canonical))
            session.add(LocalCommunityMessageSurface(
                connector_id=thread_surface.connector_id,
                channel_surface_id=thread_surface.channel_surface_id,
                local_community_message_id=parent_id,
                local_community_thread_surface_id=thread_surface.id,
                external_message_id='900', role='host'))
        with self.db.session() as session:
            self.assertEqual(resolve_target_parent(
                session, domain='local', canonical_thread_id=self.canonical,
                target=target, target_surface_id=self.surface,
                canonical_parent_message_id=parent_id),
                MessageRef('discord-primary', '987', '900'))
            with self.assertRaises(ValueError):
                resolve_target_parent(
                    session, domain='local', canonical_thread_id=self.canonical,
                    target=target, target_surface_id=self.surface,
                    canonical_parent_message_id=parent_id + 100)

    def test_connector_or_placement_mismatch_rejected(self):
        """Same physical IDs cannot bypass a different connector or target forum."""
        target = self._create_verified_root()
        with self.db.session() as session:
            with self.assertRaises(ValueError):
                resolve_target_parent(
                    session, domain='local', canonical_thread_id=self.canonical,
                    target=ThreadRef(ChannelRef('discord-primary', 'not-321', 'forum', '1'), '987'),
                    target_surface_id=self.surface,
                    canonical_parent_message_id=None)
            with self.assertRaises(ValueError):
                resolve_target_parent(
                    session, domain='remote', canonical_thread_id=self.canonical,
                    target=target, target_surface_id=self.surface,
                    canonical_parent_message_id=None)


if __name__ == '__main__':
    unittest.main()
