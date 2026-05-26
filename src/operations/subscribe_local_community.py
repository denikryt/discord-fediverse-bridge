"""Framework-backed local-subscriber subscribe policy.

Stage 1 only persists same-instance local subscriber state. It must not send
remote Follow activities or mutate bridge_actor_follows / remote_subscribers.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from discordops import OperationDefinition, OperationResult, Precondition

from ..db import Database


@dataclass
class SubscribeLocalCommunityInput:
    """Carry one local-subscriber subscribe request plus cached DB state."""

    database: Database
    discord_user_id: str
    guild_id: int | None
    channel_id: int
    channel_mention: str
    local_community_id: int
    local_community_name: str
    _bridge_user: object | None = field(default=None, init=False, repr=False)
    _bridge_user_loaded: bool = field(default=False, init=False, repr=False)
    _local_community: object | None = field(default=None, init=False, repr=False)
    _local_community_loaded: bool = field(default=False, init=False, repr=False)
    _existing_local_subscriber: object | None = field(default=None, init=False, repr=False)
    _existing_local_subscriber_loaded: bool = field(default=False, init=False, repr=False)
    _existing_remote_subscription: object | None = field(default=None, init=False, repr=False)
    _existing_remote_subscription_loaded: bool = field(default=False, init=False, repr=False)

    def get_bridge_user(self) -> object | None:
        """Load the registered bridge user initiating the subscription."""
        if not self._bridge_user_loaded:
            self._bridge_user = self.database.get_user_by_discord_user_id(self.discord_user_id)
            self._bridge_user_loaded = True
        return self._bridge_user

    def get_local_community(self) -> object | None:
        """Load the local community row targeted by this subscription."""
        if not self._local_community_loaded:
            self._local_community = self.database.get_local_community_by_id(self.local_community_id)
            self._local_community_loaded = True
        return self._local_community

    def get_existing_local_subscriber(self) -> object | None:
        """Load any existing local-subscriber row for the target channel."""
        if not self._existing_local_subscriber_loaded:
            self._existing_local_subscriber = self.database.get_local_subscriber_by_channel(self.channel_id)
            self._existing_local_subscriber_loaded = True
        return self._existing_local_subscriber

    def get_existing_remote_subscription(self) -> object | None:
        """Load any existing remote-subscription row for the target channel."""
        if not self._existing_remote_subscription_loaded:
            self._existing_remote_subscription = self.database.get_subscription_by_channel(self.channel_id)
            self._existing_remote_subscription_loaded = True
        return self._existing_remote_subscription


def _reject(
    operation_input: SubscribeLocalCommunityInput,
    *,
    reason: str,
    message: str,
    **_: object,
) -> OperationResult:
    """Return one uniform rejection result for the command adapter."""
    return OperationResult(applied=False, message=message, reason=reason)


def _registration_message(_: SubscribeLocalCommunityInput) -> str:
    """Explain why local subscribe still requires bridge registration."""
    return "You must register with the bridge before subscribing a channel. Use `/register` first."


def _host_forum_message(operation_input: SubscribeLocalCommunityInput) -> str:
    """Explain that the host forum cannot subscribe to itself."""
    return (
        f"Channel {operation_input.channel_mention} is the host forum for this local community "
        "and cannot subscribe to itself."
    )


def _already_remote_message(operation_input: SubscribeLocalCommunityInput) -> str:
    """Explain that the channel already has a remote community role."""
    return f"Channel {operation_input.channel_mention} already has a remote community subscription."


def _already_local_message(operation_input: SubscribeLocalCommunityInput) -> str:
    """Explain that the channel is already a local subscriber forum."""
    return (
        f"Channel {operation_input.channel_mention} is already subscribed to local community "
        f"**{operation_input.local_community_name}**."
    )


def _already_local_host_message(operation_input: SubscribeLocalCommunityInput) -> str:
    """Explain that the target channel already hosts another local community."""
    existing = operation_input.database.get_local_community_by_forum_channel_id(operation_input.channel_id)
    display_name = getattr(existing, "display_name", "another local community") if existing is not None else "another local community"
    return f"Channel {operation_input.channel_mention} already hosts **{display_name}**."


def _body(operation_input: SubscribeLocalCommunityInput) -> OperationResult:
    """Persist one local-subscriber row without touching remote follow state."""
    operation_input.database.create_local_subscriber(
        local_community_id=operation_input.local_community_id,
        discord_guild_id=operation_input.guild_id,
        discord_channel_id=operation_input.channel_id,
        initiated_by_discord_user_id=operation_input.discord_user_id,
        status="active",
    )
    return OperationResult(
        applied=True,
        message=(
            f"Subscribed {operation_input.channel_mention} to local community "
            f"**{operation_input.local_community_name}**."
        ),
    )


subscribe_local_community_operation = OperationDefinition(
    name="subscribe_local_community",
    preconditions=(
        Precondition(
            name="discord_user_is_registered",
            message=_registration_message,
            predicate=lambda op: op.get_bridge_user() is not None,
        ),
        Precondition(
            name="local_community_exists",
            message=lambda _: "The selected local community no longer exists.",
            predicate=lambda op: op.get_local_community() is not None,
        ),
        Precondition(
            name="target_is_not_host_forum",
            message=_host_forum_message,
            predicate=lambda op: getattr(op.get_local_community(), "discord_forum_channel_id", None) != op.channel_id,
        ),
        Precondition(
            name="channel_has_no_remote_subscription",
            message=_already_remote_message,
            predicate=lambda op: op.get_existing_remote_subscription() is None,
        ),
        Precondition(
            name="channel_is_not_local_community_host",
            message=_already_local_host_message,
            predicate=lambda op: op.database.get_local_community_by_forum_channel_id(op.channel_id) is None,
        ),
        Precondition(
            name="channel_is_not_already_local_subscriber",
            message=_already_local_message,
            predicate=lambda op: op.get_existing_local_subscriber() is None,
        ),
    ),
    reject=_reject,
    body=_body,
)

