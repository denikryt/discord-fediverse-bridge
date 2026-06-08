"""Framework-backed unsubscribe policy for channel-to-community mappings.

When the last Discord channel subscription for a community is deleted, an
Undo(Follow) is sent to the remote instance and the BridgeActorFollow row
is removed only if that cleanup succeeds. When other channels still subscribe
to the same community, only the ChannelCommunitySubscription row is deleted —
no AP activity is sent.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from discordops import OperationDefinition, OperationResult, Precondition

from ..db import Database
from ..fedify_gateway_client import FedifyGatewayClient, UnfollowCommunityResult

logger = logging.getLogger(__name__)


@dataclass
class UnsubscribeInput:
    """Carry parsed unsubscribe intent plus cached DB state for one attempt."""

    database: Database
    fedify_gateway: FedifyGatewayClient
    channel_id: int
    channel_mention: str
    _subscription: object | None = field(default=None, init=False, repr=False)
    _subscription_loaded: bool = field(default=False, init=False, repr=False)

    def get_subscription(self) -> object | None:
        """Load and memoize the channel subscription row for lifecycle checks."""
        # Preconditions and success messaging both depend on the current row, so
        # the input memoizes the DB lookup.
        if not self._subscription_loaded:
            self._subscription = self.database.remote_subscriptions.get_subscription_by_channel(self.channel_id)
            self._subscription_loaded = True
        return self._subscription


def _missing_message(operation_input: UnsubscribeInput) -> str:
    """Explain that the channel has no subscription to remove."""
    # Missing subscriptions are explained with the original channel mention so
    # the moderator sees exactly which channel lookup failed.
    return f"Channel {operation_input.channel_mention} has no active subscription."


def _community_label(subscription: object) -> str:
    """Return the best moderator-facing label for an existing subscription."""
    # Existing subscription rows may have either a short name or only the actor
    # ID, so the success confirmation follows the same fallback chain as before.
    return subscription.lemmy_community_name or subscription.lemmy_community_actor_id


def _has_channel_subscription(operation_input: UnsubscribeInput) -> bool:
    """Return whether the target channel has a subscription to remove."""
    return operation_input.get_subscription() is not None


def _reject(
    operation_input: UnsubscribeInput,
    *,
    reason: str,
    message: str,
    **_: object,
) -> OperationResult:
    """Return one uniform rejected result for Discord adapters."""
    return OperationResult(applied=False, message=message, reason=reason)


async def _body(operation_input: UnsubscribeInput) -> OperationResult:
    """Delete the channel subscription and send Undo(Follow) if this was the last channel.

    The Undo is only dispatched when no other channel subscriptions remain for
    the same community, because the bridge actor follow is shared across all
    channels that subscribe to the same community. The shared follow row is
    retained until the remote instance accepts the cleanup.
    """
    subscription = operation_input.get_subscription()
    if subscription is None:
        return OperationResult(
            applied=False,
            message=_missing_message(operation_input),
            reason="subscription_missing_during_delete",
        )

    community_actor_id = subscription.lemmy_community_actor_id
    label = _community_label(subscription)
    subscription_count = operation_input.database.remote_subscriptions.count_subscriptions_for_community(
        community_actor_id
    )
    is_last_channel = subscription_count <= 1

    bridge_follow = None
    follow_activity_id: str | None = None
    if is_last_channel:
        # Last-channel cleanup is only safe when the shared follow row still
        # knows the exact outbound Follow activity that must be undone.
        bridge_follow = operation_input.database.bridge_actor_follows.get_bridge_actor_follow(community_actor_id)
        if bridge_follow is not None:
            follow_activity_id = bridge_follow.follow_activity_id
        if follow_activity_id is None:
            return OperationResult(
                applied=False,
                message=(
                    f"Could not unsubscribe {operation_input.channel_mention} from "
                    f"**{label}** because the bridge follow activity id is missing. "
                    "Remote Undo(Follow) cannot be retried safely."
                ),
                reason="follow_activity_id_missing",
            )

    deleted = operation_input.database.remote_subscriptions.delete_subscription(operation_input.channel_id)
    if not deleted:
        return OperationResult(
            applied=False,
            message=_missing_message(operation_input),
            reason="subscription_missing_during_delete",
        )

    if not is_last_channel:
        return OperationResult(
            applied=True,
            message=f"Unsubscribed {operation_input.channel_mention} from **{label}**.",
        )

    # At this point the local channel cleanup already happened. Remote cleanup
    # is attempted afterward, and the bridge follow row survives any failure.
    cleanup_result = await _send_remote_unfollow(
        operation_input,
        community_actor_id=community_actor_id,
        follow_activity_id=follow_activity_id,
    )
    if cleanup_result.accepted:
        operation_input.database.bridge_actor_follows.delete_bridge_actor_follow(community_actor_id)
        return OperationResult(
            applied=True,
            message=f"Unsubscribed {operation_input.channel_mention} from **{label}**.",
        )

    error_detail = cleanup_result.error or "gateway did not confirm cleanup"
    return OperationResult(
        applied=False,
        message=(
            f"Unsubscribed {operation_input.channel_mention} from **{label}** locally, "
            f"but remote Undo(Follow) failed: {error_detail}. "
            "The bridge follow row was kept for retry."
        ),
        reason="remote_unfollow_failed",
    )


async def _send_remote_unfollow(
    operation_input: UnsubscribeInput,
    *,
    community_actor_id: str,
    follow_activity_id: str,
) -> UnfollowCommunityResult:
    """Dispatch one remote Undo(Follow) and convert unexpected errors into retryable failures."""
    try:
        return await operation_input.fedify_gateway.unfollow_community(
            community_actor_id,
            follow_activity_id,
        )
    except Exception:
        # AsyncMock-based command tests and unexpected client regressions may
        # still surface exceptions directly. Preserve retry state either way.
        logger.exception(
            "Could not send Undo(Follow) for community %s; bridge_actor_follows row was preserved",
            community_actor_id,
        )
        return UnfollowCommunityResult(
            accepted=False,
            error="unexpected gateway failure during Undo(Follow)",
        )


unsubscribe_operation = OperationDefinition(
    name="unsubscribe_channel",
    preconditions=(
        Precondition(
            name="channel_subscription_not_found",
            message=_missing_message,
            predicate=_has_channel_subscription,
        ),
    ),
    reject=_reject,
    body=_body,
)
