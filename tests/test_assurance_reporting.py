"""Tests for the minimal shared assurance reporting framework."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from tools.aggregate_assurance_report import build_aggregate
from tools.assurance_reporting import build_case_report, build_owner_report
from tools.contract_report_support import CollectedCaseResult


@dataclass(frozen=True, slots=True)
class ExampleCase:
    """Minimal typed case fixture for shared report tests."""

    id: str
    action: str


@dataclass(frozen=True, slots=True)
class ExampleRule:
    """Minimal typed rule fixture for shared report tests."""

    id: str
    description: str
    represented_by: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ExampleOwnerEntry:
    """Minimal typed owner fixture for shared report tests."""

    rule_id: str
    family: str
    classification: str
    node_prefixes: tuple[str, ...]


def test_shared_case_report_preserves_domain_dimensions_and_gaps() -> None:
    """Shared wiring must preserve explicit domain serialization and missing rules."""

    case = ExampleCase(id="case-a", action="create")
    result = CollectedCaseResult("file.py::test_a", case, "passed")
    rules = (
        ExampleRule(
            id="present",
            description="present",
            represented_by=("case-a",),
        ),
        ExampleRule(
            id="missing",
            description="missing",
            represented_by=("case-b",),
        ),
    )
    report = build_case_report(
        domain="example",
        results=(result,),
        required_rules=rules,
        serialize_case=lambda value: {"dimensions": {"action": value.action}},
    )
    assert report["missing_rule_ids"] == ["missing"]
    assert report["cases"][0]["dimensions"] == {"action": "create"}


def test_shared_owner_report_preserves_classification_and_status() -> None:
    """Named scenario reports retain classification and exact owner status."""

    entries = (
        ExampleOwnerEntry(
            rule_id="rule",
            family="routing",
            classification="named_scenario",
            node_prefixes=("f.py::test_",),
        ),
    )
    report = build_owner_report(
        domain="fanout", entries=entries, status={"f.py::test_one": "passed"}
    )
    assert report["summary"]["represented_rules"] == 1
    assert report["rules"][0]["classification"] == "named_scenario"


def test_shared_reporting_rejects_duplicate_rule_ids() -> None:
    """Ambiguous registry data fails before a misleading artifact is emitted."""

    entries = (
        ExampleOwnerEntry(
            rule_id="duplicate",
            family="a",
            classification="named_scenario",
            node_prefixes=("a",),
        ),
        ExampleOwnerEntry(
            rule_id="duplicate",
            family="b",
            classification="named_scenario",
            node_prefixes=("b",),
        ),
    )
    with pytest.raises(ValueError, match="duplicate rule IDs"):
        build_owner_report(domain="x", entries=entries, status={})


def test_aggregate_report_sums_only_declared_domain_facts() -> None:
    """Aggregate totals are deterministic sums of provider reports."""

    aggregate = build_aggregate(
        [
            {
                "domain": "b",
                "summary": {
                    "required_rules": 2,
                    "represented_rules": 1,
                    "missing_rules": 1,
                },
                "missing_rule_ids": ["b2"],
            },
            {
                "domain": "a",
                "summary": {
                    "required_rules": 3,
                    "represented_rules": 3,
                    "missing_rules": 0,
                },
                "missing_rule_ids": [],
            },
        ]
    )
    assert aggregate["summary"] == {
        "domains": 2,
        "required_rules": 5,
        "represented_rules": 4,
        "missing_rules": 1,
    }
    assert [row["domain"] for row in aggregate["domains"]] == ["a", "b"]
