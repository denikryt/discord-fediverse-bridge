"""Coordinator-only Discord Create admission before any SDK physical POST."""

from __future__ import annotations

import asyncio
import unittest

from src.discord_adapter import DiscordAdapter
from src.platform_types import (
    ChannelRef, ConnectorRef, ContentDocument, CreateAttempt,
    CreatedMessage, DeliveryPresentation, MessageRef, PlatformOperationError,
)


class CoordinatorCreatePermitScenarios(unittest.IsolatedAsyncioTestCase):
    """Reserved CreateAttempt keys must never bypass a durable coordinator claim."""

    def setUp(self) -> None:
        """Create a tiny process-local registry without a Discord SDK client."""
        self.adapter = DiscordAdapter(
            connector=ConnectorRef('discord-primary', 'discord'),
            bot=object(), max_create_attempts=2,
        )
        self.channel = ChannelRef('discord-primary', '100', 'text', '1')
        self.content = ContentDocument('hello')
        self.presentation = DeliveryPresentation('bot')
        self.sent = []

    def attempt(self, suffix: str) -> CreateAttempt:
        """Return one exact reserved logical Create key with stable destination."""
        return CreateAttempt(f'fanout:v1:local:comment:{suffix}:discord-primary:1',
                             'discord-primary', 'message', self.channel)

    async def send(self, attempt: CreateAttempt, *, uncertain: bool = False):
        """Simulate a single physical transport crossing and optional lost ACK."""
        self.sent.append(attempt.key)
        if uncertain:
            raise PlatformOperationError('unknown_delivery', 'lost response')
        return CreatedMessage(MessageRef('discord-primary', '100', '501'), 'bot')

    async def dispatch(self, attempt: CreateAttempt, *, permit=None,
                       content: ContentDocument | None = None, uncertain=False):
        """Exercise adapter singleflight with an exact transport signature."""
        return await self.adapter._begin_create_attempt(
            attempt, operation='message', destination=self.channel,
            content=content or self.content, presentation=self.presentation,
            title=None, reply_to=None, dispatch_permit=permit,
            send=lambda: self.send(attempt, uncertain=uncertain),
        )

    async def test_raw_reserved_attempt_never_posts_even_after_cache_eviction(self) -> None:
        """A direct caller without a persisted coordinator claim must fail closed."""
        attempt = self.attempt('a')
        with self.assertRaises(PlatformOperationError) as caught:
            await self.dispatch(attempt)
        self.assertEqual(caught.exception.reason, 'invalid_request')
        self.assertEqual(self.sent, [])

    async def test_bare_claim_metadata_cannot_mint_a_reserved_permit(self) -> None:
        """A caller that knows the adapter and a guessed token cannot authorize POST."""
        from src.platform_types import CommittedCreateDispatchProof
        attempt = self.attempt('bare')
        kwargs = dict(intent_id=12, attempt=attempt,
                      request_fingerprint='a'*64, dispatch_token='fake-claim',
                      operation='message', destination=self.channel,
                      content=self.content, presentation=self.presentation,
                      title=None, reply_to=None)
        with self.assertRaises(TypeError):
            self.adapter._mint_coordinator_dispatch_permit(**kwargs)
        forged = CommittedCreateDispatchProof(12, attempt.key, 'a'*64,
                                               'fake-claim', b'x'*32)
        with self.assertRaises(PlatformOperationError):
            self.adapter._mint_coordinator_dispatch_permit(proof=forged, **kwargs)
        with self.assertRaises(PlatformOperationError):
            await self.dispatch(attempt)
        self.assertEqual(self.sent, [])

    async def test_verified_coordinator_permit_is_one_use_and_signature_bound(self) -> None:
        """The same permit cannot authorize a second physical action or changed DTO."""
        attempt = self.attempt('a')
        from src.platform_types import _issue_committed_create_dispatch_proof
        proof = _issue_committed_create_dispatch_proof(
            42, attempt.key, 'a'*64, 'writer-cas-a')
        permit = self.adapter._mint_coordinator_dispatch_permit(
            proof=proof, intent_id=42, attempt=attempt, request_fingerprint='a'*64,
            dispatch_token='writer-cas-a', operation='message',
            destination=self.channel, content=self.content,
            presentation=self.presentation, title=None, reply_to=None,
        )
        with self.assertRaises(PlatformOperationError):
            await self.dispatch(attempt, permit=permit, content=ContentDocument('forged'))
        # The invalid signature did not consume a valid one-time permit.
        self.assertEqual(self.sent, [])
        receipt = await self.dispatch(attempt, permit=permit)
        self.assertEqual(receipt.message.external_message_id, '501')
        self.assertEqual(len(self.sent), 1)
        with self.assertRaises(PlatformOperationError):
            await self.dispatch(attempt, permit=permit)
        # A still-reachable proof already used for one permit cannot create a
        # fresh permit after the successful flight is acknowledged/evicted.
        with self.assertRaises(PlatformOperationError):
            self.adapter._mint_coordinator_dispatch_permit(
                proof=proof, intent_id=42, attempt=attempt,
                request_fingerprint='a'*64, dispatch_token='writer-cas-a',
                operation='message', destination=self.channel,
                content=self.content, presentation=self.presentation,
                title=None, reply_to=None,
            )
        self.assertEqual(len(self.sent), 1)

    async def test_forged_or_premature_ack_cannot_evict_unknown_flight(self) -> None:
        """A claimed token alone is not a durable SQLite terminal ACK proof."""
        from src.platform_types import (
            CommittedCreateAckProof, _issue_committed_create_ack_proof,
        )
        attempt = self.attempt('proof')
        from src.platform_types import _issue_committed_create_dispatch_proof
        proof = _issue_committed_create_dispatch_proof(
            42, attempt.key, 'b'*64, 'writer-cas-proof')
        permit = self.adapter._mint_coordinator_dispatch_permit(
            proof=proof, intent_id=42, attempt=attempt, request_fingerprint='b'*64,
            dispatch_token='writer-cas-proof', operation='message',
            destination=self.channel, content=self.content,
            presentation=self.presentation, title=None, reply_to=None,
        )
        started = asyncio.Event()
        finish = asyncio.Event()

        async def blocked_send():
            """Hold one unresolved network call while ACK is attempted."""
            self.sent.append(attempt.key)
            started.set()
            await finish.wait()
            raise PlatformOperationError('unknown_delivery', 'lost response')

        call = asyncio.create_task(self.adapter._begin_create_attempt(
            attempt, operation='message', destination=self.channel,
            content=self.content, presentation=self.presentation,
            title=None, reply_to=None, send=blocked_send, dispatch_permit=permit,
        ))
        await started.wait()
        issued = _issue_committed_create_ack_proof(
            42, attempt.key, 'b'*64, 'writer-cas-proof', 'unresolved')
        args = dict(permit=permit, intent_id=42, attempt=attempt,
                    request_fingerprint='b'*64, dispatch_token='writer-cas-proof',
                    terminal_status='unresolved')
        self.assertFalse(await self.adapter.acknowledge_persisted_create(
            proof=issued, **args))  # Future remains in progress.
        finish.set()
        with self.assertRaises(PlatformOperationError):
            await call
        forged = CommittedCreateAckProof(
            42, attempt.key, 'b'*64, 'writer-cas-proof', 'unresolved', b'0'*32)
        self.assertFalse(await self.adapter.acknowledge_persisted_create(
            proof=forged, **args))
        self.assertEqual(len(self.adapter._creates), 1)
        self.assertTrue(await self.adapter.acknowledge_persisted_create(
            proof=issued, **args))
        self.assertEqual(len(self.adapter._creates), 0)

    async def test_cancelled_owner_remains_uncertain_until_durable_ack(self) -> None:
        """Cancellation after committed dispatch is not evidence of no POST."""
        from src.platform_types import (
            _issue_committed_create_dispatch_proof,
            _issue_committed_create_ack_proof,
        )
        attempt = self.attempt('cancel')
        claim_proof = _issue_committed_create_dispatch_proof(
            20, attempt.key, 'e'*64, 'cas-cancel')
        permit = self.adapter._mint_coordinator_dispatch_permit(
            proof=claim_proof, intent_id=20, attempt=attempt,
            request_fingerprint='e'*64, dispatch_token='cas-cancel',
            operation='message', destination=self.channel,
            content=self.content, presentation=self.presentation,
            title=None, reply_to=None,
        )
        started = asyncio.Event()
        never = asyncio.Event()

        async def physical():
            """Model a POST whose network response cannot be observed."""
            self.sent.append(attempt.key)
            started.set()
            await never.wait()
            raise AssertionError('cancelled transport unexpectedly resumed')

        owner = asyncio.create_task(self.adapter._begin_create_attempt(
            attempt, operation='message', destination=self.channel,
            content=self.content, presentation=self.presentation,
            title=None, reply_to=None, send=physical,
            dispatch_permit=permit,
        ))
        await started.wait()
        owner.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await owner
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(len(self.adapter._creates), 1)
        bad = _issue_committed_create_ack_proof(
            20, attempt.key, 'e'*64, 'cas-cancel', 'delivered')
        kwargs = dict(permit=permit, intent_id=20, attempt=attempt,
                      request_fingerprint='e'*64, dispatch_token='cas-cancel')
        self.assertFalse(await self.adapter.acknowledge_persisted_create(
            proof=bad, terminal_status='delivered', **kwargs))
        good = _issue_committed_create_ack_proof(
            20, attempt.key, 'e'*64, 'cas-cancel', 'unresolved')
        self.assertTrue(await self.adapter.acknowledge_persisted_create(
            proof=good, terminal_status='unresolved', **kwargs))
        self.assertEqual(len(self.adapter._creates), 0)
        with self.assertRaises(PlatformOperationError):
            await self.dispatch(attempt)
        self.assertEqual(len(self.sent), 1)

    async def test_direct_unknown_attempts_keep_fail_closed_capacity(self) -> None:
        """Unowned legacy traffic may not claim the durable cache ACK privilege."""
        for number in range(2):
            attempt = CreateAttempt(f'legacy-unknown-{number}', 'discord-primary',
                                    'message', self.channel)
            with self.assertRaises(PlatformOperationError) as failed:
                await self.dispatch(attempt, uncertain=True)
            self.assertEqual(failed.exception.reason, 'unknown_delivery')
        third = CreateAttempt('legacy-third', 'discord-primary', 'message',
                              self.channel)
        with self.assertRaises(PlatformOperationError) as full:
            await self.dispatch(third)
        self.assertEqual(full.exception.reason, 'unavailable')
        self.assertEqual(len(self.sent), 2)

    async def test_unknown_after_durable_ack_can_release_capacity_without_repost(self) -> None:
        """Three distinct unknown creates may progress while old DB intents stay unresolved."""
        for index in range(3):
            attempt = self.attempt(str(index))
            from src.platform_types import _issue_committed_create_dispatch_proof
            proof = _issue_committed_create_dispatch_proof(
                index+1, attempt.key, str(index)*64, f'cas-{index}')
            permit = self.adapter._mint_coordinator_dispatch_permit(
                proof=proof, intent_id=index+1, attempt=attempt, request_fingerprint=str(index)*64,
                dispatch_token=f'cas-{index}', operation='message',
                destination=self.channel, content=self.content,
                presentation=self.presentation, title=None, reply_to=None,
            )
            with self.assertRaises(PlatformOperationError) as lost:
                await self.dispatch(attempt, permit=permit, uncertain=True)
            self.assertEqual(lost.exception.reason, 'unknown_delivery')
            # Unit-level adapter test simulates a proof already issued by
            # the repository after commit; SQLite production test below
            # independently verifies proof issuance ordering.
            from src.platform_types import _issue_committed_create_ack_proof
            proof = _issue_committed_create_ack_proof(
                index+1, attempt.key, str(index)*64, f'cas-{index}', 'unresolved')
            self.assertTrue(await self.adapter.acknowledge_persisted_create(
                permit=permit, proof=proof, intent_id=index+1, attempt=attempt,
                request_fingerprint=str(index)*64,
                dispatch_token=f'cas-{index}', terminal_status='unresolved',
            ))
            with self.assertRaises(PlatformOperationError):
                await self.dispatch(attempt)
        self.assertEqual(len(self.sent), 3)
        self.assertEqual(len(self.adapter._creates), 0)


if __name__ == '__main__':
    unittest.main()


class SQLiteCoordinatorPermitScenarios(unittest.TestCase):
    """The real SQLite coordinator must own cache ACK, not a forged raw call."""

    def setUp(self) -> None:
        """Persist three independent Local Community physical targets on one forum."""
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from sqlalchemy import select
        from src.db import Database
        from src.models import LocalCommunity, LocalCommunityThread, ChannelSurface, PlatformConnector

        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db = Database(f'sqlite:///{Path(temp.name) / "bridge.db"}')
        self.addCleanup(self.db.close)
        self.db.migrate()
        self.forum = ChannelRef('discord-primary', '321', 'forum', '1')
        with self.db.immediate_session() as session:
            community = LocalCommunity(
                slug='permit', display_name='Permit',
                actor_url='https://bridge.test/actors/communities/permit',
                inbox_url='https://bridge.test/actors/communities/permit/inbox',
                outbox_url='https://bridge.test/actors/communities/permit/outbox',
                followers_url='https://bridge.test/actors/communities/permit/followers',
                public_key_pem='public', private_key_pem='private', status='active',
            )
            session.add(community)
            session.flush()
            self.owner = community.id
            connector = session.scalar(select(PlatformConnector).where(
                PlatformConnector.connector_key == 'discord-primary'))
            surface = ChannelSurface(
                connector_id=connector.id, external_channel_id='321',
                external_guild_id='1', kind='forum', local_community_id=community.id,
            )
            session.add(surface)
            session.flush()
            self.surface = surface.id
            self.canonical_ids = []
            for i in range(3):
                row = LocalCommunityThread(
                    local_community_id=community.id,
                    ap_activity_id=f'https://bridge.test/a/create/{i}',
                    ap_object_id=f'https://bridge.test/objects/{i}',
                    direction='outbound', origin_kind='discord',
                )
                session.add(row)
                session.flush()
                self.canonical_ids.append(row.id)
        self.adapter = DiscordAdapter(
            connector=ConnectorRef('discord-primary', 'discord'),
            bot=object(), max_create_attempts=2,
        )
        self.calls: list[str] = []
        self.unknown = True

        async def create_forum_thread(adapter, forum, content, *, title, presentation,
                                      attempt, dispatch_permit=None):
            """Run the real singleflight/permit gate around an SDK fake."""
            async def physical():
                """Model a confirmed POST with a possibly missing HTTP receipt."""
                self.calls.append(attempt.key)
                if self.unknown:
                    raise PlatformOperationError('unknown_delivery', 'response lost')
                return __import__('src.platform_types', fromlist=['CreatedThread']).CreatedThread(
                    __import__('src.platform_types', fromlist=['ThreadRef']).ThreadRef(forum, '901'),
                    MessageRef('discord-primary', '901', '902'), 'bot',
                )
            return await adapter._begin_create_attempt(
                attempt, operation='forum_thread', destination=forum,
                content=content, presentation=presentation, title=title,
                reply_to=None, send=physical, dispatch_permit=dispatch_permit,
            )
        import types
        self.adapter.create_forum_thread = types.MethodType(create_forum_thread, self.adapter)
        class Registry:
            """Select only the exact adapter configured for this connector."""
            def get(_self, key):
                """Refuse any fallback transport for the frozen Create."""
                if key != 'discord-primary':
                    raise AssertionError('wrong connector')
                return self.adapter
        from src.content_sync.platform_fanout import PlatformFanoutCoordinator
        self.coordinator = PlatformFanoutCoordinator(self.db, Registry())

    def _pending(self, canonical_id: int) -> tuple[int, CreateAttempt]:
        """Commit immutable intent DTO and exact target before any fake POST."""
        attempt = CreateAttempt(
            f'fanout:v1:local:post:{canonical_id}:discord-primary:{self.surface}',
            'discord-primary', 'forum_thread', self.forum,
        )
        repo = self.db.platform_delivery_intents
        row = repo.reserve(
            domain='local', kind='post', canonical_id=canonical_id,
            forum=self.forum, channel_surface_id=self.surface,
            owner_kind='local_host', owner_id=self.owner,
            attempt=attempt, content=ContentDocument('hello'),
            presentation=DeliveryPresentation('bot'), title='Title',
        )
        repo.materialize(
            row.id, attempt=attempt, content=ContentDocument('hello'),
            presentation=DeliveryPresentation('bot'), title='Title',
        )
        return row.id, attempt

    def test_dispatch_grant_requires_matching_live_sqlite_claim(self) -> None:
        """A completed/revoked SQLite claim cannot mint another SDK authority."""
        from src.db.repositories.platform_delivery_intents import ClaimedCreate
        from src.platform_types import _verify_committed_create_dispatch_proof
        repo = self.db.platform_delivery_intents
        intent_id, _ = self._pending(self.canonical_ids[0])
        self.assertIsNone(repo.committed_dispatch_proof(
            ClaimedCreate(intent_id, 'guessed', 'not-a-request', 'bad-fingerprint')))
        claim = repo.claim(intent_id)
        self.assertIsNotNone(claim)
        grant = repo.committed_dispatch_proof(claim)
        self.assertTrue(_verify_committed_create_dispatch_proof(grant))
        self.assertTrue(repo.mark_unresolved(intent_id, token=claim.token,
                                             reason='lost_response'))
        self.assertIsNone(repo.committed_dispatch_proof(claim))
        self.assertIsNone(repo.claim(intent_id))

    def test_three_committed_unknown_attempts_release_cache_without_repost(self) -> None:
        """Cache limit two must not block third DB-owned Create; replays remain blocked."""
        from src.models import PlatformDeliveryIntent
        for row_id in self.canonical_ids:
            intent_id, attempt = self._pending(row_id)
            self.assertFalse(asyncio.run(self.coordinator.deliver(intent_id)))
            with self.db.session() as session:
                self.assertEqual(session.get(PlatformDeliveryIntent, intent_id).state,
                                 'unresolved')
            self.assertEqual(len(self.adapter._creates), 0)
            self.assertFalse(asyncio.run(self.coordinator.deliver(intent_id)))
            with self.assertRaises(PlatformOperationError):
                asyncio.run(self.adapter._begin_create_attempt(
                    attempt, operation='forum_thread', destination=self.forum,
                    content=ContentDocument('hello'),
                    presentation=DeliveryPresentation('bot'), title='Title',
                    reply_to=None, send=lambda: None,
                ))
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(len(set(self.calls)), 3)

    def test_positive_receipt_after_sqlite_failure_is_adopted_without_repost(self) -> None:
        """A validated SDK receipt survives a failed mapping commit as one unresolved attempt."""
        from src.models import PlatformDeliveryIntent, LocalCommunityThreadSurface
        from sqlalchemy import select
        self.unknown = False
        intent_id, attempt = self._pending(self.canonical_ids[0])
        repo = self.db.platform_delivery_intents
        original = repo.commit_receipt
        injected = False

        def fail_once(*args, **kwargs):
            """Crash the first receipt write after the fake Discord POST returned."""
            nonlocal injected
            if not injected:
                injected = True
                raise RuntimeError('receipt transaction rolled back')
            return original(*args, **kwargs)

        repo.commit_receipt = fail_once
        self.assertFalse(asyncio.run(self.coordinator.deliver(intent_id)))
        repo.commit_receipt = original
        with self.db.session() as session:
            self.assertEqual(session.get(PlatformDeliveryIntent, intent_id).state,
                             'unresolved')
            self.assertIsNone(session.scalar(select(LocalCommunityThreadSurface).where(
                LocalCommunityThreadSurface.local_community_thread_id == self.canonical_ids[0])))
        # The adapter still holds the original *verified* completed flight.
        self.assertTrue(asyncio.run(self.coordinator.reconcile_unknown(intent_id)))
        with self.db.session() as session:
            self.assertEqual(session.get(PlatformDeliveryIntent, intent_id).state,
                             'delivered')
            physical = session.scalar(select(LocalCommunityThreadSurface).where(
                LocalCommunityThreadSurface.local_community_thread_id == self.canonical_ids[0]))
            self.assertEqual((physical.external_thread_id, physical.external_starter_message_id),
                             ('901', '902'))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.adapter._creates), 0)
        self.assertFalse(asyncio.run(self.coordinator.deliver(intent_id)))
        self.assertFalse(asyncio.run(self.coordinator.reconcile_unknown(intent_id)))
        self.assertEqual(len(self.calls), 1)

    def test_unknown_or_forged_reconciliation_does_not_adopt(self) -> None:
        """A synthetic receipt cannot convert an unresolved Discord Create to delivered."""
        from src.models import PlatformDeliveryIntent
        from src.platform_types import (
            CreatedThread, ThreadRef, VerifiedCreateReconciliationProof,
        )
        intent_id, attempt = self._pending(self.canonical_ids[0])
        self.assertFalse(asyncio.run(self.coordinator.deliver(intent_id)))
        repo = self.db.platform_delivery_intents
        claim = repo.unresolved_claim(intent_id)
        receipt = CreatedThread(ThreadRef(self.forum, '901'),
                                MessageRef('discord-primary', '901', '902'), 'bot')
        self.assertFalse(repo.commit_receipt(intent_id, token=claim.token, receipt=receipt))
        forged = VerifiedCreateReconciliationProof(
            claim.intent_id, attempt.key, claim.fingerprint, claim.token, b'0'*32)
        self.assertFalse(repo.commit_receipt(intent_id, token=claim.token,
                                             receipt=receipt, reconciliation_proof=forged))
        self.assertFalse(asyncio.run(self.coordinator.reconcile_unknown(intent_id)))
        with self.db.session() as session:
            self.assertEqual(session.get(PlatformDeliveryIntent, intent_id).state,
                             'unresolved')
        self.assertEqual(len(self.calls), 1)

    def test_verified_receipt_commits_before_success_cache_ack(self) -> None:
        """The original receipt/mapping is durable before the local cache is evicted."""
        from sqlalchemy import select
        from src.models import PlatformDeliveryIntent, LocalCommunityThreadSurface
        self.unknown = False
        intent_id, attempt = self._pending(self.canonical_ids[0])
        self.assertTrue(asyncio.run(self.coordinator.deliver(intent_id)))
        with self.db.session() as session:
            self.assertEqual(session.get(PlatformDeliveryIntent, intent_id).state,
                             'delivered')
            copy = session.scalar(select(LocalCommunityThreadSurface).where(
                LocalCommunityThreadSurface.local_community_thread_id == self.canonical_ids[0]))
            self.assertEqual((copy.external_thread_id, copy.external_starter_message_id),
                             ('901', '902'))
        self.assertEqual(len(self.adapter._creates), 0)
        self.assertFalse(asyncio.run(self.coordinator.deliver(intent_id)))
        self.assertEqual(len(self.calls), 1)
