"""Collect and assert complete observable effects for ban contract cases."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import select

from src.models import CommunityActorBan
from support.ban_contracts import BanExpected


class OperationResult(Protocol):
    """Describe the public result fields shared by ban and unban operations."""

    applied: bool
    reason: str


@dataclass(frozen=True, slots=True)
class BanRowEffect:
    """Represent persisted moderation state relevant to the pilot contract."""

    status: str
    actor_handle: str
    discord_user_id: str | None


@dataclass(frozen=True, slots=True)
class BanObservedEffects:
    """Snapshot operation output, persisted rows, and management audit events."""

    applied: bool
    reason: str
    rows: tuple[BanRowEffect, ...]
    audit_events: tuple[tuple[str, str], ...]
    target_discord_user_id: str | None


def collect_ban_effects(
    *,
    database: object,
    result: OperationResult,
    target_discord_user_id: str | None,
    audit_offset: int = 0,
) -> BanObservedEffects:
    """Collect public post-action state without inspecting private operation internals."""

    with database.session() as session:
        rows = tuple(
            BanRowEffect(
                status=row.status,
                actor_handle=row.actor_handle,
                discord_user_id=row.target_discord_user_id,
            )
            for row in session.scalars(
                select(CommunityActorBan).order_by(CommunityActorBan.id)
            )
        )
    audit_rows = database.management_audit_events.list_oldest_first()[audit_offset:]
    audits = tuple((event.action, event.result) for event in audit_rows)
    return BanObservedEffects(
        applied=result.applied,
        reason=result.reason,
        rows=rows,
        audit_events=audits,
        target_discord_user_id=target_discord_user_id,
    )


def assert_ban_effects(observed: BanObservedEffects, expected: BanExpected) -> None:
    """Compare each contract field separately so failures remain diagnostic."""

    active_rows = tuple(row for row in observed.rows if row.status == "active")
    inactive_rows = tuple(row for row in observed.rows if row.status == "inactive")
    assert observed.applied is expected.applied, "operation applied outcome"
    assert observed.reason == expected.reason, "operation reason"
    assert len(active_rows) == expected.active_rows, "active ban row count"
    assert len(inactive_rows) == expected.inactive_rows, "inactive ban row count"
    assert observed.target_discord_user_id == expected.target_discord_user_id, (
        "resolved target Discord identity"
    )
    assert observed.audit_events == expected.audit_events, "management audit effects"
