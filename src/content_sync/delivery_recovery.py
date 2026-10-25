"""Restart-safe inspection and reactivation of SQLite physical Create intents.

Only definitely unsent pending requests are dispatched. A possibly submitted
orphan/unknown Create is never guessed absent or automatically sent again.
This module is a helper for the existing coordinator, not a second worker.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .platform_fanout import PlatformFanoutCoordinator

logger = logging.getLogger(__name__)


async def recover_pending_creates(coordinator: PlatformFanoutCoordinator) -> list[int]:
    """Resume safe pending work using coordinator-owned dispatch and reconciliation.

    The durable repository remains the sole arbiter of each send's status,
    physical parent and source ownership; an expired claim is uncertain.
    """
    repository = coordinator.database.platform_delivery_intents
    repository.expire_claims()
    # Verified orphans must remain visible for explicitly authorized operator
    # cleanup. Never treat an orphan receipt as an invitation to re-POST.
    for orphan in repository.inspect_orphans():
        logger.warning(
            'Verified orphan Create requires operator review: intent_id=%s cleanup=%s',
            orphan['intent_id'], orphan['cleanup_state'],
        )
    # Materialization uses the exact SQLite parent receipt and authoritative
    # current content. It never thaws a deleted or re-bound source.
    repository.materialize_ready_blocked()
    # Reconciliation must be read-only. A new process normally has no proof of
    # which physical message (if any) was created by an uncertain HTTP call.
    for intent_id in repository.unresolved_ids():
        try:
            await coordinator.reconcile_unknown(intent_id)
        except Exception:
            logger.exception('Read-only Create reconciliation failed intent_id=%s', intent_id)
    completed: list[int] = []
    for intent_id in repository.recoverable_pending_ids():
        if await coordinator.deliver(intent_id):
            completed.append(intent_id)
    return completed
