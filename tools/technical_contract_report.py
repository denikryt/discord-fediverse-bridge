#!/usr/bin/env python3
"""Run Python technical owners and combine them with native gateway evidence."""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import pytest


class PrefixCollector:
    """Collect terminal pytest status for selected node prefixes."""

    def __init__(self, prefixes: set[str]) -> None:
        self.prefixes = prefixes
        self.status: dict[str, str] = {}

    def _matches(self, nodeid: str) -> bool:
        return any(nodeid.startswith(prefix) for prefix in self.prefixes)

    def pytest_collection_modifyitems(self, items: list[Any]) -> None:
        for item in items:
            if self._matches(item.nodeid):
                self.status[item.nodeid] = "collected"

    def pytest_runtest_logreport(self, report: Any) -> None:
        if report.nodeid not in self.status:
            return
        if report.failed:
            self.status[report.nodeid] = "failed"
        elif report.skipped:
            self.status[report.nodeid] = "xfailed" if hasattr(report, "wasxfail") else "skipped"
        elif report.when == "call" and report.passed:
            self.status[report.nodeid] = "passed"


def build_report(
    entries: tuple[Any, ...],
    pytest_status: dict[str, str],
    gateway_status: dict[str, Any],
) -> dict[str, Any]:
    """Build one deterministic report across native Python and gateway owners."""

    rows: list[dict[str, Any]] = []
    missing: list[str] = []
    all_statuses: list[str] = []
    gateway_scripts = gateway_status.get("scripts", {})
    for entry in entries:
        nodes: list[dict[str, str]] = []
        if entry.owner_kind == "pytest":
            for nodeid, status in sorted(pytest_status.items()):
                if any(nodeid.startswith(owner) for owner in entry.owners):
                    nodes.append({"owner": nodeid, "status": status})
        else:
            for owner in entry.owners:
                if owner == "check":
                    status = gateway_status.get("check", "not_run")
                    nodes.append({"owner": owner, "status": status})
                elif owner.endswith("-"):
                    for name, status in sorted(gateway_scripts.items()):
                        if name.startswith(owner):
                            nodes.append({"owner": name, "status": status})
                elif owner in gateway_scripts:
                    nodes.append({"owner": owner, "status": gateway_scripts[owner]})
        represented = bool(nodes)
        if not represented:
            missing.append(entry.rule_id)
        all_statuses.extend(node["status"] for node in nodes)
        rows.append(
            {
                "rule_id": entry.rule_id,
                "family": entry.family,
                "owner_kind": entry.owner_kind,
                "represented": represented,
                "owners": nodes,
            }
        )
    totals = Counter(all_statuses)
    return {
        "domain": "technical_contracts",
        "summary": {
            "required_rules": len(entries),
            "represented_rules": len(entries) - len(missing),
            "missing_rules": len(missing),
            "statuses": dict(sorted(totals.items())),
        },
        "missing_rule_ids": missing,
        "rules": rows,
    }


def main() -> int:
    """Execute Python owners and emit the unified technical report."""

    root = Path(__file__).resolve().parents[1]
    for candidate in (root, root / "tests"):
        if str(candidate) not in sys.path:
            sys.path.insert(0, str(candidate))
    from support.technical_contract_manifest import TECHNICAL_CONTRACT_ENTRIES

    prefixes = {
        owner
        for entry in TECHNICAL_CONTRACT_ENTRIES
        if entry.owner_kind == "pytest"
        for owner in entry.owners
    }
    files = sorted({prefix.split("::", 1)[0] for prefix in prefixes})
    collector = PrefixCollector(prefixes)
    code = pytest.main(["-q", *files], plugins=[collector])
    gateway_path = root / ".artifacts/test-assurance/technical-contracts/gateway-status.json"
    gateway = json.loads(gateway_path.read_text()) if gateway_path.exists() else {}
    report = build_report(TECHNICAL_CONTRACT_ENTRIES, collector.status, gateway)
    output = root / ".artifacts/test-assurance/technical-contracts/report.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return int(code or bool(report["missing_rule_ids"]))


if __name__ == "__main__":
    raise SystemExit(main())
