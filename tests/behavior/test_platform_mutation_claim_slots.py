"""Behavioral SQLite proof that read-only mutation scans never bulk-reserve work."""

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta, timezone

from src.models import LocalCommunityThread, LocalCommunityThreadSurface
from src.platform_types import AuthorRef, ContentDocument, MessageView
from bridge_test_support import physical_mutation_recovery as mutation_fixture


class MutationClaimSlotScenarios(unittest.TestCase):
    """Observe actual durable state before and after dispatch barriers."""

    setUp = mutation_fixture.PhysicalMutationScenario.setUp
    _worker = mutation_fixture.PhysicalMutationScenario._worker
    _add_older_settled_copies_and_new_lagging_copy = (
        mutation_fixture.PhysicalMutationScenario._add_older_settled_copies_and_new_lagging_copy
    )

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
