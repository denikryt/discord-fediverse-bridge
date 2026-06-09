#!/usr/bin/env python3
"""Run community-management contracts and emit a deterministic JSON report."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

try:
    from tools.contract_report_support import PassiveCaseCollector, status_totals
except ModuleNotFoundError:
    from contract_report_support import PassiveCaseCollector, status_totals


def build_report(results: Sequence[Any], required_rules: Sequence[Any]) -> dict[str, Any]:
    """Build factual rule representation and case status data."""

    ids = {result.case.id for result in results}
    rule_rows = []
    missing = []
    for rule in sorted(required_rules, key=lambda value: value.id):
        represented = bool(ids.intersection(rule.represented_by))
        rule_rows.append({
            "id": rule.id,
            "description": rule.description,
            "represented": represented,
            "represented_by": list(rule.represented_by),
        })
        if not represented:
            missing.append(rule.id)
    return {
        "domain": "community_management",
        "summary": {
            "required_rules": len(rule_rows),
            "represented_rules": len(rule_rows) - len(missing),
            "missing_rules": len(missing),
            "statuses": status_totals(results),
        },
        "missing_rule_ids": missing,
        "required_rules": rule_rows,
        "cases": [
            {
                "id": result.case.id,
                "nodeid": result.nodeid,
                "status": result.status,
                "dimensions": {
                    "action": result.case.action,
                    "caller_role": result.case.caller_role,
                    "community_state": result.case.community_state,
                    "guild_context": result.case.guild_context,
                    "requested_status": result.case.requested_status,
                },
            }
            for result in sorted(results, key=lambda value: value.case.id)
        ],
    }


def main(argv: Sequence[str] | None = None) -> int:
    """Run the contract test and write its passive report."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(".artifacts/test-assurance/community-management/report.json"))
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    for value in (root, root / "tests"):
        if str(value) not in sys.path:
            sys.path.insert(0, str(value))
    from support.community_management_contracts import CommunityManagementCase, REQUIRED_COMMUNITY_MANAGEMENT_RULES

    collector = PassiveCaseCollector(accepts=lambda case: isinstance(case, CommunityManagementCase))
    exit_code = pytest.main(["-q", "tests/operations/test_community_management_contract_cases.py"], plugins=[collector])
    output = args.output if args.output.is_absolute() else root / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(build_report(collector.results(), REQUIRED_COMMUNITY_MANAGEMENT_RULES), indent=2, sort_keys=True) + "\n")
    return int(exit_code)


if __name__ == "__main__":
    raise SystemExit(main())
