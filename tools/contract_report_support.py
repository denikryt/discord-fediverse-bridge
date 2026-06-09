"""Shared passive pytest collection primitives for contract reports."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class CollectedCaseResult:
    """Associate one typed case with its pytest node and terminal status."""

    nodeid: str
    case: Any
    status: str


class PassiveCaseCollector:
    """Collect one parametrized case family without changing pytest semantics."""

    def __init__(
        self,
        *,
        parameter_name: str = "case",
        accepts: Callable[[Any], bool] | None = None,
    ) -> None:
        """Configure the parameter name and optional domain predicate."""

        self.parameter_name = parameter_name
        self.accepts = accepts or (lambda case: True)
        self.cases_by_nodeid: dict[str, Any] = {}
        self.status_by_nodeid: dict[str, str] = {}

    def pytest_collection_modifyitems(self, items: Sequence[Any]) -> None:
        """Remember matching parametrized cases during collection."""

        for item in items:
            callspec = getattr(item, "callspec", None)
            if callspec is None:
                continue
            case = callspec.params.get(self.parameter_name)
            if case is not None and self.accepts(case):
                self.cases_by_nodeid[item.nodeid] = case

    def pytest_runtest_logreport(self, report: Any) -> None:
        """Record one terminal status while preserving pytest's result."""

        if report.nodeid not in self.cases_by_nodeid:
            return
        if report.failed:
            self.status_by_nodeid[report.nodeid] = "failed"
            return
        if report.skipped:
            self.status_by_nodeid[report.nodeid] = (
                "xfailed" if hasattr(report, "wasxfail") else "skipped"
            )
            return
        if report.when == "call" and report.passed:
            self.status_by_nodeid[report.nodeid] = "passed"

    def results(self) -> tuple[CollectedCaseResult, ...]:
        """Return stable results, including setup failures or skips."""

        return tuple(
            CollectedCaseResult(
                nodeid=nodeid,
                case=case,
                status=self.status_by_nodeid.get(nodeid, "failed"),
            )
            for nodeid, case in sorted(self.cases_by_nodeid.items())
        )


def status_totals(results: Sequence[CollectedCaseResult]) -> dict[str, int]:
    """Return deterministic totals for all supported terminal statuses."""

    totals = Counter({"passed": 0, "failed": 0, "skipped": 0, "xfailed": 0})
    totals.update(result.status for result in results)
    return dict(sorted(totals.items()))
