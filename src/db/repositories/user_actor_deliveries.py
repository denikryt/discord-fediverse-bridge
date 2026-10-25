"""Compatibility facade for durable User Actor outbound delivery persistence."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Callable

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from ...models import UserActorFollower, UserActorOutboundSource
from .base import BaseRepository
from .user_actor_delivery_admin import UserActorDeliveryAdminRepository
from .user_actor_delivery_claims import UserActorDeliveryClaimRepository
from .user_actor_delivery_outcomes import UserActorDeliveryOutcomeRepository
from .user_actor_delivery_sources import UserActorDeliverySourceRepository
from .user_actor_delivery_types import (
    ERROR_LIMIT,
    ClaimedUserActorDelivery,
    DeliveryDiagnostic,
    DeliveryRetryResult,
    bound_delivery_error,
)


class UserActorDeliveryRepository(BaseRepository):
    """Expose the stable User Actor delivery persistence facade."""

    ERROR_LIMIT = ERROR_LIMIT

    def __init__(
        self, session_provider: Callable, immediate_session_provider: Callable
    ) -> None:
        """Compose focused collaborators behind the existing database facade."""
        super().__init__(session_provider)
        self._sources = UserActorDeliverySourceRepository()
        self._claims = UserActorDeliveryClaimRepository(immediate_session_provider)
        self._outcomes = UserActorDeliveryOutcomeRepository(session_provider)
        self._admin = UserActorDeliveryAdminRepository(
            session_provider, immediate_session_provider
        )

    @staticmethod
    def validate_endpoint(endpoint: str) -> str:
        """Accept only normalized absolute HTTPS inbox endpoints."""
        return UserActorDeliverySourceRepository.validate_endpoint(endpoint)

    @staticmethod
    def insert_source_graph(
        session: Session,
        *,
        user_id: int,
        logical_version: int,
        activity_id: str,
        profile_snapshot_json: str,
        followers: list[UserActorFollower],
        media_refs: list[tuple[str, int]],
    ) -> UserActorOutboundSource:
        """Insert one source and its fixed audience inside the mutation transaction."""
        return UserActorDeliverySourceRepository.insert_source_graph(
            session,
            user_id=user_id,
            logical_version=logical_version,
            activity_id=activity_id,
            profile_snapshot_json=profile_snapshot_json,
            followers=followers,
            media_refs=media_refs,
        )

    @staticmethod
    def insert_ready_content_source(
        session: Session,
        *,
        user_id: int,
        source_kind: str,
        logical_version: int,
        activity_id: str,
        published_object_id: int,
        object_id: str,
        activity_json: dict,
        snapshot: dict,
        followers: list[UserActorFollower],
        direct_authorizations: list[dict] | None = None,
    ) -> UserActorOutboundSource:
        """Insert one already-rendered content source and independently verified audience."""
        return UserActorDeliverySourceRepository.insert_ready_content_source(
            session,
            user_id=user_id,
            source_kind=source_kind,
            logical_version=logical_version,
            activity_id=activity_id,
            published_object_id=published_object_id,
            object_id=object_id,
            activity_json=activity_json,
            snapshot=snapshot,
            followers=followers,
            direct_authorizations=direct_authorizations,
        )

    def get_source(self, source_id: int) -> UserActorOutboundSource | None:
        """Load one durable source without consulting mutable profile state."""
        with self.session() as session:
            return session.get(UserActorOutboundSource, source_id)

    def due_source_ids(self, limit: int) -> list[int]:
        """Return bounded render work in deterministic version order."""
        now = datetime.now(timezone.utc)
        with self.session() as session:
            return list(
                session.scalars(
                    select(UserActorOutboundSource.id)
                    .where(
                        UserActorOutboundSource.state.in_(
                            ("render_pending", "render_failed")
                        ),
                        or_(
                            UserActorOutboundSource.next_attempt_at.is_(None),
                            UserActorOutboundSource.next_attempt_at <= now,
                        ),
                    )
                    .order_by(
                        UserActorOutboundSource.user_id,
                        UserActorOutboundSource.logical_version,
                        UserActorOutboundSource.id,
                    )
                    .limit(limit)
                )
            )

    def record_rendered(self, source_id: int, activity_json: dict) -> bool:
        """Write canonical activity JSON exactly once through compare-and-set."""
        encoded = json.dumps(activity_json, separators=(",", ":"), sort_keys=True)
        now = datetime.now(timezone.utc)
        with self.session() as session:
            result = session.execute(
                update(UserActorOutboundSource)
                .where(
                    UserActorOutboundSource.id == source_id,
                    UserActorOutboundSource.activity_json.is_(None),
                    UserActorOutboundSource.state.in_(
                        ("render_pending", "render_failed")
                    ),
                )
                .values(
                    activity_json=encoded,
                    state="ready",
                    last_error=None,
                    next_attempt_at=None,
                    last_render_attempted_at=now,
                    render_attempt_count=UserActorOutboundSource.render_attempt_count
                    + 1,
                )
            )
            return result.rowcount == 1

    def record_render_failure(self, source_id: int, error: str) -> None:
        """Keep render intent durable with bounded exponential backoff."""
        now = datetime.now(timezone.utc)
        with self.session() as session:
            source = session.get(UserActorOutboundSource, source_id)
            if source is None or source.state == "ready":
                return
            source.render_attempt_count += 1
            source.last_render_attempted_at = now
            source.state = "render_failed"
            source.last_error = bound_delivery_error(error)
            source.next_attempt_at = now + timedelta(
                seconds=min(300, 2 ** min(source.render_attempt_count, 8))
            )

    def claim_due(
        self, *, limit: int, per_user_limit: int, stale_after_seconds: int
    ) -> list[ClaimedUserActorDelivery]:
        """Claim bounded oldest endpoint versions under one writer reservation."""
        return self._claims.claim_due(
            limit=limit,
            per_user_limit=per_user_limit,
            stale_after_seconds=stale_after_seconds,
        )

    def revalidate_claim(self, delivery_id: int, claim_token: str) -> tuple[str, ...]:
        """Return live actor grants before the side-effectful transport boundary."""
        return self._claims.revalidate_claim(delivery_id, claim_token)

    def mark_ineligible(self, delivery_id: int, claim_token: str, reason: str) -> None:
        """Terminally suppress one currently claimed endpoint without transport."""
        self._outcomes.mark_ineligible(delivery_id, claim_token, reason)

    def release_unsubmitted_claim(
        self, delivery_id: int, claim_token: str, error: str
    ) -> None:
        """Return a pre-transport claim to retryable failure without an attempt."""
        self._outcomes.release_unsubmitted_claim(delivery_id, claim_token, error)

    def mark_submitted(self, delivery_id: int, claim_token: str) -> bool:
        """Count one actual gateway submission for the currently owned claim."""
        return self._outcomes.mark_submitted(delivery_id, claim_token)

    def record_outcome(
        self, delivery_id: int, claim_token: str, *, ok: bool, error: str | None = None
    ) -> None:
        """Complete only the currently owned claim and preserve retryability."""
        self._outcomes.record_outcome(delivery_id, claim_token, ok=ok, error=error)

    def release_terminal_source_media(self, source_id: int) -> int:
        """Release source media only after rendering and all targets are terminal."""
        return self._outcomes.release_terminal_source_media(source_id)

    def list_diagnostics(
        self, *, username: str | None, status: str | None, limit: int
    ) -> list[DeliveryDiagnostic]:
        """List bounded non-secret endpoint and audience diagnostics for operators."""
        return self._admin.list_diagnostics(
            username=username, status=status, limit=limit
        )

    def retry_failed_by_delivery_id(
        self, delivery_id: int, username: str | None = None
    ) -> DeliveryRetryResult:
        """Reset one failed delivery when its optional owner matches."""
        return self._admin.retry_failed_by_delivery_id(delivery_id, username)

    def retry_failed_by_source_id(
        self, source_id: int, username: str | None = None
    ) -> DeliveryRetryResult:
        """Reset all failed endpoints for one exact source identity."""
        return self._admin.retry_failed_by_source_id(source_id, username)

    def retry_failed_batch(self, *, username: str, limit: int) -> DeliveryRetryResult:
        """Reset one deterministic bounded batch of failed deliveries."""
        return self._admin.retry_failed_batch(username=username, limit=limit)

    def source_summary(self, source_id: int) -> tuple[int, int, int]:
        """Return delivered, pending/sending/failed outstanding, and total counts."""
        return self._outcomes.source_summary(source_id)

    @classmethod
    def _bounded_error(cls, error: str) -> str:
        """Normalize and redact diagnostics before bounded persistence."""
        return bound_delivery_error(error)
