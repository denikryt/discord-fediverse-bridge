"""SQLite-backed Create preflight scenarios for unavailable connectors."""

from __future__ import annotations

import asyncio
import unittest

from src.content_sync.platform_fanout import PlatformFanoutCoordinator
from src.models import PlatformDeliveryIntent

from .test_platform_delivery_recovery_scenarios import PhysicalDeliveryRestartScenarios


class MissingAdapterRecoveryScenarios(PhysicalDeliveryRestartScenarios):
    """Unconfigured transports must never consume durable dispatch authorization."""

    def test_unregistered_connector_stays_pristine_pending_until_adapter_returns(self) -> None:
        """Recovery first skips absent transport, then sends the same frozen attempt."""
        intent_id = self._pending('C')

        class OfflineRegistry:
            """No registered SDK client exists for this persisted connector."""

            def get(self, connector_key: str):
                """Reject the exact key rather than substitute a default client."""
                raise LookupError(connector_key)

        offline = PlatformFanoutCoordinator(self.database, OfflineRegistry())
        self.assertEqual(asyncio.run(offline.recover_pending()), [])
        with self.database.session() as session:
            row = session.get(PlatformDeliveryIntent, intent_id)
            self.assertEqual(row.state, 'pending')
            self.assertIsNone(row.dispatch_token)
            self.assertIsNone(row.last_error_kind)
        self.assertEqual(asyncio.run(self.coordinator.recover_pending()), [intent_id])
        self.assertEqual(asyncio.run(self.coordinator.recover_pending()), [])
        with self.database.session() as session:
            self.assertEqual(session.get(PlatformDeliveryIntent, intent_id).state, 'delivered')

    def test_degraded_registered_adapter_skips_claim_and_recovers_when_ready(self) -> None:
        """An explicit readiness probe stops a degraded client pre-claim."""
        intent_id = self._pending('C')
        ready = False
        original = self.adapter

        class ReadinessProxy:
            """Use a real persisted request with controllable SDK readiness."""

            def dispatch_ready(self) -> bool:
                """Read the provider-health admission state without a DB write."""
                return ready

            def __getattr__(self, name: str):
                """Forward the physical transport only after a ready admission."""
                return getattr(original, name)

        class Registry:
            """Resolve the persisted connector without any default fallback."""

            def get(self, key: str):
                """Return the live client for the exact connector identity."""
                if key != 'discord-primary':
                    raise LookupError(key)
                return ReadinessProxy()

        worker = PlatformFanoutCoordinator(self.database, Registry())
        self.assertFalse(asyncio.run(worker.deliver(intent_id)))
        with self.database.session() as session:
            row = session.get(PlatformDeliveryIntent, intent_id)
            self.assertEqual(row.state, 'pending')
            self.assertIsNone(row.dispatch_token)
        ready = True
        self.assertTrue(asyncio.run(worker.deliver(intent_id)))

    def test_offline_reconciliation_preserves_uncertain_receipt(self) -> None:
        """An offline SDK never reads or clears an unknown Create outcome."""
        intent_id = self._pending('C')
        repo = self.database.platform_delivery_intents
        claim = repo.claim(intent_id)
        self.assertIsNotNone(claim)
        self.assertTrue(repo.mark_unresolved(intent_id, token=claim.token,
                                             reason='lost remote response'))
        class OfflineAdapter:
            """Model an unavailable session that must not attempt a read."""
            def dispatch_ready(self) -> bool:
                """Refuse all new physical and read-only RPC admission."""
                return False
            async def _reconcile_persisted_create(self, **kwargs):
                """A remote lookup here would violate offline admission."""
                raise AssertionError('offline reconciliation performed network I/O')
        class Registry:
            """Bind the one persisted connector without a Discord fallback."""
            def get(self, key):
                """Match the frozen intent identity exactly."""
                assert key == 'discord-primary'
                return OfflineAdapter()
        worker = PlatformFanoutCoordinator(self.database, Registry())
        self.assertFalse(asyncio.run(worker.reconcile_unknown(intent_id)))
        self.assertEqual(asyncio.run(worker.recover_pending()), [])
        with self.database.session() as session:
            row = session.get(PlatformDeliveryIntent, intent_id)
            self.assertEqual(row.state, 'unresolved')
            self.assertEqual(row.dispatch_token, claim.token)
        self.assertEqual(self.adapter.posts, [])

    def test_stale_token_cannot_release_another_dispatch(self) -> None:
        """An old pre-I/O witness cannot reset the row claimed by a new owner."""
        intent_id = self._pending('C')
        repo = self.database.platform_delivery_intents
        first = repo.claim(intent_id)
        self.assertIsNotNone(first)
        self.assertTrue(repo.release_unstarted_claim(first))
        second = repo.claim(intent_id)
        self.assertIsNotNone(second)
        self.assertFalse(repo.release_unstarted_claim(first))
        with self.database.session() as session:
            row = session.get(PlatformDeliveryIntent, intent_id)
            self.assertEqual(row.state, 'dispatching')
            self.assertEqual(row.dispatch_token, second.token)


    def test_connector_filter_never_claims_other_connector_pending_work(self) -> None:
        """A ready Discord lane cannot claim or consume the Fluxer-only scan."""
        intent_id = self._pending('C')
        self.assertEqual(asyncio.run(self.coordinator.recover_pending(
            connector_key='fluxer-local')), [])
        with self.database.session() as session:
            row = session.get(PlatformDeliveryIntent, intent_id)
            self.assertEqual(row.state, 'pending')
            self.assertIsNone(row.dispatch_token)
        self.assertEqual(asyncio.run(self.coordinator.recover_pending(
            connector_key='discord-primary')), [intent_id])
        with self.database.session() as session:
            row = session.get(PlatformDeliveryIntent, intent_id)
            self.assertEqual(row.state, 'delivered')


if __name__ == '__main__':
    unittest.main()
