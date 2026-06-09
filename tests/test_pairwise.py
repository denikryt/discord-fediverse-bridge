"""Verify deterministic constrained pairwise selection."""

from __future__ import annotations

from support.pairwise import (
    coverage_summary,
    enumerate_valid_cases,
    select_pairwise_cases,
)


def test_pairwise_selection_respects_constraints_and_covers_all_valid_pairs() -> None:
    """A small constrained model must cover every valid 2-way interaction."""

    factors = {
        "role": ("admin", "member"),
        "scope": ("global", "local"),
        "state": ("present", "missing"),
    }
    candidates = enumerate_valid_cases(
        factors,
        is_valid=lambda case: not (
            case["scope"] == "global" and case["state"] == "present"
        ),
    )
    must = {"role": "admin", "scope": "global", "state": "missing"}

    first = select_pairwise_cases(candidates, must_include=(must,))
    second = select_pairwise_cases(candidates, must_include=(must,))
    summary = coverage_summary(candidates, first)

    assert first == second
    assert must in first
    assert all(
        not (case["scope"] == "global" and case["state"] == "present")
        for case in first
    )
    assert summary["missing_pairs"] == []
    assert summary["covered_pairs"] == summary["required_pairs"]
    assert len(first) < len(candidates)
