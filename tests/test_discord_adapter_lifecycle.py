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

@pytest.mark.asyncio
async def test_quiesce_joins_a_blocked_physical_read_before_sdk_close():
    """A blocked SDK read must not escape quiesce and touch a closed client."""
    from types import SimpleNamespace
    from src.platform_types import ChannelRef
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    class BlockingSdk:
        """Represent an SDK client whose channel GET never returns."""
        def get_channel(self, _channel_id):
            """Ensure adapter proceeds to a network-backed read."""
            return None

        async def fetch_channel(self, _channel_id):
            """Report cancellation when adapter drains physical reads."""
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    transport = DiscordAdapter(connector=ConnectorRef('discord-primary', 'discord'), bot=BlockingSdk())
    fetch_task = asyncio.create_task(transport.fetch_channel(ChannelRef('discord-primary', '123')))
    await entered.wait()
    await transport.drain_ingress()
    await asyncio.wait_for(transport.quiesce(), timeout=0.5)
    assert fetch_task.done() and cancelled.is_set()
    assert not transport._io_tasks


@pytest.mark.asyncio
async def test_physical_operation_rejected_when_sdk_io_quiesced():
    """A late HTTP handler cannot start a new SDK read after drain completes."""
    from src.platform_types import ChannelRef
    transport = adapter()
    await transport.drain_ingress()
    await transport.quiesce()
    with pytest.raises(PlatformOperationError) as error:
        await transport.fetch_channel(ChannelRef('discord-primary', '123'))
    assert error.value.reason == 'unavailable'

@pytest.mark.asyncio
async def test_shutdown_cancellation_of_create_keeps_one_ambiguous_receipt(monkeypatch):
    """Stopping an in-flight physical POST never authorizes a second POST."""
    from src.platform_types import ChannelRef, ContentDocument, CreateAttempt, DeliveryPresentation
    from test_discord_adapter_transport import FakeForum, FakeBot
    import sys
    from types import SimpleNamespace

    begun = asyncio.Event()

    class FrozenForum(FakeForum):
        """Model a Create whose physical response is lost during shutdown."""
        async def create_thread(self, **kwargs):
            """Record the POST before awaiting the unresolved HTTP response."""
            self.sends.append(kwargs)
            begun.set()
            await asyncio.Event().wait()

    class AllowedMentions:
        """Minimal external SDK edge needed to construct the physical POST."""
        @staticmethod
        def none():
            """Suppress mentions on generated ActivityPub content."""
            return 'no-mentions'

    monkeypatch.setitem(sys.modules, 'discord', SimpleNamespace(
        AllowedMentions=AllowedMentions, ForumChannel=FrozenForum))
    forum = FrozenForum()
    transport = DiscordAdapter(connector=ConnectorRef('discord-primary', 'discord'),
                               bot=FakeBot([forum]))
    channel = ChannelRef('discord-primary', '300', 'forum', '10')
    attempt = CreateAttempt('post-shutdown-1', 'discord-primary', 'forum_thread', channel)

    async def publish():
        """Send one physical forum Create with immutable attempt identity."""
        return await transport.create_forum_thread(
            channel, ContentDocument('hello'), title='remote post',
            presentation=DeliveryPresentation('bot'), attempt=attempt)

    owner = asyncio.create_task(publish())
    await begun.wait()
    await transport.drain_ingress()
    await transport.quiesce()
    assert owner.cancelled()
    assert len(forum.sends) == 1
    with pytest.raises(PlatformOperationError) as error:
        await transport.reconcile_create(attempt)
    assert error.value.reason == 'unavailable'
    # No late caller can repeat a physical Create after SDK admission closes.
    with pytest.raises(PlatformOperationError) as error:
        await publish()
    assert error.value.reason == 'unavailable'
    assert len(forum.sends) == 1

@pytest.mark.asyncio
async def test_second_shutdown_cancellation_cannot_abandon_gateway_callback():
    """Two independent cancellations still join the gateway task before SDK close."""
    transport = adapter()
    entered = asyncio.Event()
    cancelling = asyncio.Event()
    release = asyncio.Event()

    async def callback():
        """Simulate a Fedify operation requiring async cleanup after cancellation."""
        assert transport.enter_ingress()
        try:
            entered.set()
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelling.set()
            await release.wait()
            raise
        finally:
            transport.leave_ingress()

    pending = asyncio.create_task(callback())
    await entered.wait()
    transport.close_ingress_admission()
    drainer = asyncio.create_task(transport.drain_ingress())
    await cancelling.wait()
    drainer.cancel()
    await asyncio.sleep(0.01)
    assert not drainer.done(), 'drain must wait for callback cleanup before SDK close'
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await drainer
    assert pending.done() and not transport._ingress_tasks
    await transport.quiesce()
