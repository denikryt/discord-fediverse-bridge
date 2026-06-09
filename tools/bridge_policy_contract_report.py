#!/usr/bin/env python3
"""Run typed bridge-policy contracts and emit a passive deterministic report."""

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


class BridgePolicyContractCollector(PassiveCaseCollector):
    """Collect only bridge-policy typed cases."""

    def __init__(self) -> None:
        """Identify cases by their bridge-policy dimensions."""

        super().__init__(accepts=lambda case: hasattr(case, "policy_type"))


def _case_dimensions(case: Any) -> dict[str, str]:
    """Extract machine-readable dimensions declared by one case."""

    return {
        "action": case.action,
        "caller_role": case.caller_role,
        "policy_type": case.policy_type,
        "existing_dynamic_state": case.existing_dynamic_state,
        "guild_context": case.guild_context,
    }


def build_contract_report(
    results: Sequence[CollectedCaseResult],
    required_rules: Sequence[Any],
) -> dict[str, Any]:
    """Build deterministic factual coverage for bridge-policy contracts."""

    collected_ids = {result.case.id for result in results}
    rule_rows = []
    missing_rules = []
    for rule in sorted(required_rules, key=lambda value: value.id):
        represented = bool(collected_ids.intersection(rule.represented_by))
        rule_rows.append(
            {
                "id": rule.id,
                "description": rule.description,
                "represented": represented,
                "represented_by": list(rule.represented_by),
            }
        )
        if not represented:
            missing_rules.append(rule.id)

    values: dict[str, set[str]] = {}
    combinations = {
        "action_policy_type": set(),
        "caller_role_action": set(),
        "existing_state_action": set(),
    }
    case_rows = []
    for result in sorted(results, key=lambda value: value.case.id):
        dimensions = _case_dimensions(result.case)
        for name, value in dimensions.items():
            values.setdefault(name, set()).add(value)
        combinations["action_policy_type"].add(
            (dimensions["action"], dimensions["policy_type"])
        )
        combinations["caller_role_action"].add(
            (dimensions["caller_role"], dimensions["action"])
        )
        combinations["existing_state_action"].add(
            (dimensions["existing_dynamic_state"], dimensions["action"])
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
        "domain": "bridge_policy",
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
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")


def main(argv: Sequence[str] | None = None) -> int:
    """Run the typed second-domain pilot and preserve pytest's exit status."""

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(".artifacts/test-assurance/bridge-policy-contract/report.json"),
    )
    args = parser.parse_args(argv)
    project_root = Path(__file__).resolve().parents[1]
    tests_root = project_root / "tests"
    for path in (project_root, tests_root):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))

    from support.bridge_policy_contracts import REQUIRED_BRIDGE_POLICY_RULES

    collector = BridgePolicyContractCollector()
    exit_code = pytest.main(
        ["-q", "tests/operations/test_bridge_policy_contract_cases.py"],
        plugins=[collector],
    )
    output = args.output if args.output.is_absolute() else project_root / args.output
    write_report(
        output,
        build_contract_report(collector.results(), REQUIRED_BRIDGE_POLICY_RULES),
    )
    return int(exit_code)


if __name__ == "__main__":
    sys.exit(main())
