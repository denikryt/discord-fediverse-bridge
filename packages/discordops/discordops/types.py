"""Generic operation result type."""

from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class OperationResult:
    """Result returned by an operation to its command adapter.

    Fields:
        applied: True if operation succeeded, False if rejected by precondition.
        message: Human-readable message (shown to user).
        reason: Optional reason code (e.g., "actor_authority_missing").
        extra_kwargs: Optional app-specific fields passed through reject handler.
    """

    applied: bool
    message: str
    reason: Optional[str] = None
    extra_kwargs: Optional[dict[str, Any]] = None
