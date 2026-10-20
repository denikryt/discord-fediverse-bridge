"""Shutdown actions -> all worker/transport owners drained before SDK closes.

The Discord network is replaced at its client context boundary. No timers or
live Gateway connections are needed to observe lifetime ordering.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from src.app import _run_bot_lifetime


class FakeBot:
    """Track whether the borrowed Discord SDK closes after shutdown actions."""

    def __init__(self, steps, *, failure=None, pause=False):
        """Make the SDK start either finish, fail, or await cancellation."""
        self.steps = steps
        self.failure = failure
        self.pause = pause
        self.started = asyncio.Event()

    async def __aenter__(self):
        """Open one Discord socket context."""
        self.steps.append('sdk-enter')
        return self

    async def __aexit__(self, *_):
        """Reveal exactly when Discord's SDK resources are finally closed."""
        self.steps.append('sdk-close')

    async def start(self, token):
        """Model the long-lived Gateway task and its failure modes."""
        assert token == 'dummy'
        self.steps.append('gateway-start')
        self.started.set()
        if self.failure:
            raise self.failure
        if self.pause:
            await asyncio.Event().wait()


class FakeAdapter:
    """Record admission closure, ingress drain, and SDK I/O drain."""

    def __init__(self, steps):
        self.steps = steps

    def close_ingress_admission(self):
        """Close the synchronous ingress gate before the next await."""
        self.steps.append('ingress-close')

    async def drain_ingress(self):
        """Join callbacks that may still be using the database and Fedify."""
        self.steps.append('ingress-drain')

    async def quiesce(self):
        """Drain Discord HTTP tasks only after both recovery workers stop."""
        self.steps.append('io-drain')


class FakeWorker:
    """Observe one worker's stop even if its sibling fails."""

    def __init__(self, steps, name, *, failure=None):
        self.steps = steps
        self.name = name
        self.failure = failure

    async def stop(self):
        """Stop process-owned retries and optionally raise a shutdown error."""
        self.steps.append(self.name)
        if self.failure:
            raise self.failure


def world(*, gateway_failure=None, feed_failure=None, pause=False):
    """Construct the actual lifetime collaborators behind their outer edges."""
    steps = []
    runtime = SimpleNamespace(
        bot=FakeBot(steps, failure=gateway_failure, pause=pause),
        discord_adapter=FakeAdapter(steps),
        personal_feed_discord_delivery_worker=FakeWorker(steps, 'feed-stop', failure=feed_failure),
        user_actor_delivery_worker=FakeWorker(steps, 'user-stop'),
    )
    return runtime, steps


@pytest.mark.asyncio
async def test_gateway_normal_completion_stops_both_workers_before_sdk_close():
    """No recovery worker can use a closed SDK or shared Fedify client."""
    runtime, steps = world()
    await _run_bot_lifetime(runtime, 'dummy')
    assert steps == ['sdk-enter', 'gateway-start', 'ingress-close',
                     'ingress-drain', 'feed-stop', 'user-stop', 'io-drain', 'sdk-close']


@pytest.mark.asyncio
async def test_gateway_failure_still_drains_before_context_exit():
    """TaskGroup sibling exceptions must not cause early SDK shutdown."""
    runtime, steps = world(gateway_failure=OSError('socket failed'))
    with pytest.raises(OSError, match='socket failed'):
        await _run_bot_lifetime(runtime, 'dummy')
    assert steps[-6:] == ['ingress-close', 'ingress-drain', 'feed-stop',
                          'user-stop', 'io-drain', 'sdk-close']


@pytest.mark.asyncio
async def test_first_worker_stop_error_does_not_skip_second_worker_or_io_drain():
    """One failed worker shutdown cannot abandon a second active worker."""
    runtime, steps = world(feed_failure=RuntimeError('stop failed'))
    with pytest.raises(RuntimeError, match='stop failed'):
        await _run_bot_lifetime(runtime, 'dummy')
    assert steps[-4:] == ['feed-stop', 'user-stop', 'io-drain', 'sdk-close']


@pytest.mark.asyncio
async def test_cancelled_gateway_joins_workers_before_context_exit():
    """A TaskGroup cancellation still performs the same bounded cleanup."""
    runtime, steps = world(pause=True)
    task = asyncio.create_task(_run_bot_lifetime(runtime, 'dummy'))
    await runtime.bot.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert steps[-6:] == ['ingress-close', 'ingress-drain', 'feed-stop',
                          'user-stop', 'io-drain', 'sdk-close']
