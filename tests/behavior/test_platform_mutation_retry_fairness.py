"""SQLite behavior for bounded mutation scans and explicit revision re-admission."""

from __future__ import annotations

import asyncio
import unittest

from src.models import LocalCommunityThread, LocalCommunityThreadSurface
from tests import test_physical_mutation_recovery as fixture


class PhysicalMutationFairnessScenarios(unittest.TestCase):
    """A refused old copy cannot monopolize a connector or cause a new claim."""

    setUp = fixture.PhysicalMutationRecoveryScenarios.setUp
    _worker = fixture.PhysicalMutationRecoveryScenarios._worker
    _add_older_settled_copies_and_new_lagging_copy = (
        fixture.PhysicalMutationRecoveryScenarios._add_older_settled_copies_and_new_lagging_copy
    )

    def test_same_revision_policy_denial_requires_explicit_readmission(self) -> None:
        """Four timer ticks cannot repeatedly re-evaluate a durable denied revision."""
        worker, writes = self._worker()
        checks = []

        def denied(connector_key, surface_id):
            """Record each policy check before the remote read/write boundary."""
            checks.append((connector_key, surface_id))
            return False

        worker.target_policy = denied
        self.assertEqual(asyncio.run(worker.drain()), 0)
        for _ in range(4):
            self.assertEqual(asyncio.run(worker.drain()), 0)
        self.assertEqual(checks, [('discord-primary', self.surface_id)])
        self.assertEqual(writes, [])
        # Changing a policy callable alone must not silently readmit old work.
        worker.target_policy = lambda _key, _surface: True
        self.assertEqual(asyncio.run(worker.drain()), 0)
        self.assertFalse(worker.readmit_failed_revision(
            domain='local', kind='post', copy_id=self.copy_id,
            connector_key='foreign-instance', revision=1,
        ))
        self.assertFalse(worker.readmit_failed_revision(
            domain='local', kind='post', copy_id=self.copy_id,
            connector_key='discord-primary', revision=99,
        ))
        self.assertTrue(worker.readmit_failed_revision(
            domain='local', kind='post', copy_id=self.copy_id,
            connector_key='discord-primary', revision=1,
        ))
        self.assertEqual(asyncio.run(worker.drain()), 1)
        self.assertEqual(len(writes), 1)
        with self.db.session() as session:
            copy = session.get(LocalCommunityThreadSurface, self.copy_id)
            self.assertEqual(copy.applied_revision, 1)
            self.assertEqual(copy.mutation_state, 'idle')

    def test_new_canonical_revision_is_admitted_without_old_retry(self) -> None:
        """A new desired canonical version invalidates a prior known-unsent refusal."""
        worker, writes = self._worker()
        worker.target_policy = lambda _key, _surface: False
        self.assertEqual(asyncio.run(worker.drain()), 0)
        with self.db.immediate_session() as session:
            session.get(LocalCommunityThread, self.post_id).content_revision = 2
        worker.target_policy = lambda _key, _surface: True
        self.assertEqual(asyncio.run(worker.drain()), 1)
        self.assertEqual(len(writes), 1)
        with self.db.session() as session:
            self.assertEqual(session.get(LocalCommunityThreadSurface,
                                         self.copy_id).applied_revision, 2)

    def test_read_only_scan_resumes_after_bounded_pages(self) -> None:
        """A backlog of settled copies is traversed over bounded scheduler sweeps."""
        with self.db.immediate_session() as session:
            first = session.get(LocalCommunityThreadSurface, self.copy_id)
            first.applied_revision = 1
            post = session.get(LocalCommunityThread, self.post_id)
            for i in range(72):
                # The 72 old copies are complete and must not require an
                # unbounded scan before the next connector's lane can run.
                older = LocalCommunityThread(
                    local_community_id=post.local_community_id,
                    ap_activity_id=f'https://bridge.test/old-action/{i}',
                    ap_object_id=f'https://bridge.test/old-object/{i}',
                    direction='ap_to_discord', origin_kind='remote_follower',
                    content_revision=1,
                )
                session.add(older)
                session.flush()
                session.add(LocalCommunityThreadSurface(
                    local_community_thread_id=older.id, connector_id=first.connector_id,
                    channel_surface_id=first.channel_surface_id,
                    external_thread_id=f'old-thread-{i}',
                    external_starter_message_id=f'old-message-{i}',
                    role='host', applied_revision=1,
                ))
        newest = self._add_older_settled_copies_and_new_lagging_copy()
        worker, remote_writes = self._worker()
        self.assertEqual(worker._discover_due(connector_key='discord-primary'), [])
        found = []
        for _ in range(4):
            found.extend(worker._discover_due(connector_key='discord-primary'))
            if any(candidate.copy_id == newest for candidate in found):
                break
        self.assertEqual([candidate.copy_id for candidate in found], [newest])
        self.assertEqual(remote_writes, [])
        with self.db.session() as session:
            copy = session.get(LocalCommunityThreadSurface, newest)
            self.assertEqual(copy.mutation_state, 'idle')
            self.assertIsNone(copy.mutation_claim_token)


if __name__ == '__main__':
    unittest.main()
