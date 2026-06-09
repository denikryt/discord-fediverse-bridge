#!/usr/bin/env python3
"""Run inbound ActivityPub contract owners and emit their JSON report."""

from __future__ import annotations

from pathlib import Path

try:
    from tools.assurance_reporting import add_project_import_paths, run_owner_report
except ModuleNotFoundError:
    from assurance_reporting import add_project_import_paths, run_owner_report


def main() -> int:
    """Run all declared inbound owner tests and write the generated report."""

    root = Path(__file__).resolve().parents[1]
    add_project_import_paths(root)

    from support.inbound_contract_manifest import INBOUND_CONTRACT_ENTRIES

    return run_owner_report(
        root=root,
        domain="inbound_activitypub",
        entries=INBOUND_CONTRACT_ENTRIES,
        output=root / ".artifacts/test-assurance/inbound-activitypub/report.json",
    )


if __name__ == "__main__":
    raise SystemExit(main())
