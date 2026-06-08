"""Persistence for reversible dynamic bridge policy entries."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import BridgePolicyEntry
from .base import BaseRepository


@dataclass(frozen=True, slots=True)
class PolicyActivationResult:
    """Describe whether a policy row was created or reactivated."""

    entry: BridgePolicyEntry
    kind: str
    before: dict[str, object] | None
    after: dict[str, object]


class BridgePolicyEntryRepository(BaseRepository):
    """Read and mutate dynamic policy rows without applying policy precedence."""

    def get_by_type_and_subject(self, *, policy_type: str, normalized_subject: str) -> BridgePolicyEntry | None:
        """Return one dynamic row regardless of active status."""
        with self.session() as session:
            return session.scalar(
                select(BridgePolicyEntry).where(
                    BridgePolicyEntry.policy_type == policy_type,
                    BridgePolicyEntry.normalized_subject == normalized_subject,
                )
            )

    def list_active_by_type(self, *, policy_type: str) -> list[BridgePolicyEntry]:
        """Return active rows for one policy type in stable subject order."""
        with self.session() as session:
            return list(session.scalars(select(BridgePolicyEntry).where(
                BridgePolicyEntry.policy_type == policy_type,
                BridgePolicyEntry.status == "active",
            ).order_by(BridgePolicyEntry.normalized_subject)))

    def list_all_active(self) -> list[BridgePolicyEntry]:
        """Return all active rows for one request-scoped policy snapshot."""
        with self.session() as session:
            return list(session.scalars(select(BridgePolicyEntry).where(
                BridgePolicyEntry.status == "active"
            ).order_by(BridgePolicyEntry.policy_type, BridgePolicyEntry.normalized_subject)))

    def create_active(self, *, policy_type: str, normalized_subject: str, actor_discord_user_id: str, reason: str | None) -> BridgePolicyEntry:
        """Create one active row in its own repository transaction for tests/tools."""
        with self.session() as session:
            result = self.create_active_in_session(
                session,
                policy_type=policy_type,
                normalized_subject=normalized_subject,
                actor_discord_user_id=actor_discord_user_id,
                reason=reason,
            )
            return result.entry

    def create_active_in_session(self, session: Session, *, policy_type: str, normalized_subject: str, actor_discord_user_id: str, reason: str | None) -> PolicyActivationResult:
        """Create an active row inside a caller-owned transaction."""
        entry = BridgePolicyEntry(
            policy_type=policy_type,
            normalized_subject=normalized_subject,
            status="active",
            reason=reason,
            created_by_discord_user_id=actor_discord_user_id,
            updated_by_discord_user_id=actor_discord_user_id,
        )
        session.add(entry)
        session.flush()
        after = {"status": "active", "reason": reason}
        return PolicyActivationResult(entry, "created", None, after)

    def reactivate_in_session(self, session: Session, *, entry_id: int, actor_discord_user_id: str, reason: str | None) -> PolicyActivationResult:
        """Reactivate one inactive row and update activation metadata."""
        entry = session.get(BridgePolicyEntry, entry_id)
        if entry is None:
            raise LookupError("Bridge policy entry disappeared before reactivation.")
        before = {"status": entry.status, "reason": entry.reason}
        entry.status = "active"
        entry.reason = reason
        entry.updated_by_discord_user_id = actor_discord_user_id
        session.flush()
        return PolicyActivationResult(entry, "reactivated", before, {"status": "active", "reason": reason})

    def deactivate_in_session(self, session: Session, *, entry_id: int, actor_discord_user_id: str) -> BridgePolicyEntry:
        """Deactivate one active row without overwriting its activation reason."""
        entry = session.get(BridgePolicyEntry, entry_id)
        if entry is None:
            raise LookupError("Bridge policy entry disappeared before removal.")
        entry.status = "inactive"
        entry.updated_by_discord_user_id = actor_discord_user_id
        session.flush()
        return entry
