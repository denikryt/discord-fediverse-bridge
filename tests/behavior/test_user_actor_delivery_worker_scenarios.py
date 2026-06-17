"""Behavior scenarios for process-owned User Actor delivery recovery."""

from __future__ import annotations

import asyncio

import pytest

from src.user_actor_delivery_worker import UserActorDeliveryWorker


class RecordingService:
    """Expose worker lifecycle effects without replacing asyncio ownership."""

    def __init__(self) -> None:
        self.calls = 0
        self.called = asyncio.Event()

    async def drain_once(self) -> int:
        """Record one bounded pass."""
        self.calls += 1
        self.called.set()
        return 0


@pytest.mark.asyncio
async def test_worker_drains_immediately_wakes_and_stops_without_orphan_task() -> None:
    """The runtime owner performs recovery before sleeping and is explicitly awaited."""
    service = RecordingService()
    worker = UserActorDeliveryWorker(service)
    worker.start()
    await asyncio.wait_for(service.called.wait(), timeout=1)
    service.called.clear()
    worker.wake()
    await asyncio.wait_for(service.called.wait(), timeout=1)
    await worker.stop()

    assert service.calls >= 2
    assert worker._task is None


class RecoveringService:
    """Fail one startup pass, then expose successful later recovery."""

    def __init__(self) -> None:
        self.calls = 0
        self.recovered = asyncio.Event()

    async def drain_once(self) -> int:
        """Model an unavailable gateway without terminating the owner task."""
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("gateway unavailable")
        self.recovered.set()
        return 1


@pytest.mark.asyncio
async def test_worker_isolates_unavailable_gateway_during_startup_and_recovers() -> (
    None
):
    """A failed immediate drain does not block runtime ownership or later work."""
    service = RecoveringService()
    worker = UserActorDeliveryWorker(service)
    worker.start()
    while service.calls == 0:
        await asyncio.sleep(0)
    worker.wake()
    await asyncio.wait_for(service.recovered.wait(), timeout=1)
    await worker.stop()

    assert service.calls >= 2


class BlockingOutcomeService:
    """Model transport completion followed by a durable outcome boundary."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.transport_completed = asyncio.Event()
        self.allow_return = asyncio.Event()
        self.outcome_persisted = asyncio.Event()
        self.calls = 0

    async def drain_once(self) -> int:
        self.calls += 1
        self.transport_completed.set()
        await self.allow_return.wait()
        self.outcome_persisted.set()
        if self.fail:
            raise RuntimeError("request failed after durable recovery state")
        return 1


@pytest.mark.asyncio
async def test_stop_allows_inflight_transport_outcome_to_reach_durable_boundary() -> (
    None
):
    service = BlockingOutcomeService()
    worker = UserActorDeliveryWorker(service)
    worker.start()
    await asyncio.wait_for(service.transport_completed.wait(), timeout=1)

    stop_task = asyncio.create_task(worker.stop())
    await asyncio.sleep(0)
    assert not stop_task.done()
    assert not service.outcome_persisted.is_set()

    service.allow_return.set()
    await asyncio.wait_for(stop_task, timeout=1)

    assert service.outcome_persisted.is_set()
    assert service.calls == 1
    assert worker._task is None


@pytest.mark.asyncio
async def test_stop_allows_request_failure_to_persist_recoverable_state() -> None:
    service = BlockingOutcomeService(fail=True)
    worker = UserActorDeliveryWorker(service)
    worker.start()
    await asyncio.wait_for(service.transport_completed.wait(), timeout=1)

    stop_task = asyncio.create_task(worker.stop())
    await asyncio.sleep(0)
    assert not stop_task.done()

    service.allow_return.set()
    await asyncio.wait_for(stop_task, timeout=1)

    assert service.outcome_persisted.is_set()
    assert service.calls == 1
    assert worker._task is None
