"""Report tests for outbound fanout rule ownership."""

from types import SimpleNamespace
from tools.fanout_contract_report import build_report


def test_fanout_report_detects_missing_rule_prefixes() -> None:
    entries = (
        SimpleNamespace(
            rule_id="present",
            family="routing",
            classification="typed_case",
            node_prefixes=("file.py::test_present",),
        ),
        SimpleNamespace(
            rule_id="missing",
            family="retry",
            classification="named_scenario",
            node_prefixes=("file.py::test_missing",),
        ),
    )
    report = build_report(entries, {"file.py::test_present": "passed"})
    assert report["missing_rule_ids"] == ["missing"]
    assert report["summary"]["represented_rules"] == 1
