"""Constrained ban interaction model and independent expected outcomes."""

from __future__ import annotations

from dataclasses import dataclass

from support.ban_contracts import BanExpected
from support.pairwise import enumerate_valid_cases, select_pairwise_cases

BAN_FACTORS: dict[str, tuple[str, ...]] = {
    "action": ("ban", "unban"),
    "caller_role": ("owner", "super_admin", "unauthorized"),
    "scope": ("community", "global"),
    "community_state": ("enabled", "disabled", "missing"),
    "target_kind": ("remote", "local"),
    "existing_ban_state": ("absent", "active", "removed"),
}


def is_valid_ban_interaction(case: dict[str, str]) -> bool:
    """Reject impossible scope/community/persistence combinations."""

    if case["scope"] == "global" and case["community_state"] != "missing":
        return False
    if (
        case["scope"] == "community"
        and case["community_state"] == "missing"
        and case["existing_ban_state"] != "absent"
    ):
        return False
    return True


MUST_TEST_BAN_INTERACTIONS: tuple[dict[str, str], ...] = (
    {
        "action": "ban",
        "caller_role": "owner",
        "scope": "community",
        "community_state": "disabled",
        "target_kind": "remote",
        "existing_ban_state": "absent",
    },
    {
        "action": "unban",
        "caller_role": "super_admin",
        "scope": "global",
        "community_state": "missing",
        "target_kind": "remote",
        "existing_ban_state": "active",
    },
    {
        "action": "ban",
        "caller_role": "unauthorized",
        "scope": "community",
        "community_state": "enabled",
        "target_kind": "local",
        "existing_ban_state": "absent",
    },
)


@dataclass(frozen=True, slots=True)
class BanInteractionCase:
    """Represent one selected constrained interaction and explicit expectation."""

    id: str
    action: str
    caller_role: str
    scope: str
    community_state: str
    target_kind: str
    existing_ban_state: str
    expected: BanExpected


def expected_ban_interaction(case: dict[str, str]) -> BanExpected:
    """Apply a small independent precondition/transition table."""

    action = case["action"]
    role = case["caller_role"]
    scope = case["scope"]
    community_state = case["community_state"]
    existing = case["existing_ban_state"]

    active_rows = 1 if existing == "active" else 0
    inactive_rows = 1 if existing == "removed" else 0

    if scope == "global" and role != "super_admin":
        audits = (
            (("ban.create_forbidden", "forbidden"),)
            if action == "ban"
            else ()
        )
        return BanExpected(
            False,
            "global_scope_requires_super_admin",
            active_rows,
            inactive_rows=inactive_rows,
            audit_events=audits,
        )
    if scope == "community" and community_state == "missing":
        return BanExpected(False, "unknown_or_inaccessible_community", 0)
    if scope == "community" and role == "unauthorized":
        audit_action = "ban.create_forbidden" if action == "ban" else "ban.remove_forbidden"
        return BanExpected(
            False,
            "cannot_manage_community",
            active_rows,
            inactive_rows=inactive_rows,
            audit_events=((audit_action, "forbidden"),),
        )
    if scope == "community" and community_state == "disabled":
        audit_action = "ban.create_forbidden" if action == "ban" else "ban.remove_forbidden"
        return BanExpected(
            False,
            "community_disabled",
            active_rows,
            inactive_rows=inactive_rows,
            audit_events=((audit_action, "forbidden"),),
        )

    if action == "ban":
        if existing == "active":
            return BanExpected(False, "duplicate_active_ban", 1)
        if existing == "removed":
            return BanExpected(
                True,
                "reactivated",
                1,
                audit_events=(("ban.reactivated", "success"),),
            )
        return BanExpected(
            True,
            "created",
            1,
            target_discord_user_id="777" if case["target_kind"] == "local" else None,
            audit_events=(("ban.created", "success"),),
        )

    if existing == "active":
        return BanExpected(
            True,
            "unbanned",
            0,
            inactive_rows=1,
            audit_events=(("ban.removed", "success"),),
        )
    return BanExpected(
        False,
        "no_active_ban",
        0,
        inactive_rows=inactive_rows,
    )


def _case_id(case: dict[str, str]) -> str:
    return ".".join(case[name] for name in BAN_FACTORS)


VALID_BAN_INTERACTIONS = enumerate_valid_cases(
    BAN_FACTORS,
    is_valid=is_valid_ban_interaction,
)
SELECTED_BAN_INTERACTIONS = select_pairwise_cases(
    VALID_BAN_INTERACTIONS,
    must_include=MUST_TEST_BAN_INTERACTIONS,
)
BAN_INTERACTION_CASES: tuple[BanInteractionCase, ...] = tuple(
    BanInteractionCase(
        id=_case_id(case),
        expected=expected_ban_interaction(case),
        **case,
    )
    for case in SELECTED_BAN_INTERACTIONS
)
