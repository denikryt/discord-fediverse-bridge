from __future__ import annotations

from datetime import datetime

from sqlalchemy import select

from ...models import (
    ActivityPubEventReceipt,
    RegistrationSession,
    User,
)
from .base import BaseRepository


"""ActivityPub event receipt and idempotency persistence."""


class EventReceiptRepository(BaseRepository):
    """Persist the event receipts domain."""

    def get_event_receipt(self, delivery_id: str) -> ActivityPubEventReceipt | None:
            """Load the receipt row for one inbound delivery ID."""
            with self.session() as session:
                return session.scalar(select(ActivityPubEventReceipt).where(ActivityPubEventReceipt.delivery_id == delivery_id))

    def create_event_receipt(self, *, delivery_id: str, event_type: str, object_ap_id: str, status: str, detail: str | None = None) -> ActivityPubEventReceipt:
            """Create the receipt row that gates idempotent event processing."""
            with self.session() as session:
                receipt = ActivityPubEventReceipt(
                    delivery_id=delivery_id,
                    event_type=event_type,
                    object_ap_id=object_ap_id,
                    status=status,
                    detail=detail,
                )
                session.add(receipt)
                session.flush()
                return receipt

    def update_event_receipt(self, *, delivery_id: str, status: str, detail: str | None = None) -> None:
            """Update one inbound delivery receipt after processing progresses."""
            with self.session() as session:
                receipt = session.scalar(select(ActivityPubEventReceipt).where(ActivityPubEventReceipt.delivery_id == delivery_id))
                if receipt is None:
                    raise RuntimeError(f"Missing receipt for delivery {delivery_id}")
                receipt.status = status
                receipt.detail = detail
