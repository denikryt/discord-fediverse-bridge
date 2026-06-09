"""Verify the measurable test-baseline tooling contracts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.test_assurance_baseline import (
    CRITICAL_MODULES,
    TEST_GROUPS,
    CommandResult,
    extract_critical_coverage,
    parse_collected_nodeids,
    run_baseline,
    write_summary,
)


def test_group_selectors_partition_the_existing_suite() -> None:
    """Keep the documented broad test groups stable and non-overlapping."""

    assert TEST_GROUPS["behavior"] == ("tests/behavior",)
    assert TEST_GROUPS["command_operation"] == (
        "tests/commands",
        "tests/operations",
    )
    assert TEST_GROUPS["project"] == (
        "tests",
        "--ignore=tests/behavior",
        "--ignore=tests/commands",
        "--ignore=tests/operations",
    )
    assert TEST_GROUPS["vendor_discordops"] == ("vendor/discordops/tests",)


def test_parse_collected_nodeids_keeps_parameterized_cases_only() -> None:
    """Ignore pytest summaries while preserving exact collected node IDs."""

    output = "\n".join(
        [
            "tests/test_example.py::test_plain",
            "tests/test_example.py::test_case[remote-user]",
            "",
            "2 tests collected in 0.03s",
        ]
    )

    assert parse_collected_nodeids(output) == (
        "tests/test_example.py::test_plain",
        "tests/test_example.py::test_case[remote-user]",
    )


def test_extract_critical_coverage_reports_missing_modules() -> None:
    """Represent unexercised critical modules explicitly instead of omitting them."""

    payload = {
        "files": {
            "src/bridge_policy.py": {
                "summary": {
                    "covered_lines": 12,
                    "num_statements": 20,
                    "covered_branches": 4,
                    "num_branches": 8,
                }
            }
        }
    }

    result = extract_critical_coverage(payload)

    assert result["src/bridge_policy.py"] == {
        "covered_lines": 12,
        "num_statements": 20,
        "covered_branches": 4,
        "num_branches": 8,
        "measured": True,
    }
    assert set(result) == set(CRITICAL_MODULES)
    assert result["src/user_bans.py"] == {"measured": False}


def test_write_summary_is_deterministic(tmp_path: Path) -> None:
    """Write stable JSON so baseline diffs and tooling consumers are predictable."""

    path = tmp_path / "summary.json"
    summary = {"z": 1, "a": {"d": 4, "b": 2}}

    write_summary(path, summary)

    assert path.read_text(encoding="utf-8") == (
        '{\n  "a": {\n    "b": 2,\n    "d": 4\n  },\n  "z": 1\n}\n'
    )


def test_failed_collection_writes_partial_summary_and_returns_nonzero(
    tmp_path: Path,
) -> None:
    """Preserve diagnostics and stop before running tests after collection fails."""

    calls: list[tuple[str, ...]] = []

    def failing_runner(command: tuple[str, ...], cwd: Path) -> CommandResult:
        """Return a deterministic collection failure for the first command."""

        calls.append(command)
        return CommandResult(exit_code=3, stdout="collection failed", elapsed_seconds=0.2)

    exit_code = run_baseline(
        output_dir=tmp_path,
        project_root=Path("/project"),
        python_executable="python",
        runner=failing_runner,
        groups=("behavior",),
    )

    assert exit_code == 3
    assert len(calls) == 1
    assert "--collect-only" in calls[0]
    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert summary["groups"]["behavior"]["collection_exit_code"] == 3
    assert summary["groups"]["behavior"]["status"] == "collection_failed"
    assert summary["pilot_domain"] == "ban_management"


def test_unknown_group_is_rejected_before_subprocess_execution(tmp_path: Path) -> None:
    """Reject unsupported group names instead of silently changing suite scope."""

    def unexpected_runner(command: tuple[str, ...], cwd: Path) -> CommandResult:
        pytest.fail(f"runner must not be called: {command}")

    with pytest.raises(ValueError, match="unknown test group"):
        run_baseline(
            output_dir=tmp_path,
            project_root=Path("/project"),
            python_executable="python",
            runner=unexpected_runner,
            groups=("missing",),
        )
