"""Typed deterministic contracts for channel subscription lifecycle."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Action = Literal["subscribe", "unsubscribe"]
ChannelState = Literal["missing", "accepted", "pending", "failed"]
FollowState = Literal["missing", "accepted", "pending", "failed", "missing_id"]
RemoteOutcome = Literal["accepted", "failed", "not_called"]


@dataclass(frozen=True, slots=True)
class SubscriptionExpected:
    """Describe the externally observable result of one subscription action."""

    applied: bool
    reason: str | None
    follow_calls: int = 0
    unfollow_calls: int = 0
    create_channel_calls: int = 0
    delete_channel_calls: int = 0
    delete_follow_calls: int = 0


@dataclass(frozen=True, slots=True)
class SubscriptionCase:
    """Describe one independent subscription lifecycle contract case."""

    id: str
    action: Action
    registered: bool
    channel_state: ChannelState
    follow_state: FollowState
    subscription_count: int
    remote_outcome: RemoteOutcome
    expected: SubscriptionExpected


SUBSCRIPTION_CASES = (
    SubscriptionCase(
        id="subscribe.unregistered.rejected",
        action="subscribe",
        registered=False,
        channel_state="missing",
        follow_state="missing",
        subscription_count=0,
        remote_outcome="not_called",
        expected=SubscriptionExpected(
            applied=False,
            reason="discord_user_not_registered",
        ),
    ),
    SubscriptionCase(
        id="subscribe.accepted.rejected",
        action="subscribe",
        registered=True,
        channel_state="accepted",
        follow_state="accepted",
        subscription_count=1,
        remote_outcome="not_called",
        expected=SubscriptionExpected(
            applied=False,
            reason="channel_subscription_already_accepted",
        ),
    ),
    SubscriptionCase(
        id="subscribe.pending.rejected",
        action="subscribe",
        registered=True,
        channel_state="pending",
        follow_state="pending",
        subscription_count=1,
        remote_outcome="not_called",
        expected=SubscriptionExpected(
            applied=False,
            reason="channel_subscription_already_pending",
        ),
    ),
    SubscriptionCase(
        id="subscribe.first.follow_pending",
        action="subscribe",
        registered=True,
        channel_state="missing",
        follow_state="missing",
        subscription_count=0,
        remote_outcome="accepted",
        expected=SubscriptionExpected(
            applied=True,
            reason=None,
            follow_calls=1,
            create_channel_calls=1,
        ),
    ),
    SubscriptionCase(
        id="subscribe.reuse_accepted",
        action="subscribe",
        registered=True,
        channel_state="missing",
        follow_state="accepted",
        subscription_count=1,
        remote_outcome="not_called",
        expected=SubscriptionExpected(
            applied=True,
            reason=None,
            create_channel_calls=1,
        ),
    ),
    SubscriptionCase(
        id="subscribe.reuse_pending",
        action="subscribe",
        registered=True,
        channel_state="missing",
        follow_state="pending",
        subscription_count=1,
        remote_outcome="not_called",
        expected=SubscriptionExpected(
            applied=True,
            reason=None,
            create_channel_calls=1,
        ),
    ),
    SubscriptionCase(
        id="unsubscribe.missing.rejected",
        action="unsubscribe",
        registered=True,
        channel_state="missing",
        follow_state="missing",
        subscription_count=0,
        remote_outcome="not_called",
        expected=SubscriptionExpected(
            applied=False,
            reason="channel_subscription_not_found",
        ),
    ),
    SubscriptionCase(
        id="unsubscribe.non_last.local_only",
        action="unsubscribe",
        registered=True,
        channel_state="accepted",
        follow_state="accepted",
        subscription_count=2,
        remote_outcome="not_called",
        expected=SubscriptionExpected(
            applied=True,
            reason=None,
            delete_channel_calls=1,
        ),
    ),
    SubscriptionCase(
        id="unsubscribe.last.success",
        action="unsubscribe",
        registered=True,
        channel_state="accepted",
        follow_state="accepted",
        subscription_count=1,
        remote_outcome="accepted",
        expected=SubscriptionExpected(
            applied=True,
            reason=None,
            unfollow_calls=1,
            delete_channel_calls=1,
            delete_follow_calls=1,
        ),
    ),
    SubscriptionCase(
        id="unsubscribe.last.remote_failure",
        action="unsubscribe",
        registered=True,
        channel_state="accepted",
        follow_state="accepted",
        subscription_count=1,
        remote_outcome="failed",
        expected=SubscriptionExpected(
            applied=False,
            reason="remote_unfollow_failed",
            unfollow_calls=1,
            delete_channel_calls=1,
        ),
    ),
    SubscriptionCase(
        id="unsubscribe.last.missing_follow_id",
        action="unsubscribe",
        registered=True,
        channel_state="accepted",
        follow_state="missing_id",
        subscription_count=1,
        remote_outcome="not_called",
        expected=SubscriptionExpected(
            applied=False,
            reason="follow_activity_id_missing",
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class RequiredRule:
    """Map one declared subscription rule to the cases representing it."""

    id: str
    description: str
    represented_by: tuple[str, ...]


REQUIRED_SUBSCRIPTION_RULES = (
    RequiredRule(
        id="registration_gate",
        description="Subscribe requires bridge registration.",
        represented_by=("subscribe.unregistered.rejected",),
    ),
    RequiredRule(
        id="existing_channel_states",
        description="Accepted and pending channel states reject duplicate subscribe.",
        represented_by=(
            "subscribe.accepted.rejected",
            "subscribe.pending.rejected",
        ),
    ),
    RequiredRule(
        id="follow_creation",
        description="First subscription creates a Follow-backed channel row.",
        represented_by=("subscribe.first.follow_pending",),
    ),
    RequiredRule(
        id="follow_reuse",
        description="Existing accepted or pending bridge follows are reused.",
        represented_by=(
            "subscribe.reuse_accepted",
            "subscribe.reuse_pending",
        ),
    ),
    RequiredRule(
        id="missing_unsubscribe",
        description="Missing channel subscription is rejected.",
        represented_by=("unsubscribe.missing.rejected",),
    ),
    RequiredRule(
        id="shared_follow_retained",
        description="Non-last unsubscribe removes only channel state.",
        represented_by=("unsubscribe.non_last.local_only",),
    ),
    RequiredRule(
        id="last_unfollow_success",
        description="Last unsubscribe removes shared follow after successful Undo.",
        represented_by=("unsubscribe.last.success",),
    ),
    RequiredRule(
        id="last_unfollow_retry",
        description="Remote failure preserves shared follow for retry.",
        represented_by=("unsubscribe.last.remote_failure",),
    ),
    RequiredRule(
        id="missing_follow_id_safe",
        description="Missing Follow ID blocks unsafe local cleanup.",
        represented_by=("unsubscribe.last.missing_follow_id",),
    ),
)
