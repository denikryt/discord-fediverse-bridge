"""ActivityPub event receipt and idempotency persistence."""

from __future__ import annotations

from sqlalchemy import select

from ...inbound_activity_outcomes import InboundActivityOutcome
from ...models import ActivityPubEventReceipt
from .base import BaseRepository


class EventReceiptRepository(BaseRepository):
    """Persist receipt lifecycle state and semantic outcomes atomically."""

    def get_event_receipt(self, delivery_id: str) -> ActivityPubEventReceipt | None:
        """Load the receipt row for one inbound delivery ID."""
        with self.session() as session:
            return session.scalar(
                select(ActivityPubEventReceipt).where(
                    ActivityPubEventReceipt.delivery_id == delivery_id
                )
            )

    def create_event_receipt(
        self,
        *,
        delivery_id: str,
        event_type: str,
        object_ap_id: str,
        status: str,
        outcome: InboundActivityOutcome | str | None = None,
        detail: str | None = None,
    ) -> ActivityPubEventReceipt:
        """Create the receipt row that gates idempotent event processing."""
        with self.session() as session:
            receipt = ActivityPubEventReceipt(
                delivery_id=delivery_id,
                event_type=event_type,
                object_ap_id=object_ap_id,
                status=status,
                outcome=_canonical_outcome(outcome),
                detail=detail,
            )
            session.add(receipt)
            session.flush()
            return receipt

    def update_event_receipt(
        self,
        *,
        delivery_id: str,
        status: str,
        outcome: InboundActivityOutcome | str | None,
        detail: str | None = None,
    ) -> None:
        """Update lifecycle state, outcome, and detail in one transaction."""
        with self.session() as session:
            receipt = session.scalar(
                select(ActivityPubEventReceipt).where(
                    ActivityPubEventReceipt.delivery_id == delivery_id
                )
            )
            if receipt is None:
                raise RuntimeError(f"Missing receipt for delivery {delivery_id}")
            # Keep these fields together so tooling never observes a terminal
            # status paired with an outcome from a previous attempt.
            receipt.status = status
            receipt.outcome = _canonical_outcome(outcome)
            receipt.detail = detail


def _canonical_outcome(outcome: InboundActivityOutcome | str | None) -> str | None:
    """Return the persisted string representation of an optional outcome."""
    if outcome is None:
        return None
    return outcome.value if isinstance(outcome, InboundActivityOutcome) else str(outcome)
