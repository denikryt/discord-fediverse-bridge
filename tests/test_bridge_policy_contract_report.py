"""Verify deterministic passive reporting for bridge-policy contract cases."""

from __future__ import annotations

from dataclasses import dataclass

from tools.bridge_policy_contract_report import build_contract_report
from tools.contract_report_support import CollectedCaseResult


@dataclass(frozen=True)
class FakeCase:
    """Provide the dimensions required by the bridge-policy report."""

    id: str
    action: str = "add"
    caller_role: str = "super_admin"
    policy_type: str = "federation-block"
    existing_dynamic_state: str = "absent"
    guild_context: str = "allowed"


@dataclass(frozen=True)
class FakeRule:
    """Declare one synthetic required rule."""

    id: str
    description: str
    represented_by: tuple[str, ...]


def test_report_records_dimensions_statuses_and_missing_rules() -> None:
    """Second-domain report remains factual, deterministic, and passive."""

    results = (
        CollectedCaseResult("node::one", FakeCase("case.one"), "passed"),
        CollectedCaseResult(
            "node::two",
            FakeCase(
                "case.two",
                action="remove",
                caller_role="unauthorized",
                existing_dynamic_state="active",
                guild_context="blocked",
            ),
            "failed",
        ),
    )
    rules = (
        FakeRule("represented", "present", ("case.one",)),
        FakeRule("missing", "absent", ("case.missing",)),
    )

    report = build_contract_report(results, rules)

    assert report["domain"] == "bridge_policy"
    assert report["summary"] == {
        "required_rules": 2,
        "represented_rules": 1,
        "missing_rules": 1,
        "statuses": {"failed": 1, "passed": 1, "skipped": 0, "xfailed": 0},
    }
    assert report["missing_rule_ids"] == ["missing"]
    assert report["represented_values"]["action"] == ["add", "remove"]
    assert ["add", "federation-block"] in report["represented_combinations"][
        "action_policy_type"
    ]
    assert [row["id"] for row in report["cases"]] == ["case.one", "case.two"]
