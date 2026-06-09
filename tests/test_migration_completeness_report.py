"""Tests for complete executable-test migration classification."""

from __future__ import annotations

import pytest

from assurance.migration_inventory import classify_test
from tools.migration_completeness_report import build_report


@pytest.mark.parametrize(
    ("test_id", "runtime", "classification", "domain"),
    (
        (
            "tests/operations/test_ban_contract_cases.py::test_case[x]",
            "python",
            "A",
            "ban",
        ),
        (
            "tests/behavior/test_dashboard_scenarios.py::test_page",
            "python",
            "B",
            "technical_contracts",
        ),
        (
            "tests/property/test_bridge_policy_properties.py::test_rule",
            "python",
            "C",
            "bridge_policy",
        ),
        ("tests/test_database.py::test_schema", "python", "D", "core_or_support"),
        (
            "vendor/discordops/tests/test_operation.py::test_run",
            "python",
            "D",
            "discordops_framework",
        ),
        (
            "tests/verify-local-community-relay.ts",
            "gateway",
            "D",
            "technical_contracts",
        ),
    ),
)
def test_classifier_covers_each_architectural_test_form(
    test_id: str, runtime: str, classification: str, domain: str
) -> None:
    """All supported test forms receive stable intentional classifications."""

    record = classify_test(test_id, runtime)  # type: ignore[arg-type]
    assert record.classification == classification
    assert record.domain == domain
    assert record.contract_id.startswith(f"{domain}:")


def test_completeness_report_rejects_duplicate_executable_ids() -> None:
    """One executable test may not appear twice in the canonical inventory."""

    with pytest.raises(ValueError, match="duplicate executable test IDs"):
        build_report(["tests/test_a.py::test_a"], ["tests/test_a.py::test_a"])


def test_completeness_report_has_zero_unknown_for_classifiable_inputs() -> None:
    """Known Python and gateway shapes produce a fully reviewed inventory."""

    report = build_report(
        ["tests/behavior/test_inbound_scenarios.py::test_accept"],
        ["tests/verify-python-contract.ts"],
    )
    assert report["summary"]["unknown_unreviewed"] == 0
    assert report["summary"]["total_tests"] == 2
    assert report["summary"]["duplicate_or_obsolete"] == 0
