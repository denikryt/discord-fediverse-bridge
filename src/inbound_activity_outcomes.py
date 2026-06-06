"""Stable semantic outcomes for inbound bridge activity processing.

These values describe bridge handling results for observability. They are not
ActivityPub protocol states and must remain independent from receipt retry and
idempotency statuses.
"""

from __future__ import annotations

from enum import StrEnum


class InboundActivityOutcome(StrEnum):
    """Classify the semantic result of one inbound handling attempt."""

    APPLIED = "applied"
    IGNORED_INSTANCE_NOT_ALLOWLISTED = "ignored_instance_not_allowlisted"
    IGNORED_BY_BAN = "ignored_by_ban"
    IGNORED_BY_DISABLED_COMMUNITY = "ignored_by_disabled_community"
    IGNORED_DISCORD_ORIGINATED_ECHO = "ignored_discord_originated_echo"
    IGNORED_NO_SUBSCRIPTION = "ignored_no_subscription"
    IGNORED_UNKNOWN_FOLLOW = "ignored_unknown_follow"
    IGNORED_ALREADY_APPLIED = "ignored_already_applied"
    IGNORED_UNKNOWN_LOCAL_COMMUNITY = "ignored_unknown_local_community"
    IGNORED_ACTOR_NOT_SUBSCRIBER = "ignored_actor_not_subscriber"
    IGNORED_UNMAPPED_CONTEXT = "ignored_unmapped_context"
    DEFERRED_MISSING_DEPENDENCY = "deferred_missing_dependency"
    PROCESSING_FAILED = "processing_failed"
