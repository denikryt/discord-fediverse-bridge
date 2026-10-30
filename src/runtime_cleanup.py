"""Retain unsafe runtime owners and prevent service after unconfirmed teardown.

A fatal shutdown is a process-exit condition. Strong process-level references
keep kernel lock descriptors alive even if callers discard the exception.
Only a confirmed explicit cleanup retry may clear the quarantine.
"""

from __future__ import annotations

import logging

# Failed startup has no caller-owned Runtime, so retaining only local variables
# or exception tracebacks would let GC silently drop the protective leases.
_unsafe_owners: dict[int, object] = {}


class UnsafeRuntimeShutdown(SystemExit):
    """Exit unsuccessfully with every teardown failure retained as the cause."""

    def __init__(self, failures: list[BaseException]) -> None:
        super().__init__(1)
        self.__cause__ = BaseExceptionGroup("Unconfirmed runtime shutdown", failures)


def quarantine(owner: object, failures: list[BaseException]) -> UnsafeRuntimeShutdown:
    """Keep all resources alive until explicit cleanup succeeds or process exits."""
    _unsafe_owners[id(owner)] = owner
    error = UnsafeRuntimeShutdown(failures)
    # SystemExit alone suppresses tracebacks at the entry point. Emit the full
    # cause group before exiting so the original startup failure is never lost.
    cause = error.__cause__
    logging.getLogger(__name__).critical(
        "Unsafe runtime teardown; process must exit",
        exc_info=(type(cause), cause, cause.__traceback__),
    )
    return error


def release_quarantine(owner: object) -> None:
    """Forget an owner only after all resources and both leases have closed."""
    _unsafe_owners.pop(id(owner), None)


def require_safe_process() -> None:
    """Reject new runtimes when any prior owner has unconfirmed shutdown."""
    if _unsafe_owners:
        raise UnsafeRuntimeShutdown([RuntimeError("Process has unclosed runtime resources")])
