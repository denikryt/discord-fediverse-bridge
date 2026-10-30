"""Connector-lane lifecycle and concurrency barriers at the runtime scheduler edge."""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from src.content_sync.platform_delivery_scheduler import PlatformPhysicalDeliveryScheduler


class PhysicalSchedulerLaneScenarios(unittest.IsolatedAsyncioTestCase):
    """Slow connector operations cannot block independent connector progress."""

    async def test_slow_fluxer_does_not_block_discord_create_or_mutation(self) -> None:
        """A blocked Fluxer read/write must not prevent Discord completion."""
        fluxer_entered = asyncio.Event()
        fluxer_release = asyncio.Event()
        discord_created = asyncio.Event()
        discord_mutated = asyncio.Event()
        calls: list[tuple[str, str]] = []

        class Registry:
            """Expose two explicitly ready transport identities."""
            def get(self, connector_key):
                """Require exact configured key, without default fallback."""
                if connector_key not in ('discord-primary', 'fluxer-local'):
                    raise LookupError(connector_key)
                return SimpleNamespace(dispatch_ready=lambda: True)

        class Fanout:
            """Observe independent physical Create entry and completion."""
            registry = Registry()
            async def recover_pending(self, *, connector_key):
                """Hold Fluxer until explicitly released by the test barrier."""
                calls.append(('create', connector_key))
                if connector_key == 'fluxer-local':
                    fluxer_entered.set()
                    await fluxer_release.wait()
                else:
                    discord_created.set()
                return []

        class Mutations:
            """Observe independent edit/delete progress via the same lane."""
            async def drain(self, *, connector_key, limit):
                """Record an independently completed mutation pass."""
                assert limit == 1
                calls.append(('mutation', connector_key))
                if connector_key == 'discord-primary':
                    discord_mutated.set()
                return 0

        scheduler = PlatformPhysicalDeliveryScheduler(
            connector_keys=('fluxer-local', 'discord-primary'),
            fanout=Fanout(), mutations=Mutations(), poll_seconds=30,
        )
        scheduler.start()
        scheduler.start()
        try:
            await asyncio.wait_for(fluxer_entered.wait(), 5)
            await asyncio.wait_for(discord_created.wait(), 5)
            await asyncio.wait_for(discord_mutated.wait(), 5)
            self.assertNotIn(('mutation', 'fluxer-local'), calls)
            self.assertEqual(len(scheduler._tasks), 2)
        finally:
            fluxer_release.set()
            await scheduler.stop()
        self.assertEqual(scheduler._tasks, {})

    async def test_unavailable_connector_never_enters_recovery_and_can_wake(self) -> None:
        """A readiness transition explicitly wakes only the recovered lane."""
        ready = False
        progressed = asyncio.Event()
        calls: list[str] = []

        class Registry:
            """Represent a connector registered but not yet authenticated."""
            def get(self, connector_key):
                """Expose one exact readiness snapshot."""
                assert connector_key == 'fluxer-local'
                return SimpleNamespace(dispatch_ready=lambda: ready)

        class Fanout:
            """Observe actual entry into the caller's recovery boundary."""
            registry = Registry()
            async def recover_pending(self, *, connector_key):
                """Record recovered work after readiness is true."""
                calls.append(connector_key)
                progressed.set()
                return []

        class Mutations:
            """Expose a bounded per-connector mutation drain."""
            async def drain(self, *, connector_key, limit):
                """No physical operation when readiness is false."""
                assert connector_key == 'fluxer-local' and limit == 1
                return 0

        scheduler = PlatformPhysicalDeliveryScheduler(
            connector_keys=('fluxer-local',), fanout=Fanout(), mutations=Mutations(),
            poll_seconds=30,
        )
        scheduler.start()
        try:
            # Scheduler has begun but a pre-ready connector cannot enter work.
            await asyncio.sleep(0)
            self.assertEqual(calls, [])
            ready = True
            scheduler.wake('fluxer-local')
            await asyncio.wait_for(progressed.wait(), 5)
            self.assertEqual(calls, ['fluxer-local'])
        finally:
            await scheduler.stop()
        scheduler.wake('fluxer-local')
        with self.assertRaises(RuntimeError):
            scheduler.start()

    async def test_capacity_and_invalid_wake_fail_before_scheduling(self) -> None:
        """A ninth connector cannot bypass the global configured lane bound."""
        with self.assertRaises(ValueError):
            PlatformPhysicalDeliveryScheduler(
                connector_keys=tuple(f'c{i}' for i in range(9)),
                fanout=object(), mutations=object(),
            )
        with self.assertRaises(ValueError):
            PlatformPhysicalDeliveryScheduler(
                connector_keys=('same', 'same'),
                fanout=object(), mutations=object(),
            )
        scheduler = PlatformPhysicalDeliveryScheduler(
            connector_keys=('discord-primary',), fanout=object(), mutations=object(),
        )
        with self.assertRaises(ValueError):
            scheduler.wake('wrong-connector')
        await scheduler.stop()


if __name__ == '__main__':
    unittest.main()
