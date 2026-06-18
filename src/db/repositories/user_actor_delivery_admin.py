"""Repository-side diagnostics and retry administration for User Actor deliveries."""

from __future__ import annotations

from typing import Callable

from sqlalchemy import func, select, update

from ...models import (
    User,
    UserActorOutboundDelivery,
    UserActorOutboundDeliveryFollower,
    UserActorOutboundSource,
)
from .base import BaseRepository
from .user_actor_delivery_types import (
    DeliveryDiagnostic,
    DeliveryRetryResult,
    bound_delivery_error,
)


class UserActorDeliveryAdminRepository(BaseRepository):
    """Expose bounded operator diagnostics and serialized retry resets."""

    def __init__(self, session_provider: Callable, immediate_session_provider: Callable):
        """Bind ordinary reads and writer-reserved retry transaction scopes."""
        super().__init__(session_provider)
        self._immediate_session_provider = immediate_session_provider

    def list_diagnostics(
        self, *, username: str | None, status: str | None, limit: int
    ) -> list[DeliveryDiagnostic]:
        """List bounded non-secret endpoint and audience diagnostics for operators."""
        if limit < 1 or limit > 500:
            raise ValueError("limit must be between 1 and 500")
        with self.session() as session:
            stmt = (
                select(UserActorOutboundDelivery, UserActorOutboundSource, User)
                .join(
                    UserActorOutboundSource,
                    UserActorOutboundSource.id == UserActorOutboundDelivery.source_id,
                )
                .join(User, User.id == UserActorOutboundSource.user_id)
                .order_by(UserActorOutboundDelivery.id)
                .limit(limit)
            )
            if username is not None:
                stmt = stmt.where(User.activitypub_username == username)
            if status is not None:
                stmt = stmt.where(UserActorOutboundDelivery.status == status)
            rows = session.execute(stmt).all()
            diagnostics: list[DeliveryDiagnostic] = []
            for delivery, source, user in rows:
                # Audience rows are the immutable transport context operators need;
                # claim tokens and raw activity JSON intentionally stay hidden.
                actor_ids = tuple(
                    session.scalars(
                        select(UserActorOutboundDeliveryFollower.remote_actor_id)
                        .where(
                            UserActorOutboundDeliveryFollower.delivery_id == delivery.id
                        )
                        .order_by(UserActorOutboundDeliveryFollower.id)
                    )
                )
                diagnostics.append(
                    DeliveryDiagnostic(
                        delivery_id=delivery.id,
                        source_id=source.id,
                        username=user.activitypub_username,
                        source_kind=source.source_kind,
                        logical_version=source.logical_version,
                        activity_id=source.activity_id,
                        target_endpoint_url=delivery.target_endpoint_url,
                        represented_actor_ids=actor_ids,
                        status=delivery.status,
                        attempt_count=delivery.attempt_count,
                        last_attempted_at=delivery.last_attempted_at,
                        next_attempt_at=delivery.next_attempt_at,
                        last_error=bound_delivery_error(delivery.last_error)
                        if delivery.last_error
                        else None,
                    )
                )
            return diagnostics

    def retry_failed_by_delivery_id(
        self, delivery_id: int, username: str | None = None
    ) -> DeliveryRetryResult:
        """Reset one failed delivery when its optional owner matches."""
        return self._retry_failed(
            delivery_id=delivery_id, source_id=None, username=username
        )

    def retry_failed_by_source_id(
        self, source_id: int, username: str | None = None
    ) -> DeliveryRetryResult:
        """Reset all failed endpoints for one exact source identity."""
        return self._retry_failed(
            delivery_id=None, source_id=source_id, username=username
        )

    def _retry_failed(
        self, *, delivery_id: int | None, source_id: int | None, username: str | None
    ) -> DeliveryRetryResult:
        """Reset failed rows while distinguishing lookup and ownership failures."""
        with self._immediate_session_provider() as session:
            base = (
                select(UserActorOutboundDelivery.id, User.activitypub_username)
                .select_from(UserActorOutboundDelivery)
                .join(
                    UserActorOutboundSource,
                    UserActorOutboundSource.id == UserActorOutboundDelivery.source_id,
                )
                .join(User, User.id == UserActorOutboundSource.user_id)
            )
            if delivery_id is not None:
                base = base.where(UserActorOutboundDelivery.id == delivery_id)
            if source_id is not None:
                base = base.where(UserActorOutboundDelivery.source_id == source_id)
            rows = list(session.execute(base.order_by(UserActorOutboundDelivery.id)))
            if not rows:
                return DeliveryRetryResult(0, 0, 0, 0, "not_found")
            if username is not None and any(owner != username for _id, owner in rows):
                return DeliveryRetryResult(
                    len(rows), 0, len(rows), 0, "ownership_mismatch"
                )
            ids = [row_id for row_id, _owner in rows]
            result = session.execute(
                update(UserActorOutboundDelivery)
                .where(
                    UserActorOutboundDelivery.id.in_(ids),
                    UserActorOutboundDelivery.status == "failed",
                )
                .values(
                    status="pending",
                    claim_token=None,
                    claimed_at=None,
                    next_attempt_at=None,
                    last_error=None,
                )
            )
            reset = int(result.rowcount or 0)
            if reset == 0:
                return DeliveryRetryResult(len(ids), 0, len(ids), 0, "not_retryable")
            return DeliveryRetryResult(len(ids), reset, len(ids) - reset, 0, "success")

    def retry_failed_batch(self, *, username: str, limit: int) -> DeliveryRetryResult:
        """Reset one deterministic bounded batch of failed deliveries."""
        if limit < 1 or limit > 100:
            raise ValueError("limit must be between 1 and 100")
        with self._immediate_session_provider() as session:
            base = (
                select(UserActorOutboundDelivery.id)
                .join(UserActorOutboundSource)
                .join(User)
                .where(
                    User.activitypub_username == username,
                    UserActorOutboundDelivery.status == "failed",
                )
                .order_by(UserActorOutboundDelivery.id)
            )
            ids = list(session.scalars(base.limit(limit)))
            matched = int(
                session.scalar(select(func.count()).select_from(base.subquery())) or 0
            )
            if ids:
                session.execute(
                    update(UserActorOutboundDelivery)
                    .where(
                        UserActorOutboundDelivery.id.in_(ids),
                        UserActorOutboundDelivery.status == "failed",
                    )
                    .values(
                        status="pending",
                        claim_token=None,
                        claimed_at=None,
                        next_attempt_at=None,
                        last_error=None,
                    )
                )
            reset = len(ids)
            # Rows beyond this bounded batch remain failed; they are not skipped.
            return DeliveryRetryResult(
                matched=matched,
                reset=reset,
                skipped=0,
                remaining=max(0, matched - reset),
                outcome="success",
            )
