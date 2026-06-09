#!/usr/bin/env python3
"""Run outbound fanout contract owners and emit their JSON report."""

from __future__ import annotations

from pathlib import Path
from typing import Any

try:
    from tools.assurance_reporting import (
        add_project_import_paths,
        build_owner_report,
        run_owner_report,
    )
except ModuleNotFoundError:
    from assurance_reporting import (
        add_project_import_paths,
        build_owner_report,
        run_owner_report,
    )


def build_report(
    entries: tuple[Any, ...],
    status: dict[str, str],
) -> dict[str, Any]:
    """Build the outbound fanout report from declared owners and pytest status."""

    return build_owner_report(
        domain="outbound_fanout",
        entries=entries,
        status=status,
    )


def main() -> int:
    """Run all declared fanout owner tests and write the generated report."""

    root = Path(__file__).resolve().parents[1]
    add_project_import_paths(root)

    from support.fanout_contract_manifest import FANOUT_CONTRACT_ENTRIES

    return run_owner_report(
        root=root,
        domain="outbound_fanout",
        entries=FANOUT_CONTRACT_ENTRIES,
        output=root / ".artifacts/test-assurance/outbound-fanout/report.json",
    )


if __name__ == "__main__":
    raise SystemExit(main())
