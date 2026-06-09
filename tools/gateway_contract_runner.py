#!/usr/bin/env python3
"""Run native gateway checks in resumable chunks and record deterministic status."""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any


def _run(command: list[str], cwd: Path) -> tuple[str, str]:
    """Run one native command and return terminal status plus combined output."""

    completed = subprocess.run(
        command,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    return ("passed" if completed.returncode == 0 else "failed", completed.stdout)


def merge_status(
    previous: dict[str, Any],
    *,
    discovered: tuple[str, ...],
    updates: dict[str, str],
    check_status: str | None,
) -> dict[str, Any]:
    """Merge one chunk while discarding entries for no-longer-discovered scripts."""

    old_scripts = previous.get("scripts", {})
    scripts = {name: old_scripts[name] for name in discovered if name in old_scripts}
    scripts.update(updates)
    result = {
        "check": (
            check_status
            if check_status is not None
            else previous.get("check", "not_run")
        ),
        "scripts": dict(sorted(scripts.items())),
    }
    return result


def main() -> int:
    """Run TypeScript check and a deterministic slice of gateway scripts."""

    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--skip-check", action="store_true")
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    gateway = root / "fedify-gateway"
    artifact = (
        root / ".artifacts/test-assurance/technical-contracts/gateway-status.json"
    )
    scripts = tuple(
        path.relative_to(gateway).as_posix()
        for path in sorted((gateway / "tests").glob("verify-*.ts"))
    )
    selected = scripts[
        args.start : None if args.limit is None else args.start + args.limit
    ]

    previous = json.loads(artifact.read_text()) if artifact.exists() else {}
    check_status = None
    exit_code = 0
    if not args.skip_check:
        check_status, output = _run(["npm", "run", "check"], gateway)
        print(output, end="")
        exit_code |= check_status != "passed"

    updates: dict[str, str] = {}
    for script in selected:
        status, output = _run(["./node_modules/.bin/tsx", script], gateway)
        print(f"Running {script}\n{output}", end="")
        updates[script] = status
        exit_code |= status != "passed"

    merged = merge_status(
        previous,
        discovered=scripts,
        updates=updates,
        check_status=check_status,
    )
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(json.dumps(merged, indent=2, sort_keys=True) + "\n")
    return int(bool(exit_code))


if __name__ == "__main__":
    raise SystemExit(main())
