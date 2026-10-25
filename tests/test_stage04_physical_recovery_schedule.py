"""Discord-ready recovery of Stage 04 intents on the existing worker."""
from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from src.personal_feed_discord_delivery_worker import PersonalFeedDiscordDeliveryWorker


class _IdlePersonalFeed:
    """Emulate an empty personal feed without a network transport."""

    def recover_dispatching(self) -> None:
        """Preserve the regular worker's startup recovery hook."""

    def claim_due(self) -> list:
        """Leave the shared scheduler available for community deliveries."""
        return []


class PhysicalRecoverySchedulerScenarios(unittest.IsolatedAsyncioTestCase):
    """Reuse the Discord-owned worker without adding a second background loop."""

    def setUp(self) -> None:
        """Create a worker whose personal-feed queue is empty."""
        db=SimpleNamespace(personal_feed_conversations=_IdlePersonalFeed())
        self.worker=PersonalFeedDiscordDeliveryWorker(
            database=db, adapter=object(), personal_feed_service=object())

    async def test_ready_worker_drains_new_physical_pending_work(self) -> None:
        """A committed pending intent must be recoverable after the SDK is ready."""
        ready=asyncio.Event()
        seen=[]
        async def recover():
            """Emulate a coordinator after verified SQLite claim."""
            seen.append('recovered')
            ready.set()
        self.worker.add_drain(recover)
        self.worker.start()
        try:
            await asyncio.wait_for(ready.wait(), timeout=1)
        finally:
            await self.worker.stop()
        self.assertEqual(seen,['recovered'])

    async def test_failing_recovery_cannot_block_another_registered_drain(self) -> None:
        """A failing community target never wedges the shared scheduler."""
        ready=asyncio.Event()
        async def broken():
            """Model a failed target-scoped SQLite recovery pass."""
            raise RuntimeError('intent failed')
        async def healthy():
            """Model an independent ready physical intent."""
            ready.set()
        self.worker.add_drain(broken)
        self.worker.add_drain(healthy)
        self.worker.start()
        try:
            await asyncio.wait_for(ready.wait(), timeout=1)
        finally:
            await self.worker.stop()
        self.assertTrue(ready.is_set())

    async def test_cannot_register_recovery_after_worker_is_started(self) -> None:
        """Binding late must not create an unowned process-lifetime callback."""
        self.worker.start()
        try:
            with self.assertRaises(RuntimeError):
                self.worker.add_drain(lambda: asyncio.sleep(0))
        finally:
            await self.worker.stop()
