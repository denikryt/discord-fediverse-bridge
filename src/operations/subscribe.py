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

import logging
from dataclasses import dataclass, field

import httpx

from discordops import OperationDefinition, OperationResult, Precondition

from .common_preconditions import DISCORD_USER_REGISTERED

from ..bridge_policy import BridgePolicyService
from ..db import Database
from ..fedify_gateway_client import FedifyGatewayClient, FollowCommunityResult, FollowNotDispatchedError
from ..db.repositories.bridge_actor_follows import BridgeActorFollowRepository, FollowReservationConflict

logger = logging.getLogger(__name__)


def _follow_dispatch_error_summary(error: Exception) -> str:
    """Return a safe diagnostic without logging gateway payloads or secrets.

    A verified local actor lookup failure has a useful non-secret error text.
    Other gateway failures may contain request data, so log only their status
    or exception class while retaining the original uncertain send state.
    """
    if isinstance(error, httpx.HTTPStatusError):
        try:
            detail = error.response.json().get("error")
        except (ValueError, AttributeError):
            detail = None
        if (
            isinstance(detail, str)
            and detail.startswith((
                "No actor found for the given username ",
                "No actor found for the given identifier ",
            ))
            and len(detail) < 200
            and all(character.isprintable() for character in detail)
        ):
            return detail
        return f"gateway HTTP {error.response.status_code}"
    return type(error).__name__


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
    remote_software_profile: str = "unknown"
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
    """Allow retry only when the original Follow is proven not dispatched."""
    if _channel_sub_status(operation_input) != "pending":
        return True
    binding = operation_input.get_existing_subscription()
    if getattr(binding, "lemmy_community_actor_id", None) != operation_input.actor_id:
        return False
    follow = operation_input.get_bridge_actor_follow()
    # This is only a fast precondition; reservation rechecks everything under
    # the SQLite writer lock, including the binding's exact Follow generation.
    return bool(
        follow is not None
        and follow.follow_dispatch_state == "prepared"
        and follow.status == "pending"
        and not follow.cancel_requested
        and follow.follow_activity_id == getattr(binding, "follow_activity_id", None)
    )


async def _body(operation_input: SubscribeInput) -> OperationResult:
    """Reserve the original Follow and channel before CAS-claimed federation send.

    A later gateway timeout is not evidence that no Follow reached the remote
    Group. The same immutable generation therefore remains pending until a
    signed Accept or the guarded cancellation flow (CP11) resolves it.
    """
    decision = operation_input.policy_service.federation_decision(operation_input.actor_id)
    if not decision.allowed:
        return OperationResult(
            applied=False,
            reason="federation_policy_denied",
            message=f"Federation policy denies **{_requested_community_label(operation_input)}**.",
        )

    existing = operation_input.get_bridge_actor_follow()
    if existing is not None and existing.follow_dispatch_state == "undo_sending":
        # A signed Undo may already have left this process. Restart does not
        # establish its outcome, so no new Follow generation can be prepared.
        from .unsubscribe import _unresolved_undo_message
        return OperationResult(
            applied=False,
            reason="remote_follow_cleanup_unresolved",
            message=_unresolved_undo_message(
                existing.community_actor_id, existing.follow_activity_id or "missing"
            ),
        )
    if existing is not None and existing.status in ("pending", "accepted"):
        # Reuse the original actor/inbox/ID; discovery is not authoritative
        # over a previously committed Follow generation.
        if not existing.follow_activity_id or not existing.community_inbox_url:
            return OperationResult(applied=False, reason="follow_identity_unresolved",
                                   message="Existing Follow has incomplete identity; operator verification is required.")
        prepared = FollowCommunityResult(
            community_actor_url=existing.community_actor_id,
            community_inbox_url=existing.community_inbox_url,
            follow_activity_id=existing.follow_activity_id,
        )
    else:
        try:
            prepared = await operation_input.fedify_gateway.prepare_community_follow(operation_input.actor_id)
        except Exception:
            # Preparation is read-only with respect to AP send. A failed
            # discovery must not create a fake failed relationship.
            return OperationResult(applied=False, reason="follow_prepare_failed",
                                   message="Could not verify the remote Group for Follow; no Follow was sent.")

    if prepared.community_actor_url != operation_input.actor_id:
        return OperationResult(applied=False, reason="follow_actor_mismatch",
                               message="Remote Group identity changed during Follow preparation.")
    if not prepared.follow_activity_id or not prepared.community_inbox_url:
        return OperationResult(applied=False, reason="follow_identity_unresolved",
                               message="Remote Group did not provide a complete Follow target.")
    if not operation_input.policy_service.federation_decision(prepared.community_inbox_url).allowed:
        return OperationResult(applied=False, reason="federation_policy_denied",
                               message="Federation policy denies the verified Group inbox.")

    try:
        # The IMMEDIATE lock serializes concurrent bindings to the same Group.
        # No gateway send occurs until both entities are committed.
        with operation_input.database.immediate_session() as session:
            follow, prepared_to_send = BridgeActorFollowRepository.reserve_with_binding(
                session,
                community_actor_id=prepared.community_actor_url,
                follow_activity_id=prepared.follow_activity_id,
                community_inbox_url=prepared.community_inbox_url,
                remote_software_profile=operation_input.remote_software_profile,
                discord_channel_id=operation_input.channel_id,
                discord_guild_id=operation_input.guild_id,
                community_name=operation_input.community_name,
                numeric_id=operation_input.numeric_id,
                community_handle=operation_input.community_handle,
                initiated_by_discord_user_id=operation_input.discord_user_id,
            )
            follow_id = follow.id
            committed = FollowCommunityResult(
                community_actor_url=follow.community_actor_id,
                community_inbox_url=follow.community_inbox_url,
                follow_activity_id=follow.follow_activity_id,
            )
    except FollowReservationConflict as error:
        if "cleanup" in str(error):
            # A concurrent unsubscriber might have claimed Undo since this
            # operation's earlier read. Report the exact retained generation.
            from .unsubscribe import _unresolved_undo_message
            current = operation_input.database.bridge_actor_follows.get_bridge_actor_follow(
                operation_input.actor_id
            )
            if current is not None and current.follow_dispatch_state == "undo_sending":
                return OperationResult(
                    applied=False, reason="remote_follow_cleanup_unresolved",
                    message=_unresolved_undo_message(
                        current.community_actor_id, current.follow_activity_id or "missing"
                    ),
                )
        return OperationResult(applied=False, reason="follow_reservation_conflict", message=str(error))

    if prepared_to_send and operation_input.database.bridge_actor_follows.claim_prepared_follow(follow_id):
        try:
            await operation_input.fedify_gateway.send_prepared_community_follow(committed)
        except Exception as error:
            # Only an explicit gateway attestation of a pre-network failure
            # releases the original attempt; response loss remains ambiguous.
            if isinstance(error, FollowNotDispatchedError):
                operation_input.database.bridge_actor_follows.reset_unsent_follow(follow_id)
                current_binding = operation_input.database.remote_subscriptions.get_subscription_by_channel(
                    operation_input.channel_id
                )
                current_follow = operation_input.database.bridge_actor_follows.get_bridge_actor_follow(
                    committed.community_actor_url
                )
                if current_binding is None or current_follow is None or current_follow.cancel_requested:
                    return OperationResult(
                        applied=False, reason="follow_cancelled_during_dispatch",
                        message="Channel unsubscribed during the unsuccessful Follow attempt; no Follow was sent.",
                    )
                if current_follow.follow_dispatch_state == "prepared":
                    logger.warning(
                        "Follow sender unavailable before HTTP: group=%s follow_id=%s; safe same-ID retry prepared",
                        committed.community_actor_url, committed.follow_activity_id,
                    )
                    return OperationResult(
                        applied=False, reason="follow_sender_resolution_failed",
                        message=("Bridge service signing key unavailable; Follow was not dispatched. "
                                 "Retry /subscribe-community on this channel after fixing the sender. "
                                 "The original Follow ID is preserved."),
                    )
                # A concurrent accepted or completed generation must not be
                # downgraded merely because this caller received an error.
            # Generic gateway/HTTP errors do not prove the remote Group did
            # not receive Follow, even if their message contains 'No actor'.
            failure_detail = _follow_dispatch_error_summary(error)
            logger.error(
                "Prepared Follow dispatch unresolved: group=%s follow_id=%s reason=%s; "
                "retaining the original sending generation",
                committed.community_actor_url, committed.follow_activity_id,
                failure_detail,
            )
            # A lost response is ambiguous, not a proven failed delivery. The
            # original Follow ID remains in ``sending`` for on-demand recovery.
            # A moderator can cancel the channel while HTTP is in flight; a
            # timed-out sender must not subsequently claim it is subscribed.
            current_binding = operation_input.database.remote_subscriptions.get_subscription_by_channel(
                operation_input.channel_id
            )
            current_follow = operation_input.database.bridge_actor_follows.get_bridge_actor_follow(
                committed.community_actor_url
            )
            if (
                current_binding is None
                or current_follow is None
                or current_follow.cancel_requested
            ):
                return OperationResult(
                    applied=False, reason="follow_cancelled_during_dispatch",
                    message=("The channel unsubscribed while Follow delivery was unresolved. "
                             "The original Follow generation is retained for guarded cleanup."),
                )
            return OperationResult(
                applied=True,
                reason="pending_remote_confirmation",
                message=(f"Follow for **{_requested_community_label(operation_input)}** is pending "
                         "remote confirmation; the original Follow ID is retained. "
                         "Waiting for federation acceptance."),
            )
        operation_input.database.bridge_actor_follows.complete_prepared_follow(follow_id)

        # The last channel may have unsubscribed while HTTP was in flight.
        # Only a *proven* completed Follow may proceed to a claimed Undo.
        post_send = operation_input.database.bridge_actor_follows.get_bridge_actor_follow(
            committed.community_actor_url
        )
        if post_send is not None and post_send.cancel_requested:
            from .unsubscribe import complete_cancelled_remote_follow
            await complete_cancelled_remote_follow(
                database=operation_input.database,
                fedify_gateway=operation_input.fedify_gateway,
                policy_service=operation_input.policy_service,
                community_actor_id=committed.community_actor_url,
                community_inbox_url=committed.community_inbox_url,
                follow_activity_id=committed.follow_activity_id,
            )

    # Another channel may have already received the signed Accept while our
    # HTTP request was in flight. Never downgrade that accepted DB state.
    current = operation_input.database.bridge_actor_follows.get_bridge_actor_follow(committed.community_actor_url)
    binding = operation_input.database.remote_subscriptions.get_subscription_by_channel(
        operation_input.channel_id
    )
    if current is None or binding is None or current.cancel_requested:
        # An unsubscribe may have cancelled this command's binding between
        # commit and HTTP return. Do not claim the channel is still active.
        return OperationResult(
            applied=False, reason="follow_cancelled_during_dispatch",
            message="Follow was cancelled by a concurrent unsubscribe; no active channel subscription remains.",
        )
    if current is not None and current.status == "accepted":
        return OperationResult(applied=True, message=(
            f"Subscribed {operation_input.channel_mention} to **{_requested_community_label(operation_input)}**."
        ))
    return OperationResult(
        applied=True,
        reason="pending_remote_confirmation",
        message=(f"Reserved a bridge follow for {operation_input.channel_mention} -> "
                 f"**{_requested_community_label(operation_input)}**. Waiting for federation acceptance."),
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
