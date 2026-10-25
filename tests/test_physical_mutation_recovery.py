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
from src.platform_types import AuthorRef, ContentDocument, MessageRef, MessageView


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
                actor_url='https://bridge.test/actors/communities/local',
                inbox_url='https://bridge.test/actors/communities/local/inbox',
                outbox_url='https://bridge.test/actors/communities/local/outbox',
                followers_url='https://bridge.test/actors/communities/local/followers',
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
            async def fetch_message(self, message):
                """Resolve this scenario's exact bot-owned physical copy."""
                return MessageView(message, AuthorRef(message.connector_key, 'bot', 'bot', 'Bridge', True))
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

    def test_adapter_lookup_failure_before_send_can_retry(self) -> None:
        """Absent connector binding cannot produce a remote effect or sticky unknown."""
        worker, calls = self._worker()
        healthy_registry = worker.registry

        class UnavailableRegistry:
            """Fail before an SDK method or HTTP request has been reached."""
            def get(self, connector_key):
                """Refuse a transport adapter until the connector is registered."""
                raise LookupError(f'connector not ready: {connector_key}')

        worker.registry = UnavailableRegistry()
        self.assertEqual(asyncio.run(worker.drain()), 1)
        self.assertEqual(calls, [])
        with self.db.session() as session:
            copy = session.get(LocalCommunityThreadSurface, self.copy_id)
            self.assertEqual(copy.mutation_state, 'failed_definite')
            self.assertIsNone(copy.applied_revision)
        # Registering the same connector recovers the exact original copy.
        worker.registry = healthy_registry
        self.assertEqual(asyncio.run(worker.drain()), 1)
        self.assertEqual([call[0] for call in calls], ['edit'])

    def test_tombstone_formatting_failure_never_marks_unknown(self) -> None:
        """A pure local formatter refusal cannot be mistaken for a lost HTTP ACK."""
        from datetime import datetime, timezone
        with self.db.immediate_session() as session:
            post = session.get(LocalCommunityThread, self.post_id)
            post.deleted_at = datetime.now(timezone.utc)
            post.content_revision = 2
        worker, calls = self._worker()

        def invalid_tombstone(_domain, _kind):
            """Reject the delete presentation before calling the platform."""
            raise ValueError('unsupported tombstone presentation')

        worker.tombstone_renderer = invalid_tombstone
        self.assertEqual(asyncio.run(worker.drain()), 1)
        self.assertEqual(calls, [])
        with self.db.session() as session:
            copy = session.get(LocalCommunityThreadSurface, self.copy_id)
            self.assertEqual(copy.mutation_state, 'failed_definite')
            self.assertIsNone(copy.applied_revision)
        worker.tombstone_renderer = lambda _domain, _kind: ContentDocument('*deleted*')
        self.assertEqual(asyncio.run(worker.drain()), 1)
        self.assertEqual([(kind, ref) for kind, ref, _ in calls],
                         [('edit', MessageRef('discord-primary', '900', '901'))])
        with self.db.session() as session:
            copy = session.get(LocalCommunityThreadSurface, self.copy_id)
            self.assertEqual(copy.applied_revision, 2)

    def test_policy_error_before_send_is_retryable_without_io(self) -> None:
        """A failing authorization backend never creates ambiguous transport."""
        worker, calls = self._worker()
        def unavailable_policy(_connector, _surface):
            """Represent temporary failure to resolve connector policy."""
            raise LookupError('policy lookup unavailable')
        worker.target_policy = unavailable_policy
        self.assertEqual(asyncio.run(worker.drain()), 1)
        self.assertEqual(calls, [])
        with self.db.session() as session:
            self.assertEqual(session.get(LocalCommunityThreadSurface, self.copy_id).mutation_state,
                             'failed_definite')
        worker.target_policy = lambda _connector, _surface: True
        self.assertEqual(asyncio.run(worker.drain()), 1)
        self.assertEqual(len(calls), 1)

    def test_cancelled_batch_releases_only_unstarted_mutations(self) -> None:
        """A cancelled worker must not strand work whose transport never began.

        An already-invoked edit remains potentially sent; independently
        reserved later copies can be proven unsent by this same worker.
        """
        later_copy_id = self._add_older_settled_copies_and_new_lagging_copy()
        with self.db.immediate_session() as session:
            session.get(LocalCommunityThreadSurface, self.copy_id).applied_revision = None
        from src.content_sync.platform_mutations import PlatformPhysicalMutationCoordinator
        entered = asyncio.Event()
        wire_calls = []

        class PausedAdapter:
            """Simulate cancellation after the first external send begins."""
            async def fetch_message(self, message):
                """Resolve this scenario's exact bot-owned physical copy."""
                return MessageView(message, AuthorRef(message.connector_key, 'bot', 'bot', 'Bridge', True))
            async def edit_message(self, message, _content):
                """Only the first copy may cross the adapter boundary."""
                wire_calls.append(message)
                entered.set()
                await asyncio.Event().wait()
            async def delete_message(self, message):
                """A test edit must never be changed into a delete."""
                raise AssertionError('unexpected physical delete')

        class PausedRegistry:
            """Supply a precise typed connector during the first batch."""
            def get(self, connector_key):
                """Refuse any other connector silently being selected."""
                self_key = 'discord-primary'
                if connector_key != self_key:
                    raise AssertionError('wrong connector')
                return PausedAdapter()

        worker = PlatformPhysicalMutationCoordinator(
            self.db, PausedRegistry(),
            target_policy=lambda _connector, _surface: True,
            content_renderer=lambda _domain, _kind, snapshot: ContentDocument(snapshot['bodyMarkdown']),
        )

        async def interrupted():
            """Interrupt after one I/O entry but before another starts."""
            task = asyncio.create_task(worker.drain(limit=2))
            await asyncio.wait_for(entered.wait(), 5)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        asyncio.run(interrupted())
        self.assertEqual(wire_calls, [MessageRef('discord-primary', '900', '901')])
        with self.db.session() as session:
            current = session.get(LocalCommunityThreadSurface, self.copy_id)
            never_started = session.get(LocalCommunityThreadSurface, later_copy_id)
            self.assertEqual(current.mutation_state, 'dispatching')
            self.assertEqual(never_started.mutation_state, 'failed_definite')
            self.assertIsNone(never_started.applied_revision)
        # Next worker may retry only the definitely-unsent second copy.
        restored, restored_calls = self._worker()
        self.assertEqual(asyncio.run(restored.drain(limit=1)), 1)
        self.assertEqual(restored_calls[0][1].external_container_id, 'lagging-thread')

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

    def _add_older_settled_copies_and_new_lagging_copy(self) -> int:
        """Place a legitimate lagging copy behind eight already-settled rows.

        The worker's batch limit applies to actual claimable operations, not
        the first physical-copy rows that happen to be indexed by SQLite.
        """
        with self.db.immediate_session() as session:
            initial_copy = session.get(LocalCommunityThreadSurface, self.copy_id)
            initial_copy.applied_revision = 1
            connector_id = initial_copy.connector_id
            community_id = session.get(LocalCommunityThread, self.post_id).local_community_id
            for index in range(7):
                post = LocalCommunityThread(
                    local_community_id=community_id,
                    ap_activity_id=f'https://bridge.test/create/settled-{index}',
                    ap_object_id=f'https://bridge.test/post/settled-{index}',
                    origin_kind='remote_follower', direction='ap_to_discord',
                    content_revision=1,
                )
                session.add(post)
                session.flush()
                session.add(LocalCommunityThreadSurface(
                    local_community_thread_id=post.id,
                    connector_id=connector_id, channel_surface_id=self.surface_id,
                    external_thread_id=f'settled-{index}',
                    external_starter_message_id=f'settled-msg-{index}',
                    role='host', applied_revision=1,
                ))
            lagging = LocalCommunityThread(
                local_community_id=community_id,
                ap_activity_id='https://bridge.test/create/lagging',
                ap_object_id='https://bridge.test/post/lagging',
                origin_kind='remote_follower', direction='ap_to_discord',
                content_revision=1,
                current_content_json=json.dumps({
                    'schemaVersion': 1, 'kind': 'post',
                    'apObjectId': 'https://bridge.test/post/lagging',
                    'bodyMarkdown': 'please deliver this edit',
                }),
            )
            session.add(lagging)
            session.flush()
            last_copy = LocalCommunityThreadSurface(
                local_community_thread_id=lagging.id,
                connector_id=connector_id, channel_surface_id=self.surface_id,
                external_thread_id='lagging-thread',
                external_starter_message_id='lagging-message',
                role='host', applied_revision=None,
            )
            session.add(last_copy)
            session.flush()
            return last_copy.id

    def test_recovery_pagination_respects_rebound_owner(self) -> None:
        """Paging past settled copies must not bypass live surface ownership."""
        copy_id = self._add_older_settled_copies_and_new_lagging_copy()
        with self.db.immediate_session() as session:
            session.get(ChannelSurface, self.surface_id).local_community_id = None
        worker, calls = self._worker()
        self.assertEqual(asyncio.run(worker.drain(limit=1)), 0)
        self.assertEqual(calls, [])
        with self.db.session() as session:
            self.assertIsNone(session.get(LocalCommunityThreadSurface, copy_id).applied_revision)

    def test_recovery_claim_batch_limit_is_independent_of_scan_pages(self) -> None:
        """Two lagging copies behind settled rows need two one-claim drains."""
        first_id = self._add_older_settled_copies_and_new_lagging_copy()
        with self.db.immediate_session() as session:
            first = session.get(LocalCommunityThreadSurface, first_id)
            original = session.get(LocalCommunityThread, first.local_community_thread_id)
            second_post = LocalCommunityThread(
                local_community_id=original.local_community_id,
                ap_activity_id='https://bridge.test/create/lagging-2',
                ap_object_id='https://bridge.test/post/lagging-2',
                origin_kind='remote_follower', direction='ap_to_discord',
                content_revision=1,
                current_content_json=json.dumps({
                    'schemaVersion': 1, 'kind': 'post',
                    'apObjectId': 'https://bridge.test/post/lagging-2',
                    'bodyMarkdown': 'another edit',
                }),
            )
            session.add(second_post)
            session.flush()
            copy = LocalCommunityThreadSurface(
                local_community_thread_id=second_post.id,
                connector_id=first.connector_id, channel_surface_id=self.surface_id,
                external_thread_id='lagging-thread-2',
                external_starter_message_id='lagging-message-2',
                role='host', applied_revision=None,
            )
            session.add(copy)
            session.flush()
            second_id = copy.id
        worker, calls = self._worker()
        self.assertEqual(asyncio.run(worker.drain(limit=1)), 1)
        with self.db.session() as session:
            self.assertEqual(session.get(LocalCommunityThreadSurface, first_id).applied_revision, 1)
            self.assertIsNone(session.get(LocalCommunityThreadSurface, second_id).applied_revision)
        self.assertEqual(asyncio.run(worker.drain(limit=1)), 1)
        self.assertEqual(asyncio.run(worker.drain(limit=1)), 0)
        self.assertEqual([call[1] for call in calls], [
            MessageRef('discord-primary', 'lagging-thread', 'lagging-message'),
            MessageRef('discord-primary', 'lagging-thread-2', 'lagging-message-2'),
        ])

    def test_recovery_zero_limit_never_claims_or_sends(self) -> None:
        """Zero-capacity drains must not claim anything after the paging fix."""
        copy_id = self._add_older_settled_copies_and_new_lagging_copy()
        worker, calls = self._worker()
        self.assertEqual(asyncio.run(worker.drain(limit=0)), 0)
        self.assertEqual(calls, [])
        with self.db.session() as session:
            row = session.get(LocalCommunityThreadSurface, copy_id)
            self.assertEqual(row.mutation_state, 'idle')
            self.assertIsNone(row.mutation_claim_token)

    def test_recovery_paginates_past_settled_copies(self) -> None:
        """A later outstanding Update cannot be starved by eight settled rows."""
        copy_id = self._add_older_settled_copies_and_new_lagging_copy()
        worker, calls = self._worker()
        self.assertEqual(asyncio.run(worker.drain(limit=1)), 1)
        self.assertEqual([call[1] for call in calls], [
            MessageRef('discord-primary', 'lagging-thread', 'lagging-message'),
        ])
        with self.db.session() as session:
            self.assertEqual(session.get(LocalCommunityThreadSurface, copy_id).applied_revision, 1)
        # The scanner must not re-edit settled copies on a second drain.
        self.assertEqual(asyncio.run(worker.drain(limit=1)), 0)
        self.assertEqual(len(calls), 1)



    def _add_healthy_recovery_neighbor(self) -> int:
        """Create an independent due copy beside an unsupported source snapshot."""
        with self.db.immediate_session() as session:
            original = session.get(LocalCommunityThread, self.post_id)
            original_copy = session.get(LocalCommunityThreadSurface, self.copy_id)
            post = LocalCommunityThread(
                local_community_id=original.local_community_id,
                ap_activity_id='https://bridge.test/create/healthy-neighbor',
                ap_object_id='https://bridge.test/post/healthy-neighbor',
                origin_kind='remote_follower', direction='ap_to_discord',
                content_revision=1,
                current_content_json=json.dumps({
                    'schemaVersion': 1, 'kind': 'post',
                    'apObjectId': 'https://bridge.test/post/healthy-neighbor',
                    'bodyMarkdown': 'healthy after failure',
                }),
            )
            session.add(post)
            session.flush()
            neighbor = LocalCommunityThreadSurface(
                local_community_thread_id=post.id,
                connector_id=original_copy.connector_id,
                channel_surface_id=self.surface_id,
                external_thread_id='healthy-thread',
                external_starter_message_id='healthy-msg',
                role='host', applied_revision=None,
            )
            session.add(neighbor)
            session.flush()
            return neighbor.id

    def test_unsupported_renderer_is_isolated_from_healthy_copy(self) -> None:
        """A deterministic formatting refusal cannot roll back unrelated work."""
        neighbor_id = self._add_healthy_recovery_neighbor()
        worker, calls = self._worker()
        def render(_domain, _kind, snapshot):
            """Fail closed for exactly one unsupported canonical document."""
            if snapshot['bodyMarkdown'] == 'latest':
                raise ValueError('unsupported source presentation')
            return ContentDocument(snapshot['bodyMarkdown'])
        worker.content_renderer = render
        self.assertEqual(asyncio.run(worker.drain(limit=1)), 1)
        self.assertEqual([call[1] for call in calls], [
            MessageRef('discord-primary', 'healthy-thread', 'healthy-msg'),
        ])
        with self.db.session() as session:
            failed = session.get(LocalCommunityThreadSurface, self.copy_id)
            healthy = session.get(LocalCommunityThreadSurface, neighbor_id)
            self.assertEqual(failed.mutation_state, 'failed_definite')
            self.assertIsNone(failed.applied_revision)
            self.assertIn('unsupported source presentation', failed.mutation_last_error)
            self.assertEqual(healthy.applied_revision, 1)
        # An unreadable row must not issue a POST on subsequent recovery ticks.
        self.assertEqual(asyncio.run(worker.drain(limit=1)), 0)
        self.assertEqual(len(calls), 1)

    def test_invalid_snapshot_does_not_block_new_verified_revision(self) -> None:
        """A repaired canonical revision can recover from a prior render refusal."""
        with self.db.immediate_session() as session:
            session.get(LocalCommunityThread, self.post_id).current_content_json = '{not-json'
        worker, calls = self._worker()
        self.assertEqual(asyncio.run(worker.drain()), 0)
        with self.db.session() as session:
            failed = session.get(LocalCommunityThreadSurface, self.copy_id)
            self.assertEqual(failed.mutation_state, 'failed_definite')
            self.assertIn('invalid canonical content:', failed.mutation_last_error)
        # New authenticated content gives a new revision and unblocks this
        # same bot-owned copy; neither old bytes nor fake ACK are reused.
        with self.db.immediate_session() as session:
            canonical = session.get(LocalCommunityThread, self.post_id)
            canonical.content_revision = 2
            canonical.current_content_json = json.dumps({
                'schemaVersion': 1, 'kind': 'post',
                'apObjectId': canonical.ap_object_id,
                'bodyMarkdown': 'repaired revision',
            })
        self.assertEqual(asyncio.run(worker.drain()), 1)
        self.assertEqual([call[2] for call in calls], ['repaired revision'])
        with self.db.session() as session:
            copy = session.get(LocalCommunityThreadSurface, self.copy_id)
            self.assertEqual(copy.applied_revision, 2)
            self.assertEqual(copy.mutation_state, 'idle')

    def test_invalid_renderer_result_never_acknowledges_physical_update(self) -> None:
        """A malformed domain DTO must remain unacknowledged, not block restart."""
        worker, calls = self._worker()
        worker.content_renderer = lambda _domain, _kind, _snapshot: 'not a ContentDocument'
        self.assertEqual(asyncio.run(worker.drain()), 0)
        self.assertEqual(calls, [])
        with self.db.session() as session:
            row = session.get(LocalCommunityThreadSurface, self.copy_id)
            self.assertEqual(row.mutation_state, 'failed_definite')
            self.assertIsNone(row.applied_revision)
            self.assertIn('ContentDocument', row.mutation_last_error)



    def test_cancelled_ownership_read_releases_provably_unsent_claim(self) -> None:
        """Cancellation of a read cannot strand a possibly-written mutation."""
        from types import SimpleNamespace
        entered = asyncio.Event()
        worker, calls = self._worker()

        class ReadingAdapter:
            """Pause at the read boundary before any write can start."""
            async def fetch_message(self, message):
                """Signal the controlled cancellation point."""
                entered.set()
                await asyncio.Event().wait()

        worker.registry = SimpleNamespace(get=lambda key: ReadingAdapter())

        async def cancel_read():
            """Cancel after entering the ownership lookup, then inspect SQLite."""
            task = asyncio.create_task(worker.drain())
            await asyncio.wait_for(entered.wait(), 5)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        asyncio.run(cancel_read())
        with self.db.session() as session:
            copy = session.get(LocalCommunityThreadSurface, self.copy_id)
            self.assertEqual(copy.mutation_state, 'failed_definite')
            self.assertIsNone(copy.applied_revision)
        self.assertEqual(calls, [])

if __name__ == '__main__':
    unittest.main()
