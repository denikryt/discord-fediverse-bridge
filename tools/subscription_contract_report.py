#!/usr/bin/env python3
"""Run subscription lifecycle contract cases and emit their JSON report."""

from __future__ import annotations

from pathlib import Path

try:
    from tools.assurance_reporting import add_project_import_paths, run_case_report
except ModuleNotFoundError:
    from assurance_reporting import add_project_import_paths, run_case_report


def _serialize_case(case: object) -> dict[str, object]:
    """Expose the subscription dimensions that matter in the generated report."""

    return {
        "action": case.action,
        "channel_state": case.channel_state,
        "follow_state": case.follow_state,
    }


def main() -> int:
    """Run subscription cases and write the generated report."""

    root = Path(__file__).resolve().parents[1]
    add_project_import_paths(root)

    from support.subscription_contracts import (
        SubscriptionCase,
        REQUIRED_SUBSCRIPTION_RULES,
    )

    return run_case_report(
        root=root,
        domain="subscription_lifecycle",
        test_file="tests/operations/test_subscription_contract_cases.py",
        case_type=SubscriptionCase,
        required_rules=REQUIRED_SUBSCRIPTION_RULES,
        serialize_case=_serialize_case,
        output=root / ".artifacts/test-assurance/subscription/report.json",
    )


if __name__ == "__main__":
    raise SystemExit(main())
