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

from .common_preconditions import DISCORD_USER_REGISTERED

from ..bridge_policy import BridgePolicyService
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
    policy_service: BridgePolicyService
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
            self._bridge_user = self.database.users.get_user_by_discord_user_id(
                self.discord_user_id
            )
            self._bridge_user_loaded = True
        return self._bridge_user

    def get_existing_subscription(self) -> object | None:
        """Load and memoize the channel subscription row for lifecycle checks."""
        # Multiple preconditions and the body inspect the same row, so the
        # input keeps one DB lookup per operation execution.
        if not self._existing_subscription_loaded:
            self._existing_subscription = (
                self.database.remote_subscriptions.get_subscription_by_channel(
                    self.channel_id
                )
            )
            self._existing_subscription_loaded = True
        return self._existing_subscription

    def get_bridge_actor_follow(self) -> object | None:
        """Load and memoize the bridge-actor follow row for this community."""
        # Preconditions and the body both consult the AP-level follow state.
        # Memoizing avoids a second DB round-trip for the same community.
        if not self._bridge_actor_follow_loaded:
            self._bridge_actor_follow = (
                self.database.bridge_actor_follows.get_bridge_actor_follow(
                    self.actor_id
                )
            )
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


def _has_no_accepted_channel_subscription(operation_input: SubscribeInput) -> bool:
    """Return whether the channel lacks an already accepted subscription."""
    return _channel_sub_status(operation_input) != "accepted"


def _has_no_pending_channel_subscription(operation_input: SubscribeInput) -> bool:
    """Return whether the channel lacks an already pending subscription."""
    return _channel_sub_status(operation_input) != "pending"


def _persist_follow_result(
    operation_input,
    expected_subscription,
    expected_surface,
    expected_follow,
    *,
    actor_id,
    follow_id,
    inbox_url,
    status,
):
    """CAS channel and shared Follow state together after any network wait ends."""
    from sqlalchemy import select
    from ..models import BridgeActorFollow
    from ..db.repositories.platform_surfaces import PlatformSurfaceRepository

    with operation_input.database.immediate_session() as session:
        surface = PlatformSurfaceRepository.discord_channel(
            session, operation_input.channel_id, operation_input.guild_id, "forum"
        )
        # A retry may not reclaim a deleted/reassigned channel, even if its
        # physical location survived and now appears unbound again.
        if expected_subscription is not None:
            if expected_surface is None or surface.id != expected_surface.id:
                raise ValueError("Subscription surface changed during retry")
            operation_input.database.remote_subscriptions.upsert_retry_state(
                session,
                expected_subscription_id=expected_subscription.id,
                surface_id=surface.id,
                expected_actor_id=expected_subscription.lemmy_community_actor_id,
                expected_follow_activity_id=expected_subscription.follow_activity_id,
                expected_created_at=expected_subscription.created_at,
                expected_updated_at=expected_subscription.updated_at,
                discord_guild_id=(
                    int(surface.external_guild_id)
                    if surface.external_guild_id is not None
                    else None
                ),
                lemmy_community_actor_id=actor_id,
                lemmy_community_name=operation_input.community_name,
                lemmy_community_id=operation_input.numeric_id,
                community_handle=operation_input.community_handle,
                community_inbox_url=inbox_url,
                follow_activity_id=follow_id,
                initiated_by_discord_user_id=operation_input.discord_user_id,
                status=status,
            )
        else:
            operation_input.database.remote_subscriptions.add_subscription(
                session,
                surface.id,
                lemmy_community_actor_id=actor_id,
                lemmy_community_name=operation_input.community_name,
                lemmy_community_id=operation_input.numeric_id,
                community_handle=operation_input.community_handle,
                community_inbox_url=inbox_url,
                follow_activity_id=follow_id,
                initiated_by_discord_user_id=operation_input.discord_user_id,
                status=status,
            )
        target_follow = session.scalar(
            select(BridgeActorFollow).where(
                BridgeActorFollow.community_actor_id == actor_id
            )
        )
        if expected_follow is not None:
            current = session.get(BridgeActorFollow, expected_follow.id)
            if (
                current is None
                or current.status != expected_follow.status
                or current.follow_activity_id != expected_follow.follow_activity_id
                or current.community_actor_id != expected_follow.community_actor_id
            ):
                raise ValueError("Shared Follow changed during subscription attempt")
            if target_follow is not None and target_follow.id != current.id:
                raise ValueError("Canonical community already owns another Follow")
            if status != "failed":
                # Dispatch failure never erases another channel's failed Follow
                # metadata. Successful retry is the only in-place transition.
                current.community_actor_id = actor_id
                current.status = status
                current.follow_activity_id = follow_id
                current.community_inbox_url = inbox_url
        else:
            if target_follow is not None:
                raise ValueError("Shared Follow appeared during subscription attempt")
            session.add(
                BridgeActorFollow(
                    community_actor_id=actor_id,
                    follow_activity_id=follow_id,
                    community_inbox_url=inbox_url,
                    status=status,
                )
            )
        session.flush()


async def _body(operation_input: SubscribeInput) -> OperationResult:
    """Retry in place, reserve ownership at commit, and never await under writer lock."""
    from sqlalchemy.exc import IntegrityError

    existing_channel_sub = operation_input.get_existing_subscription()
    existing_follow = operation_input.get_bridge_actor_follow()
    expected_surface = operation_input.database.platform_surfaces.resolve_channel(
        "discord-primary", str(operation_input.channel_id)
    )
    actor_id = operation_input.actor_id
    inbox_url = None
    follow_id = None
    status = "pending"
    if existing_follow is not None and existing_follow.status in {
        "accepted",
        "pending",
    }:
        # Shared AP state may be reused across connectors. No additional policy
        # gate or outbound request is introduced for an established Follow.
        status, inbox_url, follow_id = (
            existing_follow.status,
            existing_follow.community_inbox_url,
            existing_follow.follow_activity_id,
        )
    else:
        decision = operation_input.policy_service.federation_decision(actor_id)
        if not decision.allowed:
            return OperationResult(
                applied=False,
                reason="federation_policy_denied",
                message=(
                    f"Could not subscribe {operation_input.channel_mention} to **{_requested_community_label(operation_input)}** because federation policy denies the instance."
                ),
            )
        try:
            follow_result = await operation_input.fedify_gateway.follow_community(
                actor_id
            )
            actor_id, inbox_url, follow_id = (
                follow_result.community_actor_url,
                follow_result.community_inbox_url,
                follow_result.follow_activity_id,
            )
        except Exception:
            # An existing failed owner and Follow must survive dispatch failure
            # byte-for-byte. Only a first attempt records new visible failure.
            if existing_channel_sub is None:
                try:
                    _persist_follow_result(
                        operation_input,
                        None,
                        expected_surface,
                        existing_follow,
                        actor_id=actor_id,
                        follow_id=None,
                        inbox_url=None,
                        status="failed",
                    )
                except (ValueError, IntegrityError):
                    return OperationResult(
                        applied=False,
                        reason="channel_binding_conflict",
                        message="Channel binding changed during subscription attempt.",
                    )
            return OperationResult(
                applied=False,
                reason="follow_dispatch_failed",
                message=(
                    f"Could not subscribe {operation_input.channel_mention} to **{_requested_community_label(operation_input)}** because the bridge Follow request failed."
                ),
            )
    try:
        _persist_follow_result(
            operation_input,
            existing_channel_sub,
            expected_surface,
            existing_follow,
            actor_id=actor_id,
            follow_id=follow_id,
            inbox_url=inbox_url,
            status=status,
        )
    except (ValueError, IntegrityError):
        return OperationResult(
            applied=False,
            reason="channel_binding_conflict",
            message="Channel binding changed during subscription attempt.",
        )
    if status == "accepted":
        return OperationResult(
            applied=True,
            message=(
                f"Subscribed {operation_input.channel_mention} to **{_requested_community_label(operation_input)}**."
            ),
        )
    return OperationResult(
        applied=True,
        message=(
            f"Sent a bridge follow for {operation_input.channel_mention} -> **{_requested_community_label(operation_input)}**. Waiting for federation acceptance."
        ),
    )


subscribe_operation = OperationDefinition(
    name="subscribe_channel",
    preconditions=(
        DISCORD_USER_REGISTERED,
        # Block if this specific channel already has an accepted subscription.
        # Does not block when a *different* channel's follow was accepted and
        # this channel is subscribing for the first time.
        Precondition(
            name="channel_subscription_already_accepted",
            message=_accepted_message,
            predicate=_has_no_accepted_channel_subscription,
        ),
        # Block if this specific channel is already waiting for acceptance.
        # Does not block when a *different* channel's follow is pending and
        # this channel is piggybacking — that is handled in the body.
        Precondition(
            name="channel_subscription_already_pending",
            message=_pending_message,
            predicate=_has_no_pending_channel_subscription,
        ),
    ),
    reject=_reject,
    body=_body,
)
