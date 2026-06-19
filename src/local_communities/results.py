"""Result values returned by local-community runtime actions.

This module owns the small observable status object used by the
LocalCommunityRuntime facade.  Keeping it separate lets the runtime and focused
handlers share the result contract without making handlers import the facade.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class LocalCommunityRuntimeResult:
    """Report the observable result of one local-community runtime action."""

    status: str
    reason: str
    activity_id: str | None = None
    object_id: str | None = None
