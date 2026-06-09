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
    from tools.assurance_reporting import PassiveCaseCollector, build_case_report
except ModuleNotFoundError:
    from assurance_reporting import PassiveCaseCollector, build_case_report


def build_report(results: Sequence[Any], required_rules: Sequence[Any]) -> dict[str, Any]:
    """Build community-management coverage through shared report mechanics."""

    return build_case_report(
        domain="community_management",
        results=results,
        required_rules=required_rules,
        serialize_case=lambda case: {
            "dimensions": {
                "action": case.action,
                "caller_role": case.caller_role,
                "community_state": case.community_state,
                "guild_context": case.guild_context,
                "requested_status": case.requested_status,
            }
        },
    )


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
