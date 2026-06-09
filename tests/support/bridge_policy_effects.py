"""Collect and assert observable effects for bridge-policy contract cases."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select

from src.models import BridgePolicyEntry
from support.bridge_policy_contracts import BridgePolicyExpected


@dataclass(frozen=True, slots=True)
class BridgePolicyObservedEffects:
    """Snapshot public result, decision, persistence, and audit effects."""

    applied: bool | None
    reason: str
    decision_allowed: bool | None
    decision_reason: str | None
    active_entries: tuple[tuple[str, str], ...]
    inactive_entries: tuple[tuple[str, str], ...]
    audit_events: tuple[tuple[str, str], ...]


def collect_bridge_policy_effects(
    *,
    database: object,
    reason: str,
    applied: bool | None = None,
    decision_allowed: bool | None = None,
    decision_reason: str | None = None,
    audit_offset: int = 0,
) -> BridgePolicyObservedEffects:
    """Collect only public and persisted post-action state."""

    with database.session() as session:
        rows = tuple(
            (row.policy_type, row.normalized_subject, row.status)
            for row in session.scalars(
                select(BridgePolicyEntry).order_by(
                    BridgePolicyEntry.policy_type,
                    BridgePolicyEntry.normalized_subject,
                )
            )
        )
    active = tuple((kind, subject) for kind, subject, status in rows if status == "active")
    inactive = tuple((kind, subject) for kind, subject, status in rows if status == "inactive")
    audits = tuple(
        (event.action, event.result)
        for event in database.management_audit_events.list_oldest_first()[audit_offset:]
    )
    return BridgePolicyObservedEffects(
        applied=applied,
        reason=reason,
        decision_allowed=decision_allowed,
        decision_reason=decision_reason,
        active_entries=active,
        inactive_entries=inactive,
        audit_events=audits,
    )


def assert_bridge_policy_effects(
    observed: BridgePolicyObservedEffects,
    expected: BridgePolicyExpected,
) -> None:
    """Compare each domain contract field separately for useful failures."""

    assert observed.applied is expected.applied, "operation applied outcome"
    assert observed.reason == expected.reason, "operation/evaluation reason"
    assert observed.decision_allowed is expected.decision_allowed, "policy decision"
    assert observed.decision_reason == expected.decision_reason, "policy decision reason"
    assert observed.active_entries == expected.active_entries, "active policy rows"
    assert observed.inactive_entries == expected.inactive_entries, "inactive policy rows"
    assert observed.audit_events == expected.audit_events, "management audit effects"
