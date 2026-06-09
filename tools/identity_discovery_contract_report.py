#!/usr/bin/env python3
"""Run identity/discovery contract cases and emit their JSON report."""

from __future__ import annotations

from pathlib import Path

try:
    from tools.assurance_reporting import add_project_import_paths, run_case_report
except ModuleNotFoundError:
    from assurance_reporting import add_project_import_paths, run_case_report


def main() -> int:
    """Run identity/discovery cases and write the generated report."""

    root = Path(__file__).resolve().parents[1]
    add_project_import_paths(root)

    from support.identity_discovery_contracts import (
        IdentityDiscoveryCase,
        REQUIRED_IDENTITY_DISCOVERY_RULES,
    )

    return run_case_report(
        root=root,
        domain="identity_discovery",
        test_file="tests/test_identity_discovery_contract_cases.py",
        case_type=IdentityDiscoveryCase,
        required_rules=REQUIRED_IDENTITY_DISCOVERY_RULES,
        serialize_case=lambda case: {"action": case.action},
        output=root / ".artifacts/test-assurance/identity-discovery/report.json",
    )


if __name__ == "__main__":
    raise SystemExit(main())
