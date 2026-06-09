#!/usr/bin/env python3
"""Collect and classify every executable Python and gateway test."""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import pytest


class NodeCollector:
    """Capture pytest node IDs during collection without executing tests."""

    def __init__(self) -> None:
        self.nodeids: list[str] = []

    def pytest_collection_modifyitems(self, items: list[Any]) -> None:
        """Store every collected node in deterministic pytest order."""

        self.nodeids.extend(item.nodeid for item in items)


def build_report(
    python_nodeids: list[str], gateway_scripts: list[str]
) -> dict[str, Any]:
    """Build and validate the complete migration inventory."""

    root = Path(__file__).resolve().parents[1]
    for candidate in (root, root / "tests"):
        if str(candidate) not in sys.path:
            sys.path.insert(0, str(candidate))
    from assurance.migration_inventory import classify_test

    all_ids = python_nodeids + gateway_scripts
    duplicates = sorted(value for value, count in Counter(all_ids).items() if count > 1)
    if duplicates:
        raise ValueError(f"duplicate executable test IDs: {duplicates}")
    records = [classify_test(nodeid, "python") for nodeid in sorted(python_nodeids)]
    records.extend(
        classify_test(script, "gateway") for script in sorted(gateway_scripts)
    )
    unknown = [
        record.test_id
        for record in records
        if not record.domain or not record.classification
    ]
    class_counts = Counter(record.classification for record in records)
    domain_counts = Counter(record.domain for record in records)
    status_counts = Counter(record.migration_status for record in records)
    return {
        "summary": {
            "total_tests": len(records),
            "python_tests": len(python_nodeids),
            "gateway_tests": len(gateway_scripts),
            "unknown_unreviewed": len(unknown),
            "duplicate_or_obsolete": class_counts.get("E", 0),
            "by_classification": dict(sorted(class_counts.items())),
            "by_domain": dict(sorted(domain_counts.items())),
            "by_migration_status": dict(sorted(status_counts.items())),
        },
        "unknown_test_ids": unknown,
        "records": [record.to_json() for record in records],
        "interpretation": {
            "zero_unknown_means": "Every executable test has an architectural classification.",
            "zero_unknown_does_not_mean": (
                "All possible product rules are known or semantically complete."
            ),
            "duplicate_review": (
                "No test was removed because no exact redundant contract was "
                "proven in this stage."
            ),
        },
    }


def main() -> int:
    """Collect both runtimes and emit the canonical completeness artifact."""

    root = Path(__file__).resolve().parents[1]
    for candidate in (root, root / "tests"):
        if str(candidate) not in sys.path:
            sys.path.insert(0, str(candidate))
    collector = NodeCollector()
    code = pytest.main(
        ["--collect-only", "-p", "no:terminal", "tests", "vendor/discordops/tests"],
        plugins=[collector],
    )
    gateway = [
        path.relative_to(root / "fedify-gateway").as_posix()
        for path in sorted((root / "fedify-gateway/tests").glob("verify-*.ts"))
    ]
    report = build_report(collector.nodeids, gateway)
    output = root / ".artifacts/test-assurance/migration-completeness/report.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return int(code or bool(report["summary"]["unknown_unreviewed"]))


if __name__ == "__main__":
    raise SystemExit(main())
