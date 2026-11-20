"""Checkpoint 63: deterministic model-based SQLite state-transition probes.

Each random trace uses a different canonical post under one real subscriber
surface, exercising durable admission, cancellation, uncertainty and adoption.
"""
from __future__ import annotations

import asyncio
import random
import unittest
from datetime import datetime, timezone

from sqlalchemy import select

from src.content_sync.platform_fanout import PlatformFanoutCoordinator
from src.models import (
    LocalCommunityThread, LocalCommunityThreadSurface, LocalSubscriber,
    PlatformDeliveryIntent,
)
from src.platform_types import (
    ContentDocument, CreateAttempt, CreatedThread, DeliveryPresentation,
    MessageRef, ThreadRef, PlatformOperationError,
)
from behavior.test_platform_message_fanout_scenarios import _ThreeForumFixture, _Registry


class MessageFanoutSQLiteStateMachine(_ThreeForumFixture):
    """Model immutable one-POST safety over numerous independent action orders."""

    def test_ordered_and_adversarial_create_outcomes(self):
        """No possible transition may retry an uncertain/terminal physical Create."""
        rng = random.Random(14504)
        outcomes = ['delete_before_send', 'verified_delivery', 'unknown_response',
                    'target_rebound', 'foreign_receipt']
        counts = dict.fromkeys(outcomes, 0)
        _, owner_id, surface_id = self.targets['C']
        forum = self.forums['C']
        repo = self.database.platform_delivery_intents
        for number in range(35):
            action = rng.choice(outcomes)
            counts[action] += 1
            # Source AP identity must be unique; physical target remains C.
            with self.database.immediate_session() as session:
                canonical = LocalCommunityThread(
                    local_community_id=self.community_id,
                    ap_activity_id=f'https://bridge.test/a/model/{number}',
                    ap_object_id=f'https://bridge.test/o/model/{number}',
                    direction='outbound', origin_kind='discord',
                )
                session.add(canonical)
                session.flush()
                cid = canonical.id
            attempt = CreateAttempt(
                f'fanout:v1:local:post:{cid}:discord-primary:{surface_id}',
                'discord-primary', 'forum_thread', forum,
            )
            row = repo.reserve(
                domain='local', kind='post', canonical_id=cid, forum=forum,
                channel_surface_id=surface_id, owner_kind='local_subscriber',
                owner_id=owner_id, attempt=attempt,
                content=ContentDocument(f'body {number}'),
                presentation=DeliveryPresentation('bot'), title='model test',
            )
            self.assertEqual(row.state, 'pending')
            if action == 'delete_before_send':
                with self.database.immediate_session() as session:
                    session.get(LocalCommunityThread, cid).deleted_at = datetime.now(timezone.utc)
                self.assertIsNone(repo.claim(row.id))
                expected = 'cancelled'
            else:
                claim = repo.claim(row.id)
                self.assertIsNotNone(claim)
                self.assertIsNone(repo.claim(row.id))
                receipt = CreatedThread(
                    ThreadRef(forum, f'model-thread-{number}'),
                    MessageRef('discord-primary', f'model-thread-{number}',
                               f'model-starter-{number}'),
                    'bot',
                )
                if action == 'verified_delivery':
                    self.assertTrue(repo.commit_receipt(row.id, token=claim.token,
                                                         receipt=receipt))
                    expected = 'delivered'
                elif action == 'target_rebound':
                    # Simulate a revoked subscriber. A verified physical result
                    # still belongs to the original intent, never another owner.
                    with self.database.immediate_session() as session:
                        session.get(LocalSubscriber, owner_id).status = 'inactive'
                    self.assertTrue(repo.commit_receipt(row.id, token=claim.token,
                                                         receipt=receipt))
                    expected = 'cancelled_after_send'
                    with self.database.immediate_session() as session:
                        session.get(LocalSubscriber, owner_id).status = 'active'
                elif action == 'foreign_receipt':
                    unrelated = CreatedThread(
                        ThreadRef(forum, f'model-thread-{number}'),
                        MessageRef('discord-primary', f'model-thread-{number}',
                                   f'model-starter-{number}'),
                        'webhook',  # incompatible physical mode
                    )
                    with self.assertRaises(PlatformOperationError):
                        repo.commit_receipt(row.id, token=claim.token, receipt=unrelated)
                    self.assertTrue(repo.mark_unresolved(row.id, token=claim.token,
                                                         reason='wrong_mode'))
                    expected = 'unresolved'
                else:
                    self.assertTrue(repo.mark_unresolved(row.id, token=claim.token,
                                                         reason='unknown_delivery'))
                    expected = 'unresolved'
            with self.database.session() as session:
                persisted = session.get(PlatformDeliveryIntent, row.id)
                self.assertEqual(persisted.state, expected)
                copies = session.scalars(select(LocalCommunityThreadSurface).where(
                    LocalCommunityThreadSurface.local_community_thread_id == cid)).all()
                self.assertEqual(len(copies), 1 if expected == 'delivered' else 0)
            # Terminal and uncertain rows never become an executable claim.
            self.assertIsNone(repo.claim(row.id))
            self.assertNotIn(row.id, repo.recoverable_pending_ids())
        self.assertTrue(all(counts.values()), counts)


class TwoTargetCreateOracle(_ThreeForumFixture):
    """Compare complete physical histories with an independent per-target model."""

    def _run_trace(self, trace: tuple[str, ...]) -> None:
        """Execute valid action orders and compare each SQLite/effect transition."""
        expected_posts: list[str] = []
        expected = {'A': 'unseen', 'C': 'unseen'}
        c_active = True
        self.adapter.fail_forums.add('300')
        for action in trace:
            if action in {'A', 'C'}:
                # Only the first attempt crosses the platform boundary. A is
                # accepted and confirmed; C is accepted with a lost ACK.
                if expected[action] == 'unseen' and (action == 'A' or c_active):
                    expected_posts.append(self.forums[action].external_channel_id)
                    expected[action] = 'delivered' if action == 'A' else 'unresolved'
                asyncio.run(self._deliver(action))
            elif action == 'restart':
                # A freshly constructed worker has no process-local memory of
                # effects; the SQLite ledger must prevent a second C POST.
                self.coordinator = PlatformFanoutCoordinator(
                    self.database, _Registry(self.adapter),
                )
                asyncio.run(self.coordinator.recover_pending())
            elif action == 'unsubscribe_C':
                with self.database.immediate_session() as session:
                    session.get(LocalSubscriber, self.targets['C'][1]).status = 'inactive'
                c_active = False
            else:
                self.fail(f'unknown action {action}')

            with self.subTest(prefix=trace, step=action):
                # This oracle is derived solely from scripted provider outcomes,
                # never from the persistence methods under verification.
                self.assertEqual(self.adapter.posts, expected_posts)
                with self.database.session() as session:
                    copies = session.scalars(select(LocalCommunityThreadSurface).where(
                        LocalCommunityThreadSurface.local_community_thread_id == self.canonical_id,
                    )).all()
                    owners = {row.channel_surface_id for row in copies}
                    self.assertEqual(
                        owners,
                        {self.targets[label][2] for label in ('A', 'C')
                         if expected[label] == 'delivered'},
                    )
                    intents = session.scalars(select(PlatformDeliveryIntent).where(
                        PlatformDeliveryIntent.canonical_row_id == self.canonical_id,
                    )).all()
                    persisted = {row.channel_surface_id: row.state for row in intents}
                    for label in ('A', 'C'):
                        state = expected[label]
                        surface_id = self.targets[label][2]
                        if state == 'unseen':
                            self.assertNotIn(surface_id, persisted)
                        elif state == 'delivered':
                            self.assertEqual(persisted[surface_id], 'delivered')
                        else:
                            self.assertEqual(persisted[surface_id], 'unresolved')

    def test_confirmed_then_uncertain_with_duplicates_restart_unsubscribe(self) -> None:
        """The successful A receipt survives C's unknown Create and later cancellation."""
        self._run_trace(('A', 'C', 'C', 'restart', 'unsubscribe_C', 'A', 'C'))

    def test_uncertain_first_does_not_block_a_on_later_tick(self) -> None:
        """C may lose its first ACK before A delivers, without repeated posts."""
        self._run_trace(('C', 'restart', 'A', 'C', 'A', 'unsubscribe_C', 'restart'))


if __name__ == '__main__':
    unittest.main()
