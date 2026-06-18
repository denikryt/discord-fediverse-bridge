"""Worker-side outcome persistence for User Actor deliveries."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable

from sqlalchemy import delete, func, select, update

from ...models import (
    UserActorOutboundDelivery,
    UserActorOutboundSource,
    UserActorOutboundSourceMedia,
)
from .base import BaseRepository
from .user_actor_delivery_types import bound_delivery_error


class UserActorDeliveryOutcomeRepository(BaseRepository):
    """Persist worker claim outcomes and source status summaries."""

    def __init__(self, session_provider: Callable) -> None:
        """Bind the ordinary session boundary used for compare-and-set outcomes."""
        super().__init__(session_provider)

    def mark_ineligible(self, delivery_id: int, claim_token: str, reason: str) -> None:
        """Terminally suppress one currently claimed endpoint without transport."""
        with self.session() as session:
            delivery = session.scalar(
                select(UserActorOutboundDelivery).where(
                    UserActorOutboundDelivery.id == delivery_id,
                    UserActorOutboundDelivery.status == "sending",
                    UserActorOutboundDelivery.claim_token == claim_token,
                )
            )
            if delivery is None:
                return
            delivery.status = "ineligible"
            delivery.claim_token = None
            delivery.claimed_at = None
            delivery.last_error = bound_delivery_error(reason)
            delivery.next_attempt_at = None

    def release_unsubmitted_claim(
        self, delivery_id: int, claim_token: str, error: str
    ) -> None:
        """Return a pre-transport claim to retryable failure without an attempt."""
        now = datetime.now(timezone.utc)
        with self.session() as session:
            delivery = session.scalar(
                select(UserActorOutboundDelivery).where(
                    UserActorOutboundDelivery.id == delivery_id,
                    UserActorOutboundDelivery.status == "sending",
                    UserActorOutboundDelivery.claim_token == claim_token,
                )
            )
            if delivery is None:
                return
            delivery.status = "failed"
            delivery.claim_token = None
            delivery.claimed_at = None
            delivery.last_error = bound_delivery_error(error)
            delivery.next_attempt_at = now + timedelta(seconds=15)

    def mark_submitted(self, delivery_id: int, claim_token: str) -> bool:
        """Count one actual gateway submission for the currently owned claim."""
        now = datetime.now(timezone.utc)
        with self.session() as session:
            result = session.execute(
                update(UserActorOutboundDelivery)
                .where(
                    UserActorOutboundDelivery.id == delivery_id,
                    UserActorOutboundDelivery.status == "sending",
                    UserActorOutboundDelivery.claim_token == claim_token,
                )
                .values(
                    attempt_count=UserActorOutboundDelivery.attempt_count + 1,
                    last_attempted_at=now,
                )
            )
            return result.rowcount == 1

    def record_outcome(
        self, delivery_id: int, claim_token: str, *, ok: bool, error: str | None = None
    ) -> None:
        """Complete only the currently owned claim and preserve retryability."""
        now = datetime.now(timezone.utc)
        with self.session() as session:
            delivery = session.scalar(
                select(UserActorOutboundDelivery).where(
                    UserActorOutboundDelivery.id == delivery_id,
                    UserActorOutboundDelivery.status == "sending",
                    UserActorOutboundDelivery.claim_token == claim_token,
                )
            )
            if delivery is None:
                return
            delivery.claim_token = None
            delivery.claimed_at = None
            if ok:
                delivery.status = "delivered"
                delivery.delivered_at = now
                delivery.last_error = None
                delivery.next_attempt_at = None
                source = session.get(UserActorOutboundSource, delivery.source_id)
                if source is not None and source.source_kind == "content_delete":
                    older_ids = session.scalars(
                        select(UserActorOutboundDelivery.id)
                        .join(UserActorOutboundSource)
                        .where(
                            UserActorOutboundSource.ordering_key == source.ordering_key,
                            UserActorOutboundSource.logical_version
                            < source.logical_version,
                            UserActorOutboundDelivery.target_endpoint_url
                            == delivery.target_endpoint_url,
                            UserActorOutboundDelivery.status.in_(("pending", "failed")),
                        )
                    ).all()
                    if older_ids:
                        # Successful Delete is the current terminal lifecycle event for
                        # this endpoint, so older unsent content rows are finalized.
                        session.execute(
                            update(UserActorOutboundDelivery)
                            .where(UserActorOutboundDelivery.id.in_(older_ids))
                            .values(
                                status="superseded",
                                claim_token=None,
                                claimed_at=None,
                                next_attempt_at=None,
                                last_error=None,
                            )
                        )
            else:
                delivery.status = "failed"
                delivery.last_error = bound_delivery_error(error or "delivery failed")
                delivery.next_attempt_at = now + timedelta(
                    seconds=min(300, 2 ** min(delivery.attempt_count, 8))
                )

    def release_terminal_source_media(self, source_id: int) -> int:
        """Release source media only after rendering and all targets are terminal."""
        with self.session() as session:
            source = session.get(UserActorOutboundSource, source_id)
            if source is None or source.state != "ready":
                return 0
            unfinished = session.scalar(
                select(UserActorOutboundDelivery.id)
                .where(
                    UserActorOutboundDelivery.source_id == source_id,
                    UserActorOutboundDelivery.status.in_(
                        ("pending", "sending", "failed")
                    ),
                )
                .limit(1)
            )
            if unfinished is not None:
                return 0
            result = session.execute(
                delete(UserActorOutboundSourceMedia).where(
                    UserActorOutboundSourceMedia.source_id == source_id
                )
            )
            return int(result.rowcount or 0)

    def source_summary(self, source_id: int) -> tuple[int, int, int]:
        """Return delivered, pending/sending/failed outstanding, and total counts."""
        with self.session() as session:
            rows = session.execute(
                select(UserActorOutboundDelivery.status, func.count())
                .where(UserActorOutboundDelivery.source_id == source_id)
                .group_by(UserActorOutboundDelivery.status)
            ).all()
        counts = {status: count for status, count in rows}
        total = sum(counts.values())
        delivered = counts.get("delivered", 0)
        outstanding = sum(
            counts.get(status, 0) for status in ("pending", "sending", "failed")
        )
        return delivered, outstanding, total
