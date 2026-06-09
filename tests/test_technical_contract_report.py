"""Tests for technical-contract manifests and report aggregation."""

from __future__ import annotations

from dataclasses import dataclass

from support.technical_contract_manifest import TECHNICAL_CONTRACT_ENTRIES
from tools.gateway_contract_runner import merge_status
from tools.technical_contract_report import build_report


@dataclass(frozen=True, slots=True)
class TechnicalEntryFixture:
    """Typed technical-contract entry fixture for report tests."""

    rule_id: str
    family: str
    owner_kind: str
    owners: tuple[str, ...]


def test_technical_manifest_has_unique_nonempty_rule_owners() -> None:
    """Every technical rule must have a stable unique ID and executable owner."""

    ids = [entry.rule_id for entry in TECHNICAL_CONTRACT_ENTRIES]
    assert len(ids) == len(set(ids))
    assert all(entry.owners for entry in TECHNICAL_CONTRACT_ENTRIES)


def test_technical_report_detects_missing_native_owner() -> None:
    """A declared rule without collected native evidence remains visible as a gap."""

    entries = (
        TechnicalEntryFixture(
            rule_id="python",
            family="x",
            owner_kind="pytest",
            owners=("a.py::",),
        ),
        TechnicalEntryFixture(
            rule_id="gateway",
            family="x",
            owner_kind="gateway",
            owners=("tests/verify-x.ts",),
        ),
    )
    report = build_report(
        entries, {"a.py::test_a": "passed"}, {"check": "passed", "scripts": {}}
    )
    assert report["missing_rule_ids"] == ["gateway"]
    assert report["summary"]["represented_rules"] == 1


def test_gateway_status_merge_preserves_only_current_discovered_scripts() -> None:
    """Chunked execution keeps prior results but removes stale script names."""

    merged = merge_status(
        {
            "check": "passed",
            "scripts": {"tests/verify-a.ts": "passed", "tests/old.ts": "passed"},
        },
        discovered=("tests/verify-a.ts", "tests/verify-b.ts"),
        updates={"tests/verify-b.ts": "failed"},
        check_status=None,
    )
    assert merged == {
        "check": "passed",
        "scripts": {
            "tests/verify-a.ts": "passed",
            "tests/verify-b.ts": "failed",
        },
    }
