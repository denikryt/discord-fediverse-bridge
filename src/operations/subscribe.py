"""Framework-backed subscribe lifecycle for channel-to-community mappings.

The subscribe operation separates two concerns:
  1. BridgeActorFollow — whether our AP actor is following a remote community.
     One row exists per community regardless of how many Discord channels
     subscribe to it.
  2. ChannelCommunitySubscription — which Discord forum channel is mapped to
     which community. Multiple channels can map to the same community.

A Follow is only sent when no existing BridgeActorFollow row exists for the
community. Subsequent subscriptions reuse the existing follow state.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from discordops import OperationDefinition, OperationResult, Precondition

from ..db import Database
from ..fedify_gateway_client import FedifyGatewayClient


@dataclass
class SubscribeInput:
    """Carry parsed subscribe intent plus cached DB state for one attempt.

    The command layer owns Discord parsing and Lemmy lookup. The operation owns
    registration checks, subscription lifecycle decisions, and Follow dispatch.
    """

    database: Database
    fedify_gateway: FedifyGatewayClient
    discord_user_id: str
    channel_id: int
    channel_mention: str
    actor_id: str
    community_name: str | None
    numeric_id: int | None
    community_handle: str
    guild_id: int | None = None
    _bridge_user: object | None = field(default=None, init=False, repr=False)
    _bridge_user_loaded: bool = field(default=False, init=False, repr=False)
    _existing_subscription: object | None = field(default=None, init=False, repr=False)
    _existing_subscription_loaded: bool = field(default=False, init=False, repr=False)
    _bridge_actor_follow: object | None = field(default=None, init=False, repr=False)
    _bridge_actor_follow_loaded: bool = field(default=False, init=False, repr=False)

    def get_bridge_user(self) -> object | None:
        """Load and memoize the registered bridge user for this moderator."""
        # Registration is checked before any follow attempt so moderation flows
        # never create anonymous bridge subscriptions.
        if not self._bridge_user_loaded:
            self._bridge_user = self.database.get_user_by_discord_user_id(self.discord_user_id)
            self._bridge_user_loaded = True
        return self._bridge_user

    def get_existing_subscription(self) -> object | None:
        """Load and memoize the channel subscription row for lifecycle checks."""
        # Multiple preconditions and the body inspect the same row, so the
        # input keeps one DB lookup per operation execution.
        if not self._existing_subscription_loaded:
            self._existing_subscription = self.database.get_subscription_by_channel(self.channel_id)
            self._existing_subscription_loaded = True
        return self._existing_subscription

    def get_bridge_actor_follow(self) -> object | None:
        """Load and memoize the bridge-actor follow row for this community."""
        # Preconditions and the body both consult the AP-level follow state.
        # Memoizing avoids a second DB round-trip for the same community.
        if not self._bridge_actor_follow_loaded:
            self._bridge_actor_follow = self.database.get_bridge_actor_follow(self.actor_id)
            self._bridge_actor_follow_loaded = True
        return self._bridge_actor_follow


def _requested_community_label(operation_input: SubscribeInput) -> str:
    """Return the best moderator-facing label for the requested community."""
    # Moderator messages prefer the short Lemmy name when it is available from
    # autocomplete, but raw actor IDs remain a safe fallback for manual input.
    return operation_input.community_name or operation_input.actor_id


def _existing_community_label(subscription: object) -> str:
    """Return the best label for an existing subscription row."""
    # Existing rows may have a cached human-readable handle or only the raw
    # actor ID, so lifecycle rejections follow the same fallback chain.
    return (
        getattr(subscription, "community_handle", None)
        or getattr(subscription, "lemmy_community_name", None)
        or getattr(subscription, "lemmy_community_actor_id")
    )


def _registration_message(_: SubscribeInput) -> str:
    """Explain why subscribe requires prior bridge registration."""
    return "You must register with the bridge before subscribing a channel. Use `/register` first."


def _accepted_message(operation_input: SubscribeInput) -> str:
    """Explain that the channel already has an active subscription."""
    existing = operation_input.get_existing_subscription()
    return (
        f"Channel {operation_input.channel_mention} is already subscribed to "
        f"**{_existing_community_label(existing)}**."
    )


def _pending_message(operation_input: SubscribeInput) -> str:
    """Explain that the previous follow is still awaiting federation acceptance."""
    existing = operation_input.get_existing_subscription()
    return (
        f"Channel {operation_input.channel_mention} is still waiting for "
        f"**{_existing_community_label(existing)}** to accept the bridge follow."
    )


def _reject(
    operation_input: SubscribeInput,
    *,
    reason: str,
    message: str,
    **_: object,
) -> OperationResult:
    """Return one uniform rejected operation result for Discord adapters."""
    # The command adapter decides whether a rejection is ephemeral, so the
    # operation only returns consistent semantic results.
    return OperationResult(applied=False, message=message, reason=reason)


def _channel_sub_status(operation_input: SubscribeInput) -> str | None:
    """Return the status of the existing channel subscription, or None."""
    sub = operation_input.get_existing_subscription()
    return sub.status if sub is not None else None


async def _body(operation_input: SubscribeInput) -> OperationResult:
    """Dispatch one bridge follow and persist the resulting lifecycle state.

    Preconditions already blocked the accepted and pending channel-level
    cases. The body handles three remaining paths:

    1. Bridge follow accepted for another channel → create channel row as
       accepted immediately (bridge actor already federated).
    2. Bridge follow pending for another channel → piggyback; create channel
       row as pending so it activates when the shared Accept arrives.
    3. No bridge follow (or failed) → send Follow, persist both rows.
    """
    existing_channel_sub = operation_input.get_existing_subscription()
    if existing_channel_sub is not None and existing_channel_sub.status == "failed":
        # Failed rows are retriable. Delete the stale channel row first so the
        # UNIQUE constraint on discord_channel_id allows the replacement insert.
        operation_input.database.delete_subscription(operation_input.channel_id)

    existing_follow = operation_input.get_bridge_actor_follow()

    if existing_follow is not None and existing_follow.status == "accepted":
        # Another channel already established the AP-level follow and it was
        # accepted. Create the new channel row immediately as accepted.
        operation_input.database.create_subscription(
            discord_channel_id=operation_input.channel_id,
            discord_guild_id=operation_input.guild_id,
            lemmy_community_actor_id=operation_input.actor_id,
            lemmy_community_name=operation_input.community_name,
            lemmy_community_id=operation_input.numeric_id,
            community_handle=operation_input.community_handle,
            community_inbox_url=existing_follow.community_inbox_url,
            follow_activity_id=existing_follow.follow_activity_id,
            initiated_by_discord_user_id=operation_input.discord_user_id,
            status="accepted",
        )
        return OperationResult(
            applied=True,
            message=(
                f"Subscribed {operation_input.channel_mention} to "
                f"**{_requested_community_label(operation_input)}**."
            ),
        )

    if existing_follow is not None and existing_follow.status == "pending":
        # A Follow is already in flight — piggyback on it. Create a pending
        # channel row so this channel activates when the shared Accept arrives.
        operation_input.database.create_subscription(
            discord_channel_id=operation_input.channel_id,
            discord_guild_id=operation_input.guild_id,
            lemmy_community_actor_id=operation_input.actor_id,
            lemmy_community_name=operation_input.community_name,
            lemmy_community_id=operation_input.numeric_id,
            community_handle=operation_input.community_handle,
            community_inbox_url=existing_follow.community_inbox_url,
            follow_activity_id=existing_follow.follow_activity_id,
            initiated_by_discord_user_id=operation_input.discord_user_id,
            status="pending",
        )
        return OperationResult(
            applied=True,
            message=(
                f"Sent a bridge follow for {operation_input.channel_mention} -> "
                f"**{_requested_community_label(operation_input)}**. Waiting for federation acceptance."
            ),
        )

    # No existing bridge follow (or failed) — send a fresh Follow.
    if existing_follow is not None and existing_follow.status == "failed":
        # Remove the stale failed bridge follow row before creating a fresh one.
        operation_input.database.delete_bridge_actor_follow(operation_input.actor_id)

    try:
        follow_result = await operation_input.fedify_gateway.follow_community(operation_input.actor_id)
    except Exception:
        # Follow dispatch failures must be durable local state so moderators can
        # see an explicit failed attempt instead of a fake pending row.
        operation_input.database.create_bridge_actor_follow(
            community_actor_id=operation_input.actor_id,
            follow_activity_id=None,
            community_inbox_url=None,
            status="failed",
        )
        operation_input.database.create_subscription(
            discord_channel_id=operation_input.channel_id,
            discord_guild_id=operation_input.guild_id,
            lemmy_community_actor_id=operation_input.actor_id,
            lemmy_community_name=operation_input.community_name,
            lemmy_community_id=operation_input.numeric_id,
            community_handle=operation_input.community_handle,
            community_inbox_url=None,
            follow_activity_id=None,
            status="failed",
        )
        return OperationResult(
            applied=False,
            message=(
                f"Could not subscribe {operation_input.channel_mention} to "
                f"**{_requested_community_label(operation_input)}** because the bridge Follow request failed."
            ),
            reason="follow_dispatch_failed",
        )

    # Persist the bridge-actor follow row first, then the channel subscription.
    # The follow row is the AP-level source of truth; the channel row is the
    # Discord-facing routing record.
    operation_input.database.create_bridge_actor_follow(
        community_actor_id=follow_result.community_actor_url,
        follow_activity_id=follow_result.follow_activity_id,
        community_inbox_url=follow_result.community_inbox_url,
        status="pending",
    )
    operation_input.database.create_subscription(
        discord_channel_id=operation_input.channel_id,
        discord_guild_id=operation_input.guild_id,
        lemmy_community_actor_id=follow_result.community_actor_url,
        lemmy_community_name=operation_input.community_name,
        lemmy_community_id=operation_input.numeric_id,
        community_handle=operation_input.community_handle,
        community_inbox_url=follow_result.community_inbox_url,
        follow_activity_id=follow_result.follow_activity_id,
        initiated_by_discord_user_id=operation_input.discord_user_id,
        status="pending",
    )
    return OperationResult(
        applied=True,
        message=(
            f"Sent a bridge follow for {operation_input.channel_mention} -> "
            f"**{_requested_community_label(operation_input)}**. Waiting for federation acceptance."
        ),
    )


subscribe_operation = OperationDefinition(
    name="subscribe_channel",
    preconditions=(
        Precondition(
            name="discord_user_is_registered",
            message=_registration_message,
            predicate=lambda op: op.get_bridge_user() is not None,
        ),
        # Block if this specific channel already has an accepted subscription.
        # Does not block when a *different* channel's follow was accepted and
        # this channel is subscribing for the first time.
        Precondition(
            name="channel_subscription_not_accepted",
            message=_accepted_message,
            predicate=lambda op: _channel_sub_status(op) != "accepted",
        ),
        # Block if this specific channel is already waiting for acceptance.
        # Does not block when a *different* channel's follow is pending and
        # this channel is piggybacking — that is handled in the body.
        Precondition(
            name="channel_subscription_not_pending",
            message=_pending_message,
            predicate=lambda op: _channel_sub_status(op) != "pending",
        ),
    ),
    reject=_reject,
    body=_body,
)
