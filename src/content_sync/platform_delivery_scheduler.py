"""One bounded runtime-owned scheduler for connector-isolated physical recovery.

Existing Create and mutation repositories remain the sole durable claim owners;
this process-local scheduler holds no messages, send attempts or replay queue.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from collections.abc import Iterable

logger = logging.getLogger(__name__)


class DispatchAdmissionClosed(RuntimeError):
    """A bounded runtime is quiescing and cannot admit new physical dispatch."""


class ConnectorDispatchCapacity:
    """One runtime-owned physical operation slot per configured connector.

    Both a fresh direct sender and its recovery lane must enter this gate before
    a durable claim. It is process-local admission, not a second claim ledger.
    """

    def __init__(self, connector_keys: Iterable[str]) -> None:
        """Allocate exact connector slots once for the containing runtime."""
        keys = tuple(connector_keys)
        if not keys or len(keys) != len(set(keys)) or len(keys) > 8:
            raise ValueError('invalid connector dispatch capacity')
        self.connector_keys = keys
        self._slots = {key: asyncio.Lock() for key in keys}
        self._active: set[asyncio.Task] = set()
        self._open = True
        self._wake_callback = None

    def bind_wake(self, callback) -> None:
        """Bind the already-constructed scheduler's ephemeral notification only once."""
        if self._wake_callback is not None:
            raise RuntimeError('physical delivery wake already bound')
        self._wake_callback = callback

    def wake(self, connector_key: str | None = None) -> None:
        """Notify lanes of durable eligible work, without sending it inline."""
        if self._open and self._wake_callback is not None:
            self._wake_callback(connector_key)

    def active_count(self, connector_key: str) -> int:
        """Expose the connector occupancy for bounded-concurrency assertions."""
        return int(self._slots[connector_key].locked())

    @asynccontextmanager
    async def slot(self, connector_key: str):
        """Hold connector admission across preflight, claim and its physical result.

        A waiter must recheck admission after acquiring the asyncio lock:
        shutdown can close the gate while it is waiting for an older send.
        """
        if not self._open:
            raise DispatchAdmissionClosed('physical dispatch admission is closed')
        try:
            lock = self._slots[connector_key]
        except KeyError as exc:
            raise LookupError('unconfigured physical connector') from exc
        async with lock:
            if not self._open:
                raise DispatchAdmissionClosed('physical dispatch admission is closed')
            task = asyncio.current_task()
            if task is None:
                raise RuntimeError('physical dispatch requires an asyncio Task')
            self._active.add(task)
            try:
                yield
            finally:
                self._active.discard(task)

    def close_admission(self) -> None:
        """Refuse newly arriving or waiting dispatch before shutdown's first await."""
        self._open = False

    async def drain(self) -> None:
        """Join every accepted direct send before adapter and SQLite disposal."""
        while self._active:
            tasks = tuple(self._active)
            # An accepted task may be cancelled, but its coordinator must
            # persist an uncertain write outcome before leaving its slot.
            await asyncio.gather(*tasks, return_exceptions=True)


class PlatformPhysicalDeliveryScheduler:
    """Supervise one independently progressing physical recovery lane per connector."""

    def __init__(self, *, connector_keys: Iterable[str], fanout, mutations,
                 poll_seconds: float = 5.0, max_connectors: int = 8) -> None:
        """Bind shared SQLite coordinators; reject over-capacity configurations."""
        connectors = tuple(connector_keys)
        if (not connectors or len(connectors) > max_connectors
            or len(connectors) != len(set(connectors))
            or any(not isinstance(key, str) or not key for key in connectors)
            or poll_seconds <= 0 or max_connectors <= 0):
            raise ValueError('invalid or excessive physical connector lanes')
        self._connectors = connectors
        self._fanout = fanout
        self._mutations = mutations
        self._poll_seconds = poll_seconds
        self._wake = {key: asyncio.Event() for key in connectors}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._stopping = False

    @property
    def connector_keys(self) -> tuple[str, ...]:
        """Expose the immutable configured lane identities for runtime validation."""
        return self._connectors

    def start(self) -> None:
        """Start each configured connector once without an extra durable queue."""
        if self._stopping:
            raise RuntimeError('cannot restart a stopped physical scheduler')
        if self._tasks:
            return
        self._tasks = {key: asyncio.create_task(
            self._run_lane(key), name=f'physical-delivery:{key}'
        ) for key in self._connectors}

    def wake(self, connector_key: str | None = None) -> None:
        """Signal connector lane(s); lost signals recover via bounded polling."""
        if self._stopping:
            return
        if connector_key is not None:
            try:
                self._wake[connector_key].set()
            except KeyError as exc:
                raise ValueError('unknown connector wake') from exc
        else:
            for wake in self._wake.values():
                wake.set()

    async def _run_lane(self, key: str) -> None:
        """Never await another connector's REST, reconciliation or mutation work."""
        round_robin = 0
        while not self._stopping:
            wake = self._wake[key]
            wake.clear()
            try:
                adapter = self._fanout.registry.get(key)
                readiness = getattr(adapter, 'dispatch_ready', None)
                if readiness is None or readiness():
                    # Alternate priority each pass so reconciliations, Creates
                    # and mutations can all progress without a second worker.
                    if round_robin % 2 == 0:
                        await self._fanout.recover_pending(connector_key=key)
                        await self._mutations.drain(connector_key=key, limit=1)
                    else:
                        await self._mutations.drain(connector_key=key, limit=1)
                        await self._fanout.recover_pending(connector_key=key)
                    round_robin += 1
            except asyncio.CancelledError:
                raise
            except LookupError:
                # Unregistered/offline is normal for a disabled connector; no
                # durable claim has been acquired by the scheduler itself.
                pass
            except Exception:
                logger.exception('Physical recovery lane failed connector=%s', key)
            if not self._stopping:
                try:
                    await asyncio.wait_for(wake.wait(), self._poll_seconds)
                except TimeoutError:
                    pass

    async def stop(self) -> None:
        """Close wake admission, cancel and join every bounded lane before DB close."""
        self._stopping = True
        for wake in self._wake.values():
            wake.set()
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        completion = asyncio.gather(*tasks, return_exceptions=True)
        interrupted = False
        while not completion.done():
            try:
                await asyncio.shield(completion)
            except asyncio.CancelledError:
                interrupted = True
        errors = [value for value in completion.result()
                  if isinstance(value, BaseException) and not isinstance(value, asyncio.CancelledError)]
        self._tasks.clear()
        if errors:
            raise BaseExceptionGroup('Physical scheduler shutdown failures', errors)
        if interrupted:
            raise asyncio.CancelledError
