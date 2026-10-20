"""Runtime action -> shutdown lifecycle guarantees for independent Gateway callbacks."""

from __future__ import annotations

import asyncio
import pytest

from src.discord_adapter import DiscordAdapter
from src.platform_types import ConnectorRef, PlatformOperationError


def adapter():
    """Borrow a fake SDK while exercising real adapter task lifetime admission."""
    return DiscordAdapter(connector=ConnectorRef('discord-primary', 'discord'), bot=object())


@pytest.mark.asyncio
async def test_gateway_callback_waiting_on_fedify_is_cancelled_and_joined():
    """Shutdown must drain an ingress callback even if it performs zero SDK I/O."""
    transport = adapter()
    entered = asyncio.Event()
    stopped = asyncio.Event()

    async def on_message():
        """Simulate a Discord callback waiting for an ActivityPub response."""
        if not transport.enter_ingress():
            return
        try:
            entered.set()
            await asyncio.Event().wait()
        finally:
            stopped.set()
            transport.leave_ingress()

    callback = asyncio.create_task(on_message())
    await entered.wait()
    transport.close_ingress_admission()
    await transport.drain_ingress()
    assert stopped.is_set()
    assert callback.done()
    assert not transport._ingress_tasks
    await transport.quiesce()
    assert transport.enter_ingress() is False


@pytest.mark.asyncio
async def test_late_on_ready_cannot_enter_after_shutdown():
    """A callback dispatched after the gate closes cannot restart workers."""
    transport = adapter()
    started = []

    async def late_ready():
        """Represent the first instruction in the SDK on_ready callback."""
        if not transport.enter_ingress():
            return
        try:
            started.append('worker-start')
        finally:
            transport.leave_ingress()

    transport.close_ingress_admission()
    await late_ready()
    await transport.drain_ingress()
    await transport.quiesce()
    assert started == []


@pytest.mark.asyncio
async def test_normal_ingress_admission_allows_callback_then_drains():
    """Admission remains open during normal operation and closes idempotently."""
    transport = adapter()
    observed = []

    async def callback():
        """Use the normal callback enter/leave lifetime around shared state."""
        if not transport.enter_ingress():
            return
        try:
            observed.append('event')
        finally:
            transport.leave_ingress()

    await callback()
    assert observed == ['event']
    await transport.drain_ingress()
    await transport.drain_ingress()
    await transport.quiesce()
    with pytest.raises(PlatformOperationError):
        await transport._begin_create_attempt(
            None, operation='message', destination=None, content=None,
            presentation=None, title=None, reply_to=None, send=None,
        )
