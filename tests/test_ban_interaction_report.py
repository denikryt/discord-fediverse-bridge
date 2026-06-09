"""Verify deterministic pairwise interaction reporting."""

from __future__ import annotations

from tools.ban_interaction_report import build_interaction_report


def test_interaction_report_separates_candidate_and_pair_coverage() -> None:
    """Report pair facts without claiming semantic or branch completeness."""

    report = build_interaction_report(
        factors={"a": ("1", "2"), "b": ("x", "y")},
        valid_cases=({"a": "1", "b": "x"}, {"a": "2", "b": "y"}),
        selected_cases=({"a": "1", "b": "x"},),
        must_include=({"a": "1", "b": "x"},),
        coverage={
            "required_pairs": 2,
            "covered_pairs": 1,
            "missing_pairs": [("a", "2", "b", "y")],
        },
    )

    assert report["summary"] == {
        "valid_candidates": 2,
        "selected_cases": 1,
        "required_pairs": 2,
        "covered_pairs": 1,
        "missing_pairs": 1,
    }
    assert report["missing_pair_details"] == [["a", "2", "b", "y"]]
