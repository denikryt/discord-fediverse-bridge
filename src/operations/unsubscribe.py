"""Framework-backed unsubscribe policy for channel-to-community mappings."""

from __future__ import annotations

from dataclasses import dataclass, field

from discordops import OperationDefinition, OperationResult, Precondition

from ..db import Database


@dataclass
class UnsubscribeInput:
    # The operation only needs enough context to look up and remove one mapping.
    database: Database
    channel_id: int
    channel_mention: str
    _subscription: object | None = field(default=None, init=False, repr=False)
    _subscription_loaded: bool = field(default=False, init=False, repr=False)

    def get_subscription(self) -> object | None:
        # Preconditions and success messaging both depend on the current row, so
        # the input memoizes the DB lookup.
        if not self._subscription_loaded:
            self._subscription = self.database.get_subscription_by_channel(self.channel_id)
            self._subscription_loaded = True
        return self._subscription


def _missing_message(operation_input: UnsubscribeInput) -> str:
    # Missing subscriptions are explained with the original channel mention so
    # the moderator sees exactly which channel lookup failed.
    return f"Channel {operation_input.channel_mention} has no active subscription."


def _community_label(subscription: object) -> str:
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
    return OperationResult(applied=False, message=message, reason=reason)


def _body(operation_input: UnsubscribeInput) -> OperationResult:
    # The body uses the cached row so it can preserve the confirmation label
    # even though the delete API itself only returns a boolean.
    subscription = operation_input.get_subscription()
    if subscription is None:
        return OperationResult(
            applied=False,
            message=_missing_message(operation_input),
            reason="subscription_missing_during_delete",
        )

    deleted = operation_input.database.delete_subscription(operation_input.channel_id)
    if not deleted:
        return OperationResult(
            applied=False,
            message=_missing_message(operation_input),
            reason="subscription_missing_during_delete",
        )

    return OperationResult(
        applied=True,
        message=f"Unsubscribed {operation_input.channel_mention} from **{_community_label(subscription)}**.",
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
