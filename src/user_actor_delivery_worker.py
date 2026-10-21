"""Process-owned recovery loop for durable User Actor delivery work."""

from __future__ import annotations

import asyncio
import logging

from .user_actor_delivery import UserActorDeliveryService

logger = logging.getLogger(__name__)


class UserActorDeliveryWorker:
    """Own exactly one bounded periodic recovery task for the live runtime."""

    INTERVAL_SECONDS = 15.0
    SHUTDOWN_GRACE_SECONDS = 30.0

    def __init__(self, service: UserActorDeliveryService) -> None:
        self.service = service
        self._task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self._stopping = False

    def start(self) -> None:
        """Start one recovery owner and reject duplicate starts."""
        if self._task is not None:
            raise RuntimeError("User Actor delivery worker already started")
        self._task = asyncio.create_task(self._run(), name="user-actor-delivery-worker")

    def wake(self) -> None:
        """Request a prompt bounded drain after a new source commits."""
        self._wake.set()

    async def stop(self) -> None:
        """Join the current Fedify pass before shared clients can be closed.

        A shutdown-task cancellation is retained and propagated only after the
        existing bounded grace period and, if needed, forced worker cancellation.
        No in-flight delivery may outlive the caller of this stop method.
        """
        self._stopping = True
        self._wake.set()
        task = self._task
        if task is None:
            return
        interrupted = False
        deadline = asyncio.get_running_loop().time() + self.SHUTDOWN_GRACE_SECONDS
        try:
            while not task.done():
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    task.cancel()
                    break
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=remaining)
                except TimeoutError:
                    # Shield keeps timeout from cancelling a partially sent
                    # ActivityPub POST until the explicit grace deadline.
                    task.cancel()
                    break
                except asyncio.CancelledError:
                    if not task.done():
                        interrupted = True
            # Even after a grace timeout, join the task's async finally before
            # releasing Fedify and database ownership to Runtime.close().
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    if not task.done():
                        interrupted = True
            if not task.cancelled():
                task.result()
        finally:
            if task.done():
                self._task = None
        if interrupted:
            raise asyncio.CancelledError

    async def _run(self) -> None:
        """Drain immediately, then continue with failure-isolated cadence."""
        while not self._stopping:
            # Clear before the pass so a mutation committed during the drain
            # remains visible and triggers an immediate follow-up pass.
            self._wake.clear()
            try:
                await self.service.drain_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("User Actor delivery recovery pass failed")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.INTERVAL_SECONDS)
            except TimeoutError:
                pass
