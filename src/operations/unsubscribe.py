"""Framework-backed unsubscribe policy for channel-to-community mappings.

When the last Discord channel binding is removed, an unclaimed Follow is
cancelled without networking; a proven sent Follow is undone only after a
durable CAS claim. Uncertain network outcomes preserve the original Follow ID
for on-demand recovery instead of making a second send. Other channels sharing
the Follow are unaffected by a single channel's unsubscribe.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from discordops import OperationDefinition, OperationResult, Precondition

from ..bridge_policy import BridgePolicyService
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
    policy_service: BridgePolicyService
    _subscription: object | None = field(default=None, init=False, repr=False)
    _subscription_loaded: bool = field(default=False, init=False, repr=False)

    def get_subscription(self) -> object | None:
        """Load and memoize the channel subscription row for lifecycle checks."""
        # Preconditions and success messaging both depend on the current row, so
        # the input memoizes the DB lookup.
        if not self._subscription_loaded:
            self._subscription = (
                self.database.remote_subscriptions.get_subscription_by_channel(
                    self.channel_id
                )
            )
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
    """Atomically remove one binding and safely finish only a proven Follow send.

    The last unsubscriber and the outbound Follow sender serialize through
    SQLite CAS. When the remote result is ambiguous, this command reports
    unresolved cleanup without discarding the immutable Follow identity.
    """
    subscription = operation_input.get_subscription()
    if subscription is None:
        return OperationResult(applied=False, message=_missing_message(operation_input),
                               reason="subscription_missing_during_delete")
    label = _community_label(subscription)
    result = operation_input.database.bridge_actor_follows.cancel_channel_binding(
        operation_input.channel_id
    )
    if result.outcome == "not_found":
        return OperationResult(applied=False, message=_missing_message(operation_input),
                               reason="subscription_missing_during_delete")
    if result.outcome == "missing_identity":
        return OperationResult(
            applied=False, reason="follow_activity_id_missing",
            message=(f"Could not unsubscribe {operation_input.channel_mention} from **{label}** "
                     "because the bridge follow activity id is missing; no local binding was deleted."),
        )
    if result.outcome in ("other_bindings", "prepared_cancelled"):
        # A prepared Follow was deleted before any sender could take its CAS;
        # a shared Follow still belongs to the remaining Discord channels.
        return OperationResult(
            applied=True,
            message=f"Unsubscribed {operation_input.channel_mention} from **{label}**.",
        )

    actor_id = result.community_actor_id
    follow_id = result.follow_activity_id
    inbox_url = result.community_inbox_url
    if not actor_id or not follow_id or not inbox_url:
        raise RuntimeError("Follow cancellation returned an incomplete committed generation")

    if result.outcome == "await_follow_send":
        return OperationResult(
            applied=False, reason="remote_follow_cleanup_pending",
            message=(f"Unsubscribed {operation_input.channel_mention} locally. "
                     f"Follow {follow_id} could have reached {actor_id}; "
                     "its network result is not confirmed. The same generation is retained."),
        )
    if result.outcome == "undo_unresolved":
        return OperationResult(
            applied=False, reason="remote_follow_cleanup_unresolved",
            message=_unresolved_undo_message(actor_id, follow_id),
        )
    if result.outcome != "ready_for_undo":
        raise RuntimeError(f"Unsupported Follow cancellation result: {result.outcome}")

    success = await complete_cancelled_remote_follow(
        database=operation_input.database,
        fedify_gateway=operation_input.fedify_gateway,
        policy_service=operation_input.policy_service,
        community_actor_id=actor_id,
        community_inbox_url=inbox_url,
        follow_activity_id=follow_id,
    )
    if success:
        return OperationResult(applied=True,
                               message=f"Unsubscribed {operation_input.channel_mention} from **{label}**.")
    return OperationResult(
        applied=False,
        reason="remote_follow_cleanup_unresolved",
        message=_unresolved_undo_message(actor_id, follow_id),
    )


async def complete_cancelled_remote_follow(
    *,
    database: Database,
    fedify_gateway: FedifyGatewayClient,
    policy_service: BridgePolicyService,
    community_actor_id: str,
    community_inbox_url: str,
    follow_activity_id: str,
) -> bool:
    """After a proven Follow send, attempt one CAS-claimed Undo on its original route.

    This is invoked both by the last-channel unsubscribe and by a Follow sender
    that finishes after all channel bindings were removed. Unknown HTTP outcome
    retains ``undo_sending`` and cannot be blindly retried after restart.
    """
    if (
        not policy_service.federation_decision(community_actor_id).allowed
        or not policy_service.federation_decision(community_inbox_url).allowed
    ):
        return False
    if not database.bridge_actor_follows.claim_follow_undo(community_actor_id, follow_activity_id):
        return False
    try:
        result = await fedify_gateway.unfollow_community(
            community_actor_id, follow_activity_id,
            original_inbox_url=community_inbox_url,
        )
    except Exception:
        # An exception after the claimed attempt is an ambiguous send, not a
        # proof of failure. Leave the original generation and Undo ID intact.
        logger.exception("Undo(Follow) delivery is unresolved for %s", follow_activity_id)
        return False
    if not result.accepted:
        logger.warning("Undo(Follow) outcome unresolved for %s", follow_activity_id)
        return False
    return database.bridge_actor_follows.finish_follow_undo(
        community_actor_id, follow_activity_id
    )


def _unresolved_undo_message(community_actor_id: str, follow_activity_id: str) -> str:
    """Give the operator the immutable provenance needed for guarded cleanup."""
    from ..activitypub_identity_urls import derive_service_undo_follow_activity_id
    undo_id = derive_service_undo_follow_activity_id(follow_activity_id)
    return (
        f"remote_follow_cleanup_unresolved: Group={community_actor_id}; "
        f"Follow={follow_activity_id}; Undo={undo_id or 'unavailable for legacy Follow ID'}. "
        "Verify the remote follower state with the remote administrator, stop old runtimes, "
        "take a verified backup and follow the guarded offline cleanup procedure "
        "in docs/FEDERATION.md before subscribing again."
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
