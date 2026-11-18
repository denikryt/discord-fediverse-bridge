"""Behavioral SQLite proof that read-only mutation scans never bulk-reserve work."""

from __future__ import annotations

import asyncio
import json
import unittest
from datetime import datetime, timedelta, timezone

from src.models import LocalCommunityThread, LocalCommunityThreadSurface
from src.content_sync.platform_delivery_scheduler import PlatformPhysicalDeliveryScheduler
from src.platform_types import AuthorRef, ContentDocument, MessageView
from bridge_test_support import physical_mutation_recovery as mutation_fixture


class MutationClaimSlotScenarios(unittest.TestCase):
    """Observe actual durable state before and after dispatch barriers."""

    setUp = mutation_fixture.PhysicalMutationScenario.setUp
    _worker = mutation_fixture.PhysicalMutationScenario._worker
    _add_older_settled_copies_and_new_lagging_copy = (
        mutation_fixture.PhysicalMutationScenario._add_older_settled_copies_and_new_lagging_copy
    )

    def test_stale_update_ack_then_delete_cannot_resurrect_after_worker_restarts(self) -> None:
        """A held revision-1 write cannot settle revision 2 or resurrect a deleted post."""
        class StateAwareSdk:
            """Record effects at the physical boundary while delaying chosen edits."""

            def __init__(self):
                """Initialize independently controlled provider barriers."""
                self.effects = []
                self.entered = asyncio.Event()
                self.release = asyncio.Event()
                self.pause = False

            async def fetch_message(self, message):
                """Attest the same physical bot-owned starter across workers."""
                return MessageView(message, AuthorRef(
                    message.connector_key, '999', 'bot', 'Bridge', True,
                ))

            async def edit_message(self, message, content):
                """Block exactly the selected in-flight edit before physical success."""
                if self.pause:
                    self.entered.set()
                    await self.release.wait()
                    self.pause = False
                self.effects.append(('edit', message.external_message_id, content.body))

            async def delete_message(self, message):
                """Record the terminal effect without allowing a late edit afterward."""
                self.effects.append(('delete', message.external_message_id, None))

        sdk = StateAwareSdk()

        def restarted_worker():
            """Rebuild the production mutation coordinator but retain external effects."""
            worker, _ = self._worker()
            worker.registry.get = lambda _key: sdk
            return worker

        def change_revision(revision: int, *, body: str | None = None,
                            deleted: bool = False) -> None:
            """Commit a newer canonical revision while an older SDK call awaits."""
            with self.db.immediate_session() as session:
                post = session.get(LocalCommunityThread, self.post_id)
                post.content_revision = revision
                if body is not None:
                    snapshot = json.loads(post.current_content_json)
                    snapshot['bodyMarkdown'] = body
                    post.current_content_json = json.dumps(snapshot)
                if deleted:
                    post.deleted_at = datetime.now(timezone.utc)

        async def scenario() -> None:
            """Use two barriers and separate coordinators to expose stale ACKs."""
            sdk.pause = True
            first = asyncio.create_task(restarted_worker().drain(limit=1))
            await asyncio.wait_for(sdk.entered.wait(), 5)
            change_revision(2, body='revision two')
            sdk.release.set()
            self.assertEqual(await first, 1)
            with self.db.session() as session:
                copy = session.get(LocalCommunityThreadSurface, self.copy_id)
                self.assertEqual(copy.applied_revision, 1)
                self.assertEqual(copy.mutation_state, 'idle')
            self.assertEqual(await restarted_worker().drain(limit=1), 1)
            self.assertEqual(sdk.effects[-1], ('edit', '901', 'revision two'))
            with self.db.session() as session:
                self.assertEqual(session.get(LocalCommunityThreadSurface, self.copy_id).applied_revision, 2)

            sdk.entered = asyncio.Event()
            sdk.release = asyncio.Event()
            sdk.pause = True
            change_revision(3, body='revision three')
            third = asyncio.create_task(restarted_worker().drain(limit=1))
            await asyncio.wait_for(sdk.entered.wait(), 5)
            change_revision(4, deleted=True)
            sdk.release.set()
            self.assertEqual(await third, 1)
            with self.db.session() as session:
                self.assertEqual(session.get(LocalCommunityThreadSurface, self.copy_id).applied_revision, 3)
                self.assertIsNotNone(session.get(LocalCommunityThread, self.post_id).deleted_at)
            self.assertEqual(await restarted_worker().drain(limit=1), 1)
            self.assertEqual(sdk.effects[-1], ('delete', '901', None))
            self.assertEqual(await restarted_worker().drain(limit=1), 0)
            self.assertEqual(len(sdk.effects), 4)
            with self.db.session() as session:
                copy = session.get(LocalCommunityThreadSurface, self.copy_id)
                self.assertEqual(copy.applied_revision, 4)
                self.assertEqual(copy.mutation_state, 'idle')
                self.assertIsNotNone(session.get(LocalCommunityThread, self.post_id).deleted_at)

        asyncio.run(scenario())

    def test_lost_mutation_ack_persists_unresolved_across_coordinator_restart(self) -> None:
        """A direct SDK lost ACK cannot be retried by a new SQLite coordinator."""
        first, calls = self._worker(fail=True)
        # One attempted edit can have succeeded remotely, even on connection loss.
        asyncio.run(first.drain(limit=1))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], 'edit')
        with self.db.session() as session:
            copy = session.get(LocalCommunityThreadSurface, self.copy_id)
            self.assertEqual(copy.mutation_state, 'unresolved')
            self.assertIsNone(copy.applied_revision)
            self.assertIsNone(copy.mutation_claim_token)
        restarted, new_writes = self._worker()
        self.assertEqual(asyncio.run(restarted.drain(limit=1)), 0)
        self.assertEqual(new_writes, [])

    def test_scheduler_without_mutation_owner_preserves_existing_sqlite_candidate(self) -> None:
        """Fluxer-style `mutations=None` keeps Create recovery but never edits a copy."""
        progressed = asyncio.Event()
        creates = []

        class Registry:
            """Only the fixture's already persisted connector is admitted."""
            def get(self, key):
                """Report readiness without touching mutation state."""
                if key != 'discord-primary':
                    raise LookupError(key)
                return type('Ready', (), {'dispatch_ready': lambda self: True})()

        class Fanout:
            """Count real scheduler recovery calls independently of mutations."""
            registry = Registry()

            async def recover_pending(self, *, connector_key):
                """Wake once without submitting an SDK edit."""
                creates.append(connector_key)
                progressed.set()
                return []

        async def run():
            """Run the same mutation-less scheduler branch used by Fluxer-only."""
            scheduler = PlatformPhysicalDeliveryScheduler(
                connector_keys=('discord-primary',), fanout=Fanout(),
                mutations=None, poll_seconds=30,
            )
            scheduler.start()
            try:
                await asyncio.wait_for(progressed.wait(), 5)
            finally:
                await scheduler.stop()

        asyncio.run(run())
        self.assertEqual(creates, ['discord-primary'])
        with self.db.session() as session:
            copy = session.get(LocalCommunityThreadSurface, self.copy_id)
            self.assertEqual(copy.mutation_state, 'idle')
            self.assertIsNone(copy.applied_revision)
            self.assertIsNone(copy.mutation_claim_token)

    def test_crash_after_one_claim_does_not_strand_unclaimed_sibling(self) -> None:
        """One physical slot authorizes one CAS token, not a private batch."""
        second_id = self._add_older_settled_copies_and_new_lagging_copy()
        with self.db.immediate_session() as session:
            session.get(LocalCommunityThreadSurface, self.copy_id).applied_revision = None
        worker, _calls = self._worker()
        candidates = worker._discover_due()
        self.assertEqual({c.copy_id for c in candidates}, {self.copy_id, second_id})
        with self.db.session() as session:
            for key in (self.copy_id, second_id):
                copy = session.get(LocalCommunityThreadSurface, key)
                self.assertEqual(copy.mutation_state, 'idle')
                self.assertIsNone(copy.mutation_claim_token)
        first = next(c for c in candidates if c.copy_id == self.copy_id)
        claim = worker._claim_one(first)
        self.assertIsNotNone(claim)
        with self.db.immediate_session() as session:
            first_row = session.get(LocalCommunityThreadSurface, self.copy_id)
            self.assertEqual(first_row.mutation_state, 'dispatching')
            self.assertEqual(first_row.mutation_claim_token, claim.token)
            second = session.get(LocalCommunityThreadSurface, second_id)
            self.assertEqual(second.mutation_state, 'idle')
            self.assertIsNone(second.mutation_claim_token)
            first_row.mutation_lease_until = datetime.now(timezone.utc) - timedelta(days=1)
        restarted, remote_writes = self._worker()
        self.assertEqual(asyncio.run(restarted.drain(limit=1)), 1)
        self.assertEqual([request[1].external_message_id for request in remote_writes],
                         ['lagging-message'])
        with self.db.session() as session:
            self.assertEqual(session.get(LocalCommunityThreadSurface, self.copy_id).mutation_state,
                             'unresolved')
            self.assertEqual(session.get(LocalCommunityThreadSurface, second_id).applied_revision, 1)

    def test_ownership_lookup_paused_cannot_preclaim_second_copy(self) -> None:
        """A hung read-only network lookup must not reserve any dispatch tokens."""
        other_id = self._add_older_settled_copies_and_new_lagging_copy()
        with self.db.immediate_session() as session:
            session.get(LocalCommunityThreadSurface, self.copy_id).applied_revision = None
        worker, _calls = self._worker()
        entered = asyncio.Event()
        release = asyncio.Event()
        writes: list[str] = []

        class PendingRead:
            """Pause at the SDK read boundary, before durable mutation claim."""
            async def fetch_message(self, message):
                """Wait until SQLite has been inspected in another task."""
                entered.set()
                await release.wait()
                return MessageView(message, AuthorRef(message.connector_key, 'bot', 'bot', 'Bridge', True))

            async def edit_message(self, message, _content):
                """Record the actual remote method entry after the read resolves."""
                writes.append(message.external_message_id)

            async def delete_message(self, _message):
                """Never turn the test edit into a remote deletion."""
                raise AssertionError('unexpected delete')

        class Registry:
            """Expose the explicit owner of this test connector."""
            def get(self, connector_key):
                """Ensure no connector fallback occurs."""
                assert connector_key == 'discord-primary'
                return PendingRead()

        worker.registry = Registry()

        async def run() -> None:
            """Hold a read boundary and observe both physical rows."""
            task = asyncio.create_task(worker.drain(limit=2))
            await asyncio.wait_for(entered.wait(), 5)
            with self.db.session() as session:
                for key in (self.copy_id, other_id):
                    row = session.get(LocalCommunityThreadSurface, key)
                    self.assertEqual(row.mutation_state, 'idle')
                    self.assertIsNone(row.mutation_claim_token)
            self.assertEqual(writes, [])
            release.set()
            self.assertEqual(await task, 2)

        asyncio.run(run())
        self.assertEqual(writes, ['901', 'lagging-message'])

    def test_low_id_preflight_denial_does_not_starve_higher_copy(self) -> None:
        """Repeated definite refusal never claims or writes an unrelated target."""
        other_id = self._add_older_settled_copies_and_new_lagging_copy()
        with self.db.immediate_session() as session:
            session.get(LocalCommunityThreadSurface, self.copy_id).applied_revision = None
        worker, writes = self._worker()
        worker.target_policy = lambda _key, surface_id: surface_id == self.surface_id
        # Only this first physical ID is denied by the read-only provider check.
        original_get = worker.registry.get

        class ScopedRead:
            """Use a deterministic per-copy provider read denial."""
            def __init__(self, adapter):
                self.adapter = adapter

            async def fetch_message(self, message):
                """Reject exactly the earliest copy without starting a write."""
                if message.external_message_id == '901':
                    return MessageView(message, AuthorRef(message.connector_key, 'human', 'human', 'Human', False))
                return await self.adapter.fetch_message(message)

            async def edit_message(self, *args):
                """Proxy a verified physical edit to the stateful fake."""
                return await self.adapter.edit_message(*args)

            async def delete_message(self, *args):
                """Proxy a verified physical deletion to the stateful fake."""
                return await self.adapter.delete_message(*args)

        worker.registry.get = lambda key: ScopedRead(original_get(key))
        self.assertEqual(asyncio.run(worker.drain(limit=2)), 1)
        for _ in range(4):
            self.assertEqual(asyncio.run(worker.drain(limit=2)), 0)
        self.assertEqual([request[1].external_message_id for request in writes],
                         ['lagging-message'])
        with self.db.session() as session:
            refused = session.get(LocalCommunityThreadSurface, self.copy_id)
            healthy = session.get(LocalCommunityThreadSurface, other_id)
            self.assertEqual(refused.mutation_state, 'failed_definite')
            self.assertIsNone(refused.mutation_claim_token)
            self.assertEqual(healthy.applied_revision, 1)

    def test_preview_revision_change_cannot_acquire_a_stale_claim(self) -> None:
        """A concurrent canonical edit invalidates the full candidate fingerprint."""
        worker, calls = self._worker()
        candidate = worker._discover_due()[0]
        with self.db.immediate_session() as session:
            session.get(LocalCommunityThread, self.post_id).content_revision = 2
        self.assertIsNone(worker._claim_one(candidate))
        with self.db.session() as session:
            row = session.get(LocalCommunityThreadSurface, self.copy_id)
            self.assertIsNone(row.mutation_claim_token)
            self.assertEqual(row.mutation_state, 'idle')
        self.assertEqual(calls, [])


if __name__ == '__main__':
    unittest.main()
