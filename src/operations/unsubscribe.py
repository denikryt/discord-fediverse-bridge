"""Framework-backed unsubscribe policy for channel-to-community mappings.

When the last Discord channel subscription for a community is deleted, an
Undo(Follow) is sent to the remote instance and the BridgeActorFollow row
is removed. When other channels still subscribe to the same community, only
the ChannelCommunitySubscription row is deleted — no AP activity is sent.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from discordops import OperationDefinition, OperationResult, Precondition

from ..db import Database
from ..fedify_gateway_client import FedifyGatewayClient

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
            self._subscription = self.database.get_subscription_by_channel(self.channel_id)
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
    channels that subscribe to the same community.
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

    deleted = operation_input.database.delete_subscription(operation_input.channel_id)
    if not deleted:
        return OperationResult(
            applied=False,
            message=_missing_message(operation_input),
            reason="subscription_missing_during_delete",
        )

    # Check how many channel subscriptions remain for this community after deletion.
    remaining = operation_input.database.count_subscriptions_for_community(community_actor_id)

    if remaining == 0:
        # This was the last channel — send Undo(Follow) and clean up the AP follow row.
        bridge_follow = operation_input.database.get_bridge_actor_follow(community_actor_id)
        if bridge_follow is not None and bridge_follow.follow_activity_id is not None:
            try:
                await operation_input.fedify_gateway.unfollow_community(
                    community_actor_id,
                    bridge_follow.follow_activity_id,
                )
            except Exception:
                # Undo delivery failure is logged but does not block local cleanup.
                # The operator can use the CLI tool to retry the Undo manually.
                logger.exception(
                    "Could not send Undo(Follow) for community %s; "
                    "bridge_actor_follows row will still be deleted",
                    community_actor_id,
                )
        operation_input.database.delete_bridge_actor_follow(community_actor_id)

    return OperationResult(
        applied=True,
        message=f"Unsubscribed {operation_input.channel_mention} from **{label}**.",
    )


unsubscribe_operation = OperationDefinition(
    name="unsubscribe_channel",
    preconditions=(
        Precondition(
            name="channel_has_subscription",
            message=_missing_message,
            predicate=lambda operation_input: operation_input.get_subscription() is not None,
        ),
    ),
    reject=_reject,
    body=_body,
)
