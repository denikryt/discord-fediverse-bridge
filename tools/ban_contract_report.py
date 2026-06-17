#!/usr/bin/env python3
"""Run the typed ban pilot and emit a passive deterministic contract report."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest


try:
    from tools.contract_report_support import (
        CollectedCaseResult,
        PassiveCaseCollector,
        status_totals,
    )
except ModuleNotFoundError:  # Direct script execution adds tools/ to sys.path.
    from contract_report_support import (
        CollectedCaseResult,
        PassiveCaseCollector,
        status_totals,
    )


class BanContractCollector(PassiveCaseCollector):
    """Collect typed ban cases through the shared passive mechanism."""

    def __init__(self) -> None:
        """Accept only the ban pilot case shape."""

        super().__init__(accepts=lambda case: hasattr(case, "caller_role"))


def _case_dimensions(case: Any) -> dict[str, str]:
    """Extract the declared machine-readable dimensions from one case."""

    return {
        "action": case.action,
        "caller_role": case.caller_role,
        "scope": case.scope,
        "community_state": case.community_state,
        "target_kind": case.target_kind,
        "existing_ban_state": case.existing_ban_state,
    }


def build_contract_report(
    results: Sequence[CollectedCaseResult],
    required_rules: Sequence[Any],
) -> dict[str, Any]:
    """Build deterministic factual coverage data from cases and pytest results."""

    collected_ids = {result.case.id for result in results}
    rule_rows = []
    missing_rules = []
    for rule in sorted(required_rules, key=lambda value: value.id):
        represented = bool(collected_ids.intersection(rule.represented_by))
        row = {
            "id": rule.id,
            "description": rule.description,
            "represented": represented,
            "represented_by": list(rule.represented_by),
        }
        rule_rows.append(row)
        if not represented:
            missing_rules.append(rule.id)

    case_rows = []
    values: dict[str, set[str]] = {}
    combinations = {
        "caller_role_scope": set(),
        "action_existing_ban_state": set(),
        "community_state_target_kind": set(),
    }
    for result in sorted(results, key=lambda value: value.case.id):
        dimensions = _case_dimensions(result.case)
        for name, value in dimensions.items():
            values.setdefault(name, set()).add(value)
        combinations["caller_role_scope"].add(
            (dimensions["caller_role"], dimensions["scope"])
        )
        combinations["action_existing_ban_state"].add(
            (dimensions["action"], dimensions["existing_ban_state"])
        )
        combinations["community_state_target_kind"].add(
            (dimensions["community_state"], dimensions["target_kind"])
        )
        case_rows.append(
            {
                "id": result.case.id,
                "nodeid": result.nodeid,
                "status": result.status,
                "dimensions": dimensions,
            }
        )

    return {
        "domain": "ban_management",
        "summary": {
            "required_rules": len(rule_rows),
            "represented_rules": len(rule_rows) - len(missing_rules),
            "missing_rules": len(missing_rules),
            "statuses": status_totals(results),
        },
        "represented_values": {
            name: sorted(items) for name, items in sorted(values.items())
        },
        "represented_combinations": {
            name: [list(pair) for pair in sorted(items)]
            for name, items in sorted(combinations.items())
        },
        "missing_rule_ids": missing_rules,
        "required_rules": rule_rows,
        "cases": case_rows,
    }


def write_report(path: Path, report: Mapping[str, Any]) -> None:
    """Write canonical sorted JSON to the generated artifact path."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def build_parser() -> argparse.ArgumentParser:
    """Create the CLI parser for passive contract reporting."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(".artifacts/test-assurance/ban-contract/report.json"),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the typed pilot, emit its report, and return pytest's exit code."""

    args = build_parser().parse_args(argv)
    project_root = Path(__file__).resolve().parents[1]
    tests_root = project_root / "tests"
    # The existing suite imports test support as ``support``. Match pytest's
    # collection environment when the report CLI is launched directly.
    for path in (project_root, tests_root):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
    from support.ban_contracts import REQUIRED_BAN_RULES

    collector = BanContractCollector()
    exit_code = pytest.main(
        ["-q", "tests/operations/test_ban_contract_cases.py"],
        plugins=[collector],
    )
    report = build_contract_report(collector.results(), REQUIRED_BAN_RULES)
    output = args.output if args.output.is_absolute() else project_root / args.output
    write_report(output, report)
    return int(exit_code)


if __name__ == "__main__":
    raise SystemExit(main())
