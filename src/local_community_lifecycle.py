"""Lifecycle policy helpers for bridge-owned local communities.

The helpers in this module define the local-only management gate for community
rows. They deliberately return compact decision objects rather than bare bools
so future federation behavior can map the same decision reason to Reject/Update
or Delete-style protocol responses without rewriting every runtime call site.
"""

from __future__ import annotations

from dataclasses import dataclass


ACTIVE_STATUS = "active"
DISABLED_STATUS = "disabled"
VALID_LOCAL_COMMUNITY_STATUSES = {ACTIVE_STATUS, DISABLED_STATUS}
DISABLED_DETAIL = "community is disabled"
DISABLED_MODERATION_MESSAGE = "Community {slug} is disabled. Use /edit-community to re-enable it first."


@dataclass(frozen=True, slots=True)
class LocalCommunityLifecycleDecision:
    """Describe whether one community may accept new side effects.

    `reason` is intentionally stable and compact for operations and logs, while
    `detail` is the human-readable text used by handler/runtime results.
    """

    allowed: bool
    reason: str
    detail: str


def normalize_local_community_status(value: str) -> str | None:
    """Return a valid lifecycle status value, or None when invalid."""
    normalized = (value or "").strip().lower()
    if normalized in VALID_LOCAL_COMMUNITY_STATUSES:
        return normalized
    return None


def is_local_community_disabled(local_community: object) -> bool:
    """Return whether a local-community row is in the disabled lifecycle state."""
    return getattr(local_community, "status", ACTIVE_STATUS) == DISABLED_STATUS


def evaluate_local_community_lifecycle(local_community: object) -> LocalCommunityLifecycleDecision:
    """Return the local lifecycle decision for a community row.

    Unknown legacy status values remain allowed here so the new disabled gate
    only changes behavior for rows explicitly marked disabled. Edit operations
    still validate status strictly before writing a row.
    """
    if is_local_community_disabled(local_community):
        return LocalCommunityLifecycleDecision(
            allowed=False,
            reason="community_disabled",
            detail=DISABLED_DETAIL,
        )
    return LocalCommunityLifecycleDecision(
        allowed=True,
        reason="community_active",
        detail="community is active",
    )


def disabled_moderation_message(slug: str) -> str:
    """Return the shared command error for disabled moderation targets."""
    return DISABLED_MODERATION_MESSAGE.format(slug=slug)
