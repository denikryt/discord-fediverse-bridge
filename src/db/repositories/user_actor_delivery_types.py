"""Shared data contracts and diagnostics helpers for User Actor deliveries."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

ERROR_LIMIT = 500


@dataclass(frozen=True, slots=True)
class ClaimedUserActorDelivery:
    """Describe one exclusively claimed endpoint delivery."""

    delivery_id: int
    source_id: int
    user_id: int
    logical_version: int
    activity_id: str
    actor_username: str
    actor_url: str
    target_endpoint_url: str
    represented_actor_ids: tuple[str, ...]
    activity_json: dict
    claim_token: str


@dataclass(frozen=True, slots=True)
class DeliveryDiagnostic:
    """Expose bounded non-secret delivery state for local operators."""

    delivery_id: int
    source_id: int
    username: str
    source_kind: str
    logical_version: int
    activity_id: str
    target_endpoint_url: str
    represented_actor_ids: tuple[str, ...]
    status: str
    attempt_count: int
    last_attempted_at: datetime | None
    next_attempt_at: datetime | None
    last_error: str | None


@dataclass(frozen=True, slots=True)
class DeliveryRetryResult:
    """Summarize one compare-and-set retry reset operation."""

    matched: int
    reset: int
    skipped: int
    remaining: int
    outcome: str = "success"


def bound_delivery_error(error: str) -> str:
    """Normalize, redact, and bound delivery errors before persistence or display."""
    # Diagnostics can be copied from transport exceptions, so redact both
    # private-key blocks and bearer credentials before any operator-visible output.
    redacted = re.sub(
        r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----",
        "[redacted private key]",
        error,
        flags=re.DOTALL | re.IGNORECASE,
    )
    redacted = re.sub(r"(?i)bearer\s+[^\s,;]+", "Bearer [redacted]", redacted)
    return " ".join(redacted.split())[:ERROR_LIMIT]
