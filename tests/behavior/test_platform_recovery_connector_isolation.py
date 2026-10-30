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


if __name__ == '__main__':
    unittest.main()
