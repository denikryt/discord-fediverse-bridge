"""Verify deterministic passive reporting for typed ban contract cases."""

from __future__ import annotations

from dataclasses import dataclass

from tools.ban_contract_report import CollectedCaseResult, build_contract_report


@dataclass(frozen=True)
class FakeCase:
    """Provide the same declared dimensions as the real ban pilot."""

    id: str
    action: str = "ban"
    caller_role: str = "owner"
    scope: str = "community"
    community_state: str = "enabled"
    target_kind: str = "remote"
    existing_ban_state: str = "absent"


@dataclass(frozen=True)
class FakeRule:
    """Declare one synthetic required rule for report-gap tests."""

    id: str
    description: str
    represented_by: tuple[str, ...]


def test_report_records_statuses_dimensions_and_missing_rules() -> None:
    """Report facts without changing pytest outcomes or hiding absent rules."""

    results = (
        CollectedCaseResult("node::one", FakeCase("case.one"), "passed"),
        CollectedCaseResult(
            "node::two",
            FakeCase(
                "case.two",
                action="unban",
                caller_role="super_admin",
                scope="global",
                community_state="missing",
                existing_ban_state="active",
            ),
            "xfailed",
        ),
    )
    rules = (
        FakeRule("represented", "present rule", ("case.one",)),
        FakeRule("missing", "absent rule", ("case.missing",)),
    )

    report = build_contract_report(results, rules)

    assert report["summary"] == {
        "required_rules": 2,
        "represented_rules": 1,
        "missing_rules": 1,
        "statuses": {"failed": 0, "passed": 1, "skipped": 0, "xfailed": 1},
    }
    assert report["missing_rule_ids"] == ["missing"]
    assert report["represented_values"]["action"] == ["ban", "unban"]
    assert ["owner", "community"] in report["represented_combinations"][
        "caller_role_scope"
    ]
    assert ["super_admin", "global"] in report["represented_combinations"][
        "caller_role_scope"
    ]
    assert [case["id"] for case in report["cases"]] == ["case.one", "case.two"]
