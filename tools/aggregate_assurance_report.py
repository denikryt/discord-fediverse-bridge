#!/usr/bin/env python3
"""Aggregate generated domain assurance artifacts without rerunning tests."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def build_aggregate(reports: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize domain artifacts while retaining their factual boundaries."""

    domains = sorted(reports, key=lambda report: report["domain"])
    return {
        "summary": {
            "domains": len(domains),
            "required_rules": sum(report["summary"].get("required_rules", 0) for report in domains),
            "represented_rules": sum(report["summary"].get("represented_rules", 0) for report in domains),
            "missing_rules": sum(report["summary"].get("missing_rules", 0) for report in domains),
        },
        "domains": [
            {
                "domain": report["domain"],
                "summary": report["summary"],
                "missing_rule_ids": report.get("missing_rule_ids", []),
            }
            for report in domains
        ],
    }


def main() -> int:
    """Read report artifacts and emit one deterministic aggregate JSON file."""

    parser = argparse.ArgumentParser()
    parser.add_argument("reports", nargs="+", type=Path)
    parser.add_argument("--output", type=Path, default=Path(".artifacts/test-assurance/aggregate/report.json"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    reports = [json.loads((path if path.is_absolute() else root / path).read_text()) for path in args.reports]
    aggregate = build_aggregate(reports)
    output = args.output if args.output.is_absolute() else root / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(aggregate, indent=2, sort_keys=True) + "\n")
    return int(bool(aggregate["summary"]["missing_rules"]))


if __name__ == "__main__":
    raise SystemExit(main())
