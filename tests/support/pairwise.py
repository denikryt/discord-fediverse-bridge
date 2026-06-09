"""Deterministic constrained pairwise selection for finite test models."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from itertools import combinations, product
from typing import TypeVar

Value = TypeVar("Value", bound=str)
Case = dict[str, str]
Pair = tuple[str, str, str, str]


def enumerate_valid_cases(
    factors: Mapping[str, Sequence[str]],
    *,
    is_valid: Callable[[Mapping[str, str]], bool],
) -> tuple[Case, ...]:
    """Enumerate stable constrained Cartesian candidates."""

    names = tuple(factors)
    return tuple(
        dict(zip(names, values, strict=True))
        for values in product(*(factors[name] for name in names))
        if is_valid(dict(zip(names, values, strict=True)))
    )


def case_pairs(case: Mapping[str, str]) -> frozenset[Pair]:
    """Return all canonical 2-way factor/value pairs in one case."""

    return frozenset(
        (left, case[left], right, case[right])
        for left, right in combinations(case, 2)
    )


def required_pairs(candidates: Sequence[Mapping[str, str]]) -> frozenset[Pair]:
    """Return every valid pair observable in constrained candidates."""

    pairs: set[Pair] = set()
    for candidate in candidates:
        pairs.update(case_pairs(candidate))
    return frozenset(pairs)


def select_pairwise_cases(
    candidates: Sequence[Case],
    *,
    must_include: Sequence[Mapping[str, str]] = (),
) -> tuple[Case, ...]:
    """Greedily cover all valid pairs with deterministic stable tie-breaking."""

    remaining = list(candidates)
    selected: list[Case] = []
    uncovered = set(required_pairs(candidates))

    for required in must_include:
        match = next(
            (candidate for candidate in remaining if candidate == dict(required)),
            None,
        )
        if match is None:
            raise ValueError(f"must-include case is not a valid candidate: {required}")
        selected.append(match)
        remaining.remove(match)
        uncovered.difference_update(case_pairs(match))

    while uncovered:
        scored = [
            (len(case_pairs(candidate).intersection(uncovered)), tuple(candidate.items()), candidate)
            for candidate in remaining
        ]
        if not scored:
            raise AssertionError("valid pairs remain uncovered without candidates")
        gain, _, best = max(scored, key=lambda item: (item[0], tuple(reversed(item[1]))))
        if gain == 0:
            raise AssertionError("pairwise selection made no progress")
        selected.append(best)
        remaining.remove(best)
        uncovered.difference_update(case_pairs(best))

    return tuple(selected)


def coverage_summary(
    candidates: Sequence[Mapping[str, str]],
    selected: Sequence[Mapping[str, str]],
) -> dict[str, object]:
    """Return required, covered, and missing valid pair facts."""

    required = required_pairs(candidates)
    covered: set[Pair] = set()
    for case in selected:
        covered.update(case_pairs(case))
    missing = sorted(required.difference(covered))
    return {
        "required_pairs": len(required),
        "covered_pairs": len(required) - len(missing),
        "missing_pairs": missing,
    }
