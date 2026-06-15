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
        """Stop new passes and allow the in-flight bounded pass to finish."""
        self._stopping = True
        self._wake.set()
        task = self._task
        if task is None:
            return
        try:
            await asyncio.wait_for(
                asyncio.shield(task), timeout=self.SHUTDOWN_GRACE_SECONDS
            )
        except TimeoutError:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        finally:
            self._task = None

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
                await asyncio.wait_for(
                    self._wake.wait(), timeout=self.INTERVAL_SECONDS
                )
            except TimeoutError:
                pass
