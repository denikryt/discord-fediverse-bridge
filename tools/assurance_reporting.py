"""Minimal shared primitives for passive domain assurance reports."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Sequence
import json
from pathlib import Path
import sys
from typing import Any

import pytest

try:
    from tools.contract_report_support import (
        CollectedCaseResult,
        PassiveCaseCollector,
        status_totals,
    )
except ModuleNotFoundError:
    from contract_report_support import (
        CollectedCaseResult,
        PassiveCaseCollector,
        status_totals,
    )


class OwnerPrefixCollector:
    """Collect terminal pytest status for explicitly declared node prefixes."""

    def __init__(self, prefixes: set[str]) -> None:
        self.prefixes = prefixes
        self.status: dict[str, str] = {}

    def _matches(self, nodeid: str) -> bool:
        return any(nodeid.startswith(prefix) for prefix in self.prefixes)

    def pytest_collection_modifyitems(self, items: Sequence[Any]) -> None:
        """Register matching collected nodes before execution."""

        for item in items:
            if self._matches(item.nodeid):
                self.status[item.nodeid] = "collected"

    def pytest_runtest_logreport(self, report: Any) -> None:
        """Store the terminal status without changing pytest behavior."""

        if report.nodeid not in self.status:
            return
        if report.failed:
            self.status[report.nodeid] = "failed"
        elif report.skipped:
            self.status[report.nodeid] = (
                "xfailed" if hasattr(report, "wasxfail") else "skipped"
            )
        elif report.when == "call" and report.passed:
            self.status[report.nodeid] = "passed"


def _validate_unique(values: Sequence[str], label: str) -> None:
    """Reject ambiguous report identifiers before rendering artifacts."""

    duplicates = sorted(value for value, count in Counter(values).items() if count > 1)
    if duplicates:
        raise ValueError(f"duplicate {label}: {', '.join(duplicates)}")


def build_case_report(
    *,
    domain: str,
    results: Sequence[CollectedCaseResult],
    required_rules: Sequence[Any],
    serialize_case: Callable[[Any], dict[str, Any]],
) -> dict[str, Any]:
    """Build one typed-case report while preserving domain-specific dimensions."""

    _validate_unique([rule.id for rule in required_rules], "rule IDs")
    _validate_unique([result.case.id for result in results], "case IDs")
    case_ids = {result.case.id for result in results}
    missing: list[str] = []
    rules: list[dict[str, Any]] = []
    for rule in sorted(required_rules, key=lambda item: item.id):
        represented = bool(case_ids.intersection(rule.represented_by))
        if not represented:
            missing.append(rule.id)
        rules.append(
            {
                "id": rule.id,
                "description": getattr(rule, "description", ""),
                "represented": represented,
                "represented_by": list(rule.represented_by),
            }
        )
    return {
        "domain": domain,
        "summary": {
            "required_rules": len(rules),
            "represented_rules": len(rules) - len(missing),
            "missing_rules": len(missing),
            "statuses": status_totals(results),
        },
        "missing_rule_ids": missing,
        "required_rules": rules,
        "cases": [
            {
                "id": result.case.id,
                "nodeid": result.nodeid,
                "status": result.status,
                **serialize_case(result.case),
            }
            for result in sorted(results, key=lambda item: item.case.id)
        ],
    }


def build_owner_report(
    *,
    domain: str,
    entries: Sequence[Any],
    status: dict[str, str],
) -> dict[str, Any]:
    """Build a report for named/generated tests owned by node prefixes."""

    _validate_unique([entry.rule_id for entry in entries], "rule IDs")
    rows: list[dict[str, Any]] = []
    missing: list[str] = []
    for entry in entries:
        nodes = sorted(
            nodeid
            for nodeid in status
            if any(nodeid.startswith(prefix) for prefix in entry.node_prefixes)
        )
        represented = bool(nodes)
        if not represented:
            missing.append(entry.rule_id)
        rows.append(
            {
                "rule_id": entry.rule_id,
                "family": entry.family,
                "classification": entry.classification,
                "represented": represented,
                "nodes": [
                    {"nodeid": nodeid, "status": status[nodeid]} for nodeid in nodes
                ],
            }
        )
    totals = Counter(status.values())
    return {
        "domain": domain,
        "summary": {
            "required_rules": len(entries),
            "represented_rules": len(entries) - len(missing),
            "missing_rules": len(missing),
            "statuses": dict(sorted(totals.items())),
        },
        "missing_rule_ids": missing,
        "rules": rows,
    }


def add_project_import_paths(root: Path) -> None:
    """Expose the project and test-support packages to standalone report scripts."""

    for path in (root, root / "tests"):
        path_text = str(path)
        if path_text not in sys.path:
            sys.path.insert(0, path_text)


def write_json_report(output: Path, report: dict[str, Any]) -> None:
    """Write one deterministic generated report outside tracked source files."""

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def run_owner_report(
    *,
    root: Path,
    domain: str,
    entries: Sequence[Any],
    output: Path,
) -> int:
    """Run declared pytest owners and emit their passive domain report."""

    prefixes = {prefix for entry in entries for prefix in entry.node_prefixes}
    test_files = sorted({prefix.split("::", 1)[0] for prefix in prefixes})
    collector = OwnerPrefixCollector(prefixes)

    exit_code = pytest.main(["-q", *test_files], plugins=[collector])
    report = build_owner_report(
        domain=domain,
        entries=entries,
        status=collector.status,
    )
    write_json_report(output, report)
    return int(exit_code)


def run_case_report(
    *,
    root: Path,
    domain: str,
    test_file: str,
    case_type: type[Any],
    required_rules: Sequence[Any],
    serialize_case: Callable[[Any], dict[str, Any]],
    output: Path,
) -> int:
    """Run typed contract cases and emit their passive domain report."""

    collector = PassiveCaseCollector(
        accepts=lambda case: isinstance(case, case_type),
    )
    exit_code = pytest.main(["-q", test_file], plugins=[collector])
    report = build_case_report(
        domain=domain,
        results=collector.results(),
        required_rules=required_rules,
        serialize_case=serialize_case,
    )
    write_json_report(output, report)
    return int(exit_code)
