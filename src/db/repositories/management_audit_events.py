"""Persistence helpers for backend management audit events."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...management_audit import (
    RESULT_FORBIDDEN,
    VALID_ACTIONS,
    VALID_REASON_CODES,
    VALID_RESULTS,
    VALID_TARGET_TYPES,
    canonical_json,
)
from ...models import ManagementAuditEvent
from .base import BaseRepository


class ManagementAuditEventRepository(BaseRepository):
    """Insert and query v1 management audit rows.

    Public `create_event` is used for standalone forbidden audit writes. The
    `add_event` helper accepts an existing SQLAlchemy session so successful
    domain mutations can insert their audit rows in the same transaction.
    """

    def create_event(
        self,
        *,
        action: str,
        result: str,
        actor_discord_user_id: str,
        target_type: str,
        local_community_id: int | None = None,
        target_id: str | None = None,
        reason_code: str | None = None,
        before: dict[str, object] | None = None,
        after: dict[str, object] | None = None,
    ) -> ManagementAuditEvent:
        """Create one audit event in its own repository transaction."""
        with self.session() as session:
            return self.add_event(
                session,
                action=action,
                result=result,
                actor_discord_user_id=actor_discord_user_id,
                target_type=target_type,
                local_community_id=local_community_id,
                target_id=target_id,
                reason_code=reason_code,
                before=before,
                after=after,
            )

    def add_event(
        self,
        session: Session,
        *,
        action: str,
        result: str,
        actor_discord_user_id: str,
        target_type: str,
        local_community_id: int | None = None,
        target_id: str | None = None,
        reason_code: str | None = None,
        before: dict[str, object] | None = None,
        after: dict[str, object] | None = None,
    ) -> ManagementAuditEvent:
        """Add one validated audit event to an existing transaction."""
        self._validate_event(
            action=action,
            result=result,
            reason_code=reason_code,
            target_type=target_type,
            before=before,
            after=after,
        )
        event = ManagementAuditEvent(
            action=action,
            result=result,
            reason_code=reason_code,
            actor_discord_user_id=str(actor_discord_user_id),
            local_community_id=local_community_id,
            target_type=target_type,
            target_id=target_id,
            before_json=canonical_json(before),
            after_json=canonical_json(after),
        )
        # Flush here so insert failures abort the caller's transaction before a
        # successful command result can be returned.
        session.add(event)
        session.flush()
        return event

    def list_newest_first(self) -> list[ManagementAuditEvent]:
        """Return audit rows newest first for behavior-test assertions."""
        with self.session() as session:
            return list(
                session.scalars(
                    select(ManagementAuditEvent).order_by(
                        ManagementAuditEvent.created_at.desc(),
                        ManagementAuditEvent.id.desc(),
                    )
                )
            )

    def list_oldest_first(self) -> list[ManagementAuditEvent]:
        """Return audit rows oldest first for behavior-test assertions."""
        with self.session() as session:
            return list(
                session.scalars(
                    select(ManagementAuditEvent).order_by(
                        ManagementAuditEvent.created_at,
                        ManagementAuditEvent.id,
                    )
                )
            )

    def _validate_event(
        self,
        *,
        action: str,
        result: str,
        reason_code: str | None,
        target_type: str,
        before: dict[str, object] | None,
        after: dict[str, object] | None,
    ) -> None:
        """Reject event vocabulary outside the fixed v1 audit contract."""
        if action not in VALID_ACTIONS:
            raise ValueError(f"Unsupported management audit action: {action}")
        if result not in VALID_RESULTS:
            raise ValueError(f"Unsupported management audit result: {result}")
        if target_type not in VALID_TARGET_TYPES:
            raise ValueError(f"Unsupported management audit target type: {target_type}")
        if reason_code is not None and reason_code not in VALID_REASON_CODES:
            raise ValueError(f"Unsupported management audit reason code: {reason_code}")
        if result == RESULT_FORBIDDEN and (before is not None or after is not None):
            raise ValueError("Forbidden audit events must not store before/after JSON")
        if result == RESULT_FORBIDDEN and reason_code is None:
            raise ValueError("Forbidden audit events require a reason code")
        if result != RESULT_FORBIDDEN and reason_code is not None:
            raise ValueError("Successful audit events must not store a reason code")
