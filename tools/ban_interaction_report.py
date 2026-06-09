#!/usr/bin/env python3
"""Emit deterministic constrained ban interaction coverage facts."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence


def build_interaction_report(
    *,
    factors: dict[str, tuple[str, ...]],
    valid_cases: Sequence[dict[str, str]],
    selected_cases: Sequence[dict[str, str]],
    must_include: Sequence[dict[str, str]],
    coverage: dict[str, object],
) -> dict[str, Any]:
    """Build factual pairwise coverage data without running production logic."""

    return {
        "domain": "ban_authorization_interactions",
        "factors": {name: list(values) for name, values in factors.items()},
        "constraints": [
            "global scope requires community_state=missing",
            "scoped missing community requires existing_ban_state=absent",
        ],
        "summary": {
            "valid_candidates": len(valid_cases),
            "selected_cases": len(selected_cases),
            "required_pairs": coverage["required_pairs"],
            "covered_pairs": coverage["covered_pairs"],
            "missing_pairs": len(coverage["missing_pairs"]),
        },
        "must_include": list(must_include),
        "missing_pair_details": [list(pair) for pair in coverage["missing_pairs"]],
        "cases": list(selected_cases),
    }


def main(argv: Sequence[str] | None = None) -> int:
    """Write the generated interaction report and return success."""

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(".artifacts/test-assurance/ban-interactions/report.json"),
    )
    args = parser.parse_args(argv)
    project_root = Path(__file__).resolve().parents[1]
    tests_root = project_root / "tests"
    for path in (project_root, tests_root):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))

    from support.ban_interactions import (
        BAN_FACTORS,
        MUST_TEST_BAN_INTERACTIONS,
        SELECTED_BAN_INTERACTIONS,
        VALID_BAN_INTERACTIONS,
    )
    from support.pairwise import coverage_summary

    report = build_interaction_report(
        factors=BAN_FACTORS,
        valid_cases=VALID_BAN_INTERACTIONS,
        selected_cases=SELECTED_BAN_INTERACTIONS,
        must_include=MUST_TEST_BAN_INTERACTIONS,
        coverage=coverage_summary(VALID_BAN_INTERACTIONS, SELECTED_BAN_INTERACTIONS),
    )
    output = args.output if args.output.is_absolute() else project_root / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
