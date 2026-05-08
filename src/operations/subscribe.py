"""Framework-backed subscribe lifecycle for channel-to-community mappings."""

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
    _bridge_user: object | None = field(default=None, init=False, repr=False)
    _bridge_user_loaded: bool = field(default=False, init=False, repr=False)
    _existing_subscription: object | None = field(default=None, init=False, repr=False)
    _existing_subscription_loaded: bool = field(default=False, init=False, repr=False)

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


def _requested_community_label(operation_input: SubscribeInput) -> str:
    """Return the best moderator-facing label for the requested community."""
    # Moderator messages prefer the short Lemmy name when it is available from
    # autocomplete, but raw actor IDs remain a safe fallback for manual input.
    return operation_input.community_name or operation_input.actor_id


def _existing_community_label(subscription: object) -> str:
    """Return the best label for an existing subscription row."""
    # Existing rows may have a cached human-readable handle or only the raw
    # actor ID, so lifecycle rejections follow the same fallback chain.
    return getattr(subscription, "community_handle", None) or getattr(
        subscription, "lemmy_community_name", None
    ) or getattr(subscription, "lemmy_community_actor_id")


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


async def _body(operation_input: SubscribeInput) -> OperationResult:
    """Dispatch one bridge follow and persist the resulting lifecycle state."""
    existing = operation_input.get_existing_subscription()
    if existing is not None and existing.status == "failed":
        # Failed rows are retriable. Deleting first keeps the channel-level
        # uniqueness contract simple before the replacement row is inserted.
        operation_input.database.delete_subscription(operation_input.channel_id)

    try:
        follow_result = await operation_input.fedify_gateway.follow_community(operation_input.actor_id)
    except Exception:
        # Follow dispatch failures must be durable local state so moderators can
        # see an explicit failed attempt instead of a fake pending row.
        operation_input.database.create_subscription(
            discord_channel_id=operation_input.channel_id,
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

    operation_input.database.create_subscription(
        discord_channel_id=operation_input.channel_id,
        lemmy_community_actor_id=operation_input.actor_id,
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
            predicate=lambda operation_input: operation_input.get_bridge_user() is not None,
        ),
        Precondition(
            name="channel_subscription_not_accepted",
            message=_accepted_message,
            predicate=lambda operation_input: (
                operation_input.get_existing_subscription() is None
                or operation_input.get_existing_subscription().status != "accepted"
            ),
        ),
        Precondition(
            name="channel_subscription_not_pending",
            message=_pending_message,
            predicate=lambda operation_input: (
                operation_input.get_existing_subscription() is None
                or operation_input.get_existing_subscription().status != "pending"
            ),
        ),
    ),
    reject=_reject,
    body=_body,
)
