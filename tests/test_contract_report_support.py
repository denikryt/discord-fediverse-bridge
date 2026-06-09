"""Verify shared passive contract-report collection mechanics."""

from __future__ import annotations

from types import SimpleNamespace

from tools.contract_report_support import PassiveCaseCollector, status_totals


def test_passive_collector_filters_cases_and_records_terminal_statuses() -> None:
    """Collector must preserve domain filtering and normalize pytest outcomes."""

    accepted = SimpleNamespace(id="accepted", domain="wanted")
    rejected = SimpleNamespace(id="rejected", domain="other")
    items = [
        SimpleNamespace(
            nodeid="node::accepted",
            callspec=SimpleNamespace(params={"case": accepted}),
        ),
        SimpleNamespace(
            nodeid="node::rejected",
            callspec=SimpleNamespace(params={"case": rejected}),
        ),
    ]
    collector = PassiveCaseCollector(accepts=lambda case: case.domain == "wanted")

    collector.pytest_collection_modifyitems(items)
    collector.pytest_runtest_logreport(
        SimpleNamespace(
            nodeid="node::accepted", failed=False, skipped=False, when="call", passed=True
        )
    )

    results = collector.results()
    assert [(result.case.id, result.status) for result in results] == [
        ("accepted", "passed")
    ]
    assert status_totals(results) == {
        "failed": 0,
        "passed": 1,
        "skipped": 0,
        "xfailed": 0,
    }


def test_setup_failure_defaults_to_failed_result() -> None:
    """A collected case without a successful call report remains failed."""

    case = SimpleNamespace(id="setup-failure")
    collector = PassiveCaseCollector()
    collector.pytest_collection_modifyitems(
        [
            SimpleNamespace(
                nodeid="node::setup",
                callspec=SimpleNamespace(params={"case": case}),
            )
        ]
    )

    assert collector.results()[0].status == "failed"
