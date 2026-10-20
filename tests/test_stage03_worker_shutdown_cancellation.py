"""A second shutdown cancellation cannot detach active recovery worker tasks."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from src.personal_feed_discord_delivery_worker import PersonalFeedDiscordDeliveryWorker
from src.user_actor_delivery_worker import UserActorDeliveryWorker


@pytest.mark.asyncio
async def test_personal_feed_stop_joins_async_finalizer_after_second_cancel():
    """A due Discord delivery cannot resume after database close, even during cancellation."""
    recovered = []
    cleanup_started = asyncio.Event()
    allow_cleanup = asyncio.Event()

    async def active_delivery():
        """Model an in-flight Discord POST that must checkpoint its finally."""
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_started.set()
            await allow_cleanup.wait()

    db = SimpleNamespace(personal_feed_conversations=SimpleNamespace(
        recover_dispatching=lambda: recovered.append(True),
    ))
    worker = PersonalFeedDiscordDeliveryWorker(
        database=db, adapter=object(), personal_feed_service=object(),
    )
    delivery_task = asyncio.create_task(active_delivery())
    await asyncio.sleep(0)
    worker._task = delivery_task
    shutdown = asyncio.create_task(worker.stop())
    await cleanup_started.wait()
    shutdown.cancel()
    await asyncio.sleep(0.01)
    assert not shutdown.done(), 'worker stop must join delayed finalizer'
    allow_cleanup.set()
    with pytest.raises(asyncio.CancelledError):
        await shutdown
    assert delivery_task.done() and recovered == [True]


@pytest.mark.asyncio
async def test_user_actor_stop_joins_active_fedify_pass_after_second_cancel():
    """The User Actor worker cannot continue sending after SDK/Fedify closure."""
    pass_started = asyncio.Event()
    release = asyncio.Event()

    async def active_pass():
        """Model an already running ActivityPub send that needs a final receipt."""
        pass_started.set()
        await release.wait()

    worker = UserActorDeliveryWorker(service=object())
    active = asyncio.create_task(active_pass())
    await pass_started.wait()
    worker._task = active
    shutdown = asyncio.create_task(worker.stop())
    await asyncio.sleep(0)
    shutdown.cancel()
    await asyncio.sleep(0.01)
    assert not shutdown.done(), 'worker stop must join the active Fedify pass'
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await shutdown
    assert active.done() and worker._task is None
