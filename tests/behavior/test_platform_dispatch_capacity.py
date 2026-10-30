"""Observable admission and child-wake behavior for shared connector dispatch capacity."""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from src.content_sync.platform_delivery_scheduler import ConnectorDispatchCapacity
from src.content_sync.platform_fanout import PlatformFanoutCoordinator
from behavior.test_local_community_receipt_handoff import _Adapter
from behavior.test_platform_message_fanout_scenarios import _ThreeForumFixture
from tests import test_physical_mutation_recovery as mutation_fixture
from src.platform_types import ContentDocument, DeliveryPresentation


class DispatchCapacityScenarios(unittest.IsolatedAsyncioTestCase):
    """An in-flight connector write must not admit a second dispatch claim."""

    async def test_direct_send_and_recovery_share_slot_but_discord_progresses(self) -> None:
        """A blocked Fluxer write holds only Fluxer's slot, not Discord's slot."""
        capacity = ConnectorDispatchCapacity(('fluxer-local', 'discord-primary'))
        entered = asyncio.Event()
        release = asyncio.Event()
        competitor_entered = asyncio.Event()
        discord_completed = asyncio.Event()

        async def blocked_fluxer() -> None:
            """Represent a direct physical write already inside transport."""
            async with capacity.slot('fluxer-local'):
                entered.set()
                await release.wait()

        async def competing_fluxer() -> None:
            """Represent a recovery operation trying to get the same claim capacity."""
            async with capacity.slot('fluxer-local'):
                competitor_entered.set()

        async def independent_discord() -> None:
            """Represent an independently eligible Discord operation."""
            async with capacity.slot('discord-primary'):
                discord_completed.set()

        owner = asyncio.create_task(blocked_fluxer())
        await asyncio.wait_for(entered.wait(), 2)
        contender = asyncio.create_task(competing_fluxer())
        other = asyncio.create_task(independent_discord())
        try:
            await asyncio.wait_for(discord_completed.wait(), 2)
            self.assertFalse(competitor_entered.is_set())
            self.assertEqual(capacity.active_count('fluxer-local'), 1)
        finally:
            release.set()
            await asyncio.gather(owner, contender, other)
        self.assertTrue(competitor_entered.is_set())
        self.assertEqual(capacity.active_count('fluxer-local'), 0)

    async def test_shutdown_rejects_waiters_and_joins_admitted_work(self) -> None:
        """Closing capacity rejects a queued request, then drains its admitted owner."""
        capacity = ConnectorDispatchCapacity(('fluxer-local',))
        entered = asyncio.Event()
        release = asyncio.Event()
        waiter_admitted = False

        async def owner() -> None:
            """Hold a live send across shutdown admission closure."""
            async with capacity.slot('fluxer-local'):
                entered.set()
                await release.wait()

        async def waiter() -> None:
            """Observe that an already-queued request cannot enter after closure."""
            nonlocal waiter_admitted
            async with capacity.slot('fluxer-local'):
                waiter_admitted = True

        owner_task = asyncio.create_task(owner())
        await asyncio.wait_for(entered.wait(), 2)
        waiting_task = asyncio.create_task(waiter())
        await asyncio.sleep(0)
        capacity.close_admission()
        drain_task = asyncio.create_task(capacity.drain())
        self.assertFalse(drain_task.done())
        release.set()
        await owner_task
        with self.assertRaises(RuntimeError):
            await waiting_task
        await asyncio.wait_for(drain_task, 2)
        self.assertFalse(waiter_admitted)
        with self.assertRaises(RuntimeError):
            async with capacity.slot('fluxer-local'):
                self.fail('admitted after shutdown')

    async def test_child_wake_materializes_and_notifies_without_nested_http(self) -> None:
        """A confirmed parent only makes SQLite child work eligible for a lane."""
        wakes: list[str | None] = []
        class Intents:
            """Model atomic SQLite materialization without transport side effects."""
            def materialize_ready_blocked(self):
                """Return a durable ready child once and then an empty scan."""
                if not hasattr(self, 'once'):
                    self.once = True
                    return [23]
                return []
        coordinator = PlatformFanoutCoordinator(
            SimpleNamespace(platform_delivery_intents=Intents()), object(),
        )
        coordinator.bind_delivery_wake(lambda key=None: wakes.append(key))
        async def forbidden_send(_child_id):
            """A child HTTP call from the parent's receipt is forbidden."""
            raise AssertionError('nested child Create bypassed connector scheduler')
        coordinator.deliver = forbidden_send
        await coordinator._wake_blocked_children()
        self.assertEqual(wakes, [None])

    def test_short_lived_adapter_coordinator_inherits_runtime_gate(self) -> None:
        """Fresh direct coordinators use the adapter's bound runtime capacity."""
        adapter = _Adapter()
        capacity = ConnectorDispatchCapacity(('discord-primary',))
        adapter._physical_dispatch_gate = capacity
        coordinator = PlatformFanoutCoordinator.from_adapter(object(), adapter)
        self.assertIs(coordinator.dispatch_gate, capacity)


if __name__ == '__main__':
    unittest.main()


class DurableCreateCapacityScenarios(_ThreeForumFixture):
    """Enforce connector capacity before durable claim on an actual SQLite file."""

    def test_competing_direct_create_stays_pending_until_slot_released(self) -> None:
        """Another direct caller may persist an intent but cannot preclaim it."""
        from sqlalchemy import select
        from src.models import PlatformDeliveryIntent
        from behavior.test_platform_message_fanout_scenarios import _Registry

        entered = asyncio.Event()
        release = asyncio.Event()
        class SlowAdapter(_Adapter):
            """Pause the provider after the first physical HTTP method begins."""
            async def create_forum_thread(self, forum, content, *, title,
                                          presentation, attempt):
                """Keep A active while C is waiting for the connector slot."""
                if forum.external_channel_id == '100':
                    entered.set()
                    await release.wait()
                return await super().create_forum_thread(
                    forum, content, title=title, presentation=presentation,
                    attempt=attempt,
                )

        adapter = SlowAdapter()
        capacity = ConnectorDispatchCapacity(('discord-primary',))
        adapter._physical_dispatch_gate = capacity
        coordinator = PlatformFanoutCoordinator.from_adapter(self.database, adapter)

        async def send_to(label: str) -> bool:
            """Stage a real forum target and enter the exact Create claim path."""
            return await coordinator.stage_and_deliver(
                domain='local', kind='post', canonical_id=self.canonical_id,
                forum=self.forums[label], operation='forum_thread',
                destination=self.forums[label],
                content=ContentDocument('independent subscriber post'),
                presentation=DeliveryPresentation(),
                title='Shared source',
            )

        async def scenario() -> None:
            """Inspect the second intent while an unrelated flight owns capacity."""
            first = asyncio.create_task(send_to('A'))
            await asyncio.wait_for(entered.wait(), 3)
            second = asyncio.create_task(send_to('C'))
            try:
                # The pending intent is written before awaiting connector
                # admission. Wait for that observable state, not a timer.
                for _ in range(100):
                    with self.database.session() as session:
                        pending = session.scalar(select(PlatformDeliveryIntent).where(
                            PlatformDeliveryIntent.channel_surface_id == self.targets['C'][2]))
                        if pending is not None:
                            self.assertEqual(pending.state, 'pending')
                            self.assertIsNone(pending.dispatch_token)
                            break
                    await asyncio.sleep(0)
                else:
                    self.fail('second target did not durably stage its intent')
                self.assertEqual(adapter.posts, [])
                self.assertFalse(second.done())
            finally:
                release.set()
            self.assertEqual(await asyncio.gather(first, second), [True, True])
            self.assertEqual(adapter.posts, ['100', '300'])
            with self.database.session() as session:
                rows = session.scalars(select(PlatformDeliveryIntent)).all()
                self.assertEqual({row.state for row in rows}, {'delivered'})

        asyncio.run(scenario())


class DirectMutationCapacityScenarios(unittest.TestCase):
    """A direct edit call must not preclaim while recovery occupies its slot."""

    setUp = mutation_fixture.PhysicalMutationRecoveryScenarios.setUp
    _worker = mutation_fixture.PhysicalMutationRecoveryScenarios._worker

    def test_direct_mutation_waits_before_claim_in_real_sqlite(self) -> None:
        """A competing edit is still idle with no token while capacity is held."""
        from src.models import LocalCommunityThreadSurface
        gate = ConnectorDispatchCapacity(('discord-primary',))
        worker, remote_writes = self._worker()
        worker.dispatch_gate = gate
        held = asyncio.Event()
        release = asyncio.Event()

        async def holder() -> None:
            """Simulate an existing physical recovery operation in this lane."""
            async with gate.slot('discord-primary'):
                held.set()
                await release.wait()

        async def scenario() -> None:
            """Observe a pending canonical revision before any write reservation."""
            blocker = asyncio.create_task(holder())
            await asyncio.wait_for(held.wait(), 2)
            direct = asyncio.create_task(worker.drain(
                connector_key='discord-primary', limit=1))
            try:
                await asyncio.sleep(0)
                with self.db.session() as session:
                    copy = session.get(LocalCommunityThreadSurface, self.copy_id)
                    self.assertEqual(copy.mutation_state, 'idle')
                    self.assertIsNone(copy.mutation_claim_token)
                self.assertEqual(remote_writes, [])
            finally:
                release.set()
            await blocker
            self.assertEqual(await direct, 1)
            with self.db.session() as session:
                copy = session.get(LocalCommunityThreadSurface, self.copy_id)
                self.assertEqual(copy.applied_revision, 1)
                self.assertIsNone(copy.mutation_claim_token)
            self.assertEqual(len(remote_writes), 1)

        asyncio.run(scenario())
