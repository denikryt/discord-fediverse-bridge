#!/usr/bin/env python3
"""Generate reproducible test-group, duration, and branch-coverage baselines.

The tool deliberately records execution facts only. It does not infer product
requirements from coverage data or alter pytest's pass/fail semantics.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TEST_GROUPS: Mapping[str, tuple[str, ...]] = {
    "behavior": ("tests/behavior",),
    "command_operation": ("tests/commands", "tests/operations"),
    "project": (
        "tests",
        "--ignore=tests/behavior",
        "--ignore=tests/commands",
        "--ignore=tests/operations",
    ),
    "vendor_discordops": ("vendor/discordops/tests",),
}

CRITICAL_MODULES: tuple[str, ...] = (
    "src/bridge_policy.py",
    "src/federation_policy.py",
    "src/local_community_permissions.py",
    "src/user_bans.py",
    "src/operations/common_preconditions.py",
    "src/operations/ban_user.py",
    "src/operations/unban_user.py",
)


@dataclass(frozen=True)
class CommandResult:
    """Capture the observable result of one external command execution."""

    exit_code: int
    stdout: str
    elapsed_seconds: float


Runner = Callable[[tuple[str, ...], Path], CommandResult]


def subprocess_runner(command: tuple[str, ...], cwd: Path) -> CommandResult:
    """Execute one command while retaining combined output and wall-clock time."""

    started = time.monotonic()
    completed = subprocess.run(
        command,
        cwd=cwd,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return CommandResult(
        exit_code=completed.returncode,
        stdout=completed.stdout,
        elapsed_seconds=round(time.monotonic() - started, 3),
    )


def parse_collected_nodeids(output: str) -> tuple[str, ...]:
    """Return exact pytest node IDs from quiet collection output."""

    # A node ID always contains ``::``; pytest summaries and warnings do not.
    return tuple(line.strip() for line in output.splitlines() if "::" in line)


def extract_critical_coverage(payload: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Extract measured totals for every configured policy-critical module."""

    files = payload.get("files", {})
    result: dict[str, dict[str, Any]] = {}
    for module in CRITICAL_MODULES:
        file_data = files.get(module)
        if not file_data:
            result[module] = {"measured": False}
            continue
        summary = file_data.get("summary", {})
        result[module] = {
            "covered_lines": summary.get("covered_lines", 0),
            "num_statements": summary.get("num_statements", 0),
            "covered_branches": summary.get("covered_branches", 0),
            "num_branches": summary.get("num_branches", 0),
            "measured": True,
        }
    return result


def write_summary(path: Path, summary: Mapping[str, Any]) -> None:
    """Persist deterministic JSON while creating its parent directory."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _collection_command(
    python_executable: str,
    selectors: Sequence[str],
) -> tuple[str, ...]:
    """Build the collection command for one immutable group definition."""

    return (
        python_executable,
        "-m",
        "pytest",
        "--collect-only",
        "-q",
        *selectors,
    )


def _test_command(
    python_executable: str,
    selectors: Sequence[str],
    coverage_path: Path,
) -> tuple[str, ...]:
    """Build a branch-enabled pytest command for one test group."""

    return (
        python_executable,
        "-m",
        "pytest",
        "-q",
        "--durations=0",
        "--cov=src",
        "--cov-branch",
        f"--cov-report=json:{coverage_path}",
        "--cov-report=term-missing:skip-covered",
        *selectors,
    )


def run_baseline(
    *,
    output_dir: Path,
    project_root: Path,
    python_executable: str,
    runner: Runner = subprocess_runner,
    groups: Sequence[str] | None = None,
) -> int:
    """Collect and execute configured groups, preserving partial diagnostics."""

    selected_groups = tuple(groups or TEST_GROUPS.keys())
    unknown = [group for group in selected_groups if group not in TEST_GROUPS]
    if unknown:
        raise ValueError(f"unknown test group: {', '.join(unknown)}")

    summary: dict[str, Any] = {
        "groups": {},
        "pilot_domain": "ban_management",
        "policy_critical_modules": list(CRITICAL_MODULES),
    }
    summary_path = output_dir / "summary.json"

    for group in selected_groups:
        selectors = TEST_GROUPS[group]
        group_dir = output_dir / group
        group_dir.mkdir(parents=True, exist_ok=True)
        group_summary: dict[str, Any] = {
            "selectors": list(selectors),
            "status": "collecting",
        }
        summary["groups"][group] = group_summary
        write_summary(summary_path, summary)

        collection = runner(
            _collection_command(python_executable, selectors),
            project_root,
        )
        (group_dir / "collect.log").write_text(collection.stdout, encoding="utf-8")
        nodeids = parse_collected_nodeids(collection.stdout)
        (group_dir / "nodeids.txt").write_text(
            "".join(f"{nodeid}\n" for nodeid in nodeids),
            encoding="utf-8",
        )
        group_summary.update(
            {
                "collected": len(nodeids),
                "collection_elapsed_seconds": collection.elapsed_seconds,
                "collection_exit_code": collection.exit_code,
                "nodeids": str((group_dir / "nodeids.txt").relative_to(output_dir)),
                "collect_log": str((group_dir / "collect.log").relative_to(output_dir)),
            }
        )
        if collection.exit_code != 0:
            group_summary["status"] = "collection_failed"
            write_summary(summary_path, summary)
            return collection.exit_code

        coverage_path = group_dir / "coverage.json"
        test_run = runner(
            _test_command(python_executable, selectors, coverage_path),
            project_root,
        )
        (group_dir / "pytest.log").write_text(test_run.stdout, encoding="utf-8")
        group_summary.update(
            {
                "elapsed_seconds": test_run.elapsed_seconds,
                "exit_code": test_run.exit_code,
                "pytest_log": str((group_dir / "pytest.log").relative_to(output_dir)),
                "coverage_json": str(coverage_path.relative_to(output_dir)),
                "status": "passed" if test_run.exit_code == 0 else "failed",
            }
        )

        # Coverage may be absent after an early pytest failure; preserve that
        # distinction instead of manufacturing zero-percent measurements.
        if coverage_path.exists():
            coverage_payload = json.loads(coverage_path.read_text(encoding="utf-8"))
            group_summary["critical_coverage"] = extract_critical_coverage(
                coverage_payload
            )
        else:
            group_summary["critical_coverage"] = {
                module: {"measured": False} for module in CRITICAL_MODULES
            }
        write_summary(summary_path, summary)
        if test_run.exit_code != 0:
            return test_run.exit_code

    return 0


def build_parser() -> argparse.ArgumentParser:
    """Create the command-line parser for local baseline generation."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(".artifacts/test-assurance/baseline"),
        help="Directory for generated logs, node IDs, coverage, and summary JSON.",
    )
    parser.add_argument(
        "--group",
        action="append",
        choices=tuple(TEST_GROUPS),
        help="Run only the selected group; repeat to select several groups.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run baseline generation from the repository root."""

    args = build_parser().parse_args(argv)
    project_root = Path(__file__).resolve().parents[1]
    output_dir = args.output_dir
    if not output_dir.is_absolute():
        output_dir = project_root / output_dir
    return run_baseline(
        output_dir=output_dir,
        project_root=project_root,
        python_executable=sys.executable,
        groups=args.group,
    )


if __name__ == "__main__":
    raise SystemExit(main())
