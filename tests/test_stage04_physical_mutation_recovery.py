"""Durable physical mutation scenarios on real SQLite v1 delivery rows."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import select

from src.db import Database
from src.models import (
    ChannelSurface, LocalCommunity, LocalCommunityThread,
    LocalCommunityThreadSurface, PlatformConnector,
)
from src.platform_types import ContentDocument, MessageRef


class PhysicalMutationRecoveryScenarios(unittest.TestCase):
    """Canonical revision drives every verified bot-owned physical copy."""

    def setUp(self) -> None:
        """Use a destination message that already exists, without a Create intent."""
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.db = Database(f"sqlite:///{Path(t.name) / 'bridge.db'}")
        self.addCleanup(self.db.close)
        self.db.migrate()
        with self.db.immediate_session() as session:
            community = LocalCommunity(
                slug='local', display_name='Local',
                actor_url='https://bridge.test/communities/local',
                inbox_url='https://bridge.test/communities/local/inbox',
                outbox_url='https://bridge.test/communities/local/outbox',
                followers_url='https://bridge.test/communities/local/followers',
                public_key_pem='public', private_key_pem='private', status='active',
            )
            session.add(community)
            session.flush()
            connector = session.scalar(select(PlatformConnector).where(
                PlatformConnector.connector_key == 'discord-primary'))
            surface = ChannelSurface(
                connector_id=connector.id, external_channel_id='500',
                external_guild_id='1', kind='forum',
                local_community_id=community.id,
            )
            session.add(surface)
            session.flush()
            post = LocalCommunityThread(
                local_community_id=community.id,
                ap_activity_id='https://bridge.test/create/1',
                ap_object_id='https://remote.test/post/1',
                origin_kind='remote_follower', direction='ap_to_discord',
                content_revision=1,
                current_content_json=json.dumps({
                    'schemaVersion': 1, 'kind': 'post',
                    'apObjectId': 'https://remote.test/post/1',
                    'authorName': 'Alice',
                    'authorActorId': 'https://remote.test/users/alice',
                    'title': 'Changed', 'bodyMarkdown': 'latest',
                }),
            )
            session.add(post)
            session.flush()
            copy = LocalCommunityThreadSurface(
                local_community_thread_id=post.id,
                connector_id=connector.id, channel_surface_id=surface.id,
                external_thread_id='900', external_starter_message_id='901',
                discord_forum_channel_id=500, discord_thread_id=900,
                discord_starter_message_id=901,
                role='host', applied_revision=None,
            )
            session.add(copy)
            session.flush()
            self.copy_id = copy.id
            self.post_id = post.id
            self.surface_id = surface.id

    def _worker(self, *, fail: bool = False):
        """Use a typed registry with a deterministic SDK-boundary fake."""
        from src.content_sync.platform_mutations import PlatformPhysicalMutationCoordinator
        calls = []
        class Adapter:
            """Expose exact typed message edit/delete boundary."""
            async def edit_message(self, message, content):
                """Record one physical edit, possibly losing the ACK."""
                calls.append(('edit', message, content.body))
                if fail:
                    raise ConnectionError('lost edit response')
            async def delete_message(self, message):
                """Record the exact physical delete."""
                calls.append(('delete', message, None))
                if fail:
                    raise ConnectionError('lost delete response')
        class Registry:
            """Refuse fallback adapters from other connectors."""
            def get(self, key):
                """Return only the explicitly registered fake connector."""
                if key != 'discord-primary':
                    raise AssertionError('wrong connector')
                return Adapter()
        worker = PlatformPhysicalMutationCoordinator(
            self.db, Registry(),
            target_policy=lambda connector_key, surface_id: connector_key == 'discord-primary' and surface_id == self.surface_id,
            content_renderer=lambda _domain, _kind, snapshot: ContentDocument(snapshot['bodyMarkdown']),
        )
        return worker, calls

    def test_migrated_copy_gets_update_without_create_intent_and_one_ack(self) -> None:
        """A v0 copy's unknown applied baseline is not a reason to skip Update."""
        worker, calls = self._worker()
        self.assertEqual(asyncio.run(worker.drain()), 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], 'edit')
        self.assertEqual(calls[0][1], MessageRef('discord-primary', '900', '901'))
        with self.db.session() as session:
            copy = session.get(LocalCommunityThreadSurface, self.copy_id)
            self.assertEqual(copy.applied_revision, 1)
            self.assertEqual(copy.mutation_state, 'idle')
        self.assertEqual(asyncio.run(worker.drain()), 0)
        self.assertEqual(len(calls), 1)

    def test_unknown_edit_survives_restart_without_second_physical_send(self) -> None:
        """An unknown edit is a durable barrier, even if its lease expires."""
        worker, calls = self._worker(fail=True)
        self.assertEqual(asyncio.run(worker.drain()), 1)
        self.assertEqual(len(calls), 1)
        with self.db.session() as session:
            copy = session.get(LocalCommunityThreadSurface, self.copy_id)
            self.assertEqual(copy.mutation_state, 'unresolved')
            self.assertIsNone(copy.applied_revision)
        new_worker, new_calls = self._worker()
        self.assertEqual(asyncio.run(new_worker.drain()), 0)
        self.assertEqual(new_calls, [])

    def test_policy_refusal_before_edit_is_definitely_unsent_and_retryable(self) -> None:
        """Reject before I/O without making an unresolvable transport claim."""
        worker, calls = self._worker()
        worker.target_policy = lambda _connector, _surface: False
        self.assertEqual(asyncio.run(worker.drain()), 1)
        self.assertEqual(calls, [])
        with self.db.session() as session:
            copy = session.get(LocalCommunityThreadSurface, self.copy_id)
            self.assertEqual(copy.mutation_state, 'failed_definite')
            self.assertIsNone(copy.applied_revision)
        worker.target_policy = lambda _connector, _surface: True
        self.assertEqual(asyncio.run(worker.drain()), 1)
        self.assertEqual([item[0] for item in calls], ['edit'])
        with self.db.session() as session:
            copy = session.get(LocalCommunityThreadSurface, self.copy_id)
            self.assertEqual(copy.mutation_state, 'idle')
            self.assertEqual(copy.applied_revision, 1)

    def test_rebound_surface_never_gets_mutation(self) -> None:
        """An historical physical receipt is not a current owner claim."""
        with self.db.immediate_session() as session:
            session.get(ChannelSurface, self.surface_id).local_community_id = None
        worker, calls = self._worker()
        self.assertEqual(asyncio.run(worker.drain()), 0)
        self.assertEqual(calls, [])

    def test_confirmed_delete_never_restores_deleted_copy(self) -> None:
        """A canonical tombstone must select physical Delete, not stale text."""
        from datetime import datetime, timezone
        with self.db.immediate_session() as session:
            post = session.get(LocalCommunityThread, self.post_id)
            post.deleted_at = datetime.now(timezone.utc)
            post.content_revision = 2
        worker, calls = self._worker()
        self.assertEqual(asyncio.run(worker.drain()), 1)
        self.assertEqual([kind for kind, _ref, _content in calls], ['delete'])
        with self.db.session() as session:
            copy = session.get(LocalCommunityThreadSurface, self.copy_id)
            self.assertEqual(copy.applied_revision, 2)
        self.assertEqual(asyncio.run(worker.drain()), 0)

    def test_expired_dispatch_becomes_sticky_unresolved(self) -> None:
        """A process death after claim must not become a retryable edit."""
        from datetime import datetime, timedelta, timezone
        with self.db.immediate_session() as session:
            copy = session.get(LocalCommunityThreadSurface, self.copy_id)
            copy.mutation_state = 'dispatching'
            copy.mutation_target_revision = 1
            copy.mutation_claim_token = 'crashed-worker'
            copy.mutation_lease_until = datetime.now(timezone.utc) - timedelta(days=1)
        worker, calls = self._worker()
        self.assertEqual(asyncio.run(worker.drain()), 0)
        self.assertEqual(calls, [])
        with self.db.session() as session:
            copy = session.get(LocalCommunityThreadSurface, self.copy_id)
            self.assertEqual(copy.mutation_state, 'unresolved')
            self.assertIsNone(copy.applied_revision)

    def test_old_claim_refuses_update_after_canonical_revision_changes(self) -> None:
        """A committed newer canonical revision fences stale transport."""
        worker, calls = self._worker()
        claims = worker._claim_due(1)
        self.assertEqual(len(claims), 1)
        with self.db.immediate_session() as session:
            session.get(LocalCommunityThread, self.post_id).content_revision = 2
        self.assertFalse(worker._revalidate(claims[0]))
        self.assertEqual(calls, [])
        # A possibly claimed edit cannot automatically be retargeted.
        self.assertEqual(asyncio.run(worker.drain()), 0)

    def test_tombstone_renderer_edits_instead_of_deleting_forum_starter(self) -> None:
        """A tombstone is an edit; deleting a starter can destroy history."""
        from datetime import datetime, timezone
        from src.content_sync.platform_mutations import PlatformPhysicalMutationCoordinator
        with self.db.immediate_session() as session:
            post = session.get(LocalCommunityThread, self.post_id)
            post.deleted_at = datetime.now(timezone.utc)
            post.content_revision = 2
        worker, calls = self._worker()
        worker.tombstone_renderer = lambda _d, _k: ContentDocument('*deleted by creator*')
        self.assertEqual(asyncio.run(worker.drain()), 1)
        self.assertEqual([call[0] for call in calls], ['edit'])
        self.assertEqual(calls[0][2], '*deleted by creator*')
        with self.db.session() as session:
            copy = session.get(LocalCommunityThreadSurface, self.copy_id)
            self.assertEqual(copy.applied_revision, 2)

    def test_unproven_baseline_does_not_edit_at_revision_zero(self) -> None:
        """Migrated v0 copies do not have reconstructed original content."""
        with self.db.immediate_session() as session:
            post = session.get(LocalCommunityThread, self.post_id)
            post.content_revision = 0
            post.current_content_json = None
        worker, calls = self._worker()
        self.assertEqual(asyncio.run(worker.drain()), 0)
        self.assertEqual(calls, [])


if __name__ == '__main__':
    unittest.main()
