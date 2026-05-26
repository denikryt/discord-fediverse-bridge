"""Framework-backed local-subscriber unsubscribe policy.

Stage 1 local unsubscribe removes only same-instance local-subscriber state. It
must not send remote Undo(Follow) or mutate bridge_actor_follows.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from discordops import OperationDefinition, OperationResult, Precondition

from ..db import Database


@dataclass
class UnsubscribeLocalCommunityInput:
    """Carry one local-subscriber unsubscribe request plus cached DB state."""

    database: Database
    channel_id: int
    channel_mention: str
    _local_subscriber: object | None = field(default=None, init=False, repr=False)
    _local_subscriber_loaded: bool = field(default=False, init=False, repr=False)

    def get_local_subscriber(self) -> object | None:
        """Load and memoize the local-subscriber row for the target channel."""
        if not self._local_subscriber_loaded:
            self._local_subscriber = self.database.get_local_subscriber_by_channel(self.channel_id)
            self._local_subscriber_loaded = True
        return self._local_subscriber


def _missing_message(operation_input: UnsubscribeLocalCommunityInput) -> str:
    """Explain that the channel has no local subscriber state to remove."""
    return f"Channel {operation_input.channel_mention} has no local community subscriber state."


def _reject(
    operation_input: UnsubscribeLocalCommunityInput,
    *,
    reason: str,
    message: str,
    **_: object,
) -> OperationResult:
    """Return one uniform rejection result for Discord adapters."""
    return OperationResult(applied=False, message=message, reason=reason)


def _body(operation_input: UnsubscribeLocalCommunityInput) -> OperationResult:
    """Delete one local-subscriber row without touching remote follow state."""
    local_subscriber = operation_input.get_local_subscriber()
    if local_subscriber is None:
        return OperationResult(
            applied=False,
            message=_missing_message(operation_input),
            reason="local_subscriber_missing_during_delete",
        )

    local_community = operation_input.database.get_local_community_by_id(
        getattr(local_subscriber, "local_community_id")
    )
    display_name = getattr(local_community, "display_name", "local community")
    deleted = operation_input.database.delete_local_subscriber(operation_input.channel_id)
    if not deleted:
        return OperationResult(
            applied=False,
            message=_missing_message(operation_input),
            reason="local_subscriber_missing_during_delete",
        )
    return OperationResult(
        applied=True,
        message=f"Unsubscribed {operation_input.channel_mention} from local community **{display_name}**.",
    )


unsubscribe_local_community_operation = OperationDefinition(
    name="unsubscribe_local_community",
    preconditions=(
        Precondition(
            name="channel_has_local_subscriber",
            message=_missing_message,
            predicate=lambda op: op.get_local_subscriber() is not None,
        ),
    ),
    reject=_reject,
    body=_body,
)

