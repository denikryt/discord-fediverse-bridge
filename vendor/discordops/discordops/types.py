"""Public result types returned by DiscordOps runners."""

from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class OperationResult:
    """Describe the observable outcome of an operation execution.

    ``applied`` distinguishes successful operation bodies from precondition
    rejection. ``reason`` and ``extra_kwargs`` preserve application-owned
    rejection metadata without coupling the framework to a specific adapter.
    """

    applied: bool
    message: str
    reason: Optional[str] = None
    extra_kwargs: Optional[dict[str, Any]] = None


@dataclass(frozen=True)
class PolicyResult:
    """Describe whether an ordered, body-less policy allowed its input.

    Successful results contain no rejection metadata. Failed results expose the
    first failed precondition's stable reason, resolved message, and optional
    application-owned keyword arguments.
    """

    allowed: bool
    reason: Optional[str] = None
    message: Optional[str] = None
    extra_kwargs: Optional[dict[str, Any]] = None
