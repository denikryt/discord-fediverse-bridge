"""Unit tests for the community-management passive report builder."""

from __future__ import annotations

from types import SimpleNamespace

from tools.community_management_contract_report import build_report
from tools.contract_report_support import CollectedCaseResult


def test_report_marks_missing_and_represented_rules() -> None:
    """Report representation is derived only from collected case IDs."""

    case = SimpleNamespace(
        id="case.present",
        action="edit",
        caller_role="owner",
        community_state="active",
        guild_context="same",
        requested_status="active",
    )
    rules = (
        SimpleNamespace(
            id="present", description="present", represented_by=("case.present",)
        ),
        SimpleNamespace(
            id="missing", description="missing", represented_by=("case.missing",)
        ),
    )
    report = build_report((CollectedCaseResult("node", case, "passed"),), rules)

    assert report["summary"]["required_rules"] == 2
    assert report["summary"]["represented_rules"] == 1
    assert report["summary"]["missing_rules"] == 1
    assert report["missing_rule_ids"] == ["missing"]
    assert report["summary"]["statuses"]["passed"] == 1
