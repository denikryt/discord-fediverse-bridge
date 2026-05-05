"""Framework-backed subscribe policy for channel-to-community mappings."""

from __future__ import annotations

from dataclasses import dataclass, field

from discordops import OperationDefinition, OperationResult, Precondition
from sqlalchemy.exc import IntegrityError

from ..db import Database


@dataclass
class SubscribeInput:
    # The operation only needs DB access plus the already-parsed community data.
    database: Database
    channel_id: int
    channel_mention: str
    actor_id: str
    community_name: str | None
    numeric_id: int | None
    _existing_subscription: object | None = field(default=None, init=False, repr=False)
    _existing_subscription_loaded: bool = field(default=False, init=False, repr=False)

    def get_existing_subscription(self) -> object | None:
        # Preconditions and duplicate messaging both need the same lookup, so
        # the input caches it for one command attempt.
        if not self._existing_subscription_loaded:
            self._existing_subscription = self.database.get_subscription_by_channel(self.channel_id)
            self._existing_subscription_loaded = True
        return self._existing_subscription


def _community_label(name: str | None, actor_id: str) -> str:
    # User-facing messages prefer the short name when available and fall back to
    # the actor ID when raw manual input is all we have.
    return name or actor_id


def _duplicate_message(operation_input: SubscribeInput) -> str:
    # The duplicate branch includes the current mapping so moderators can see
    # which community already owns the channel without querying elsewhere.
    existing = operation_input.get_existing_subscription()
    existing_label = _community_label(
        existing.lemmy_community_name if existing is not None else None,
        existing.lemmy_community_actor_id if existing is not None else operation_input.actor_id,
    )
    return (
        f"Channel {operation_input.channel_mention} is already subscribed to **{existing_label}**. "
        "Use `/unsubscribe-channel` first."
    )


def _reject(
    operation_input: SubscribeInput,
    *,
    reason: str,
    message: str,
    **_: object,
) -> OperationResult:
    # Rejections are always non-applied results so command adapters can map
    # them to ephemeral Discord responses uniformly.
    return OperationResult(applied=False, message=message, reason=reason)


def _body(operation_input: SubscribeInput) -> OperationResult:
    # The body owns the actual subscription write and keeps the DB constraint
    # fallback so races still surface as a user-friendly rejection.
    try:
        operation_input.database.create_subscription(
            discord_channel_id=operation_input.channel_id,
            lemmy_community_actor_id=operation_input.actor_id,
            lemmy_community_name=operation_input.community_name,
            lemmy_community_id=operation_input.numeric_id,
        )
    except IntegrityError:
        return OperationResult(
            applied=False,
            message=f"Channel {operation_input.channel_mention} already has a subscription.",
            reason="duplicate_subscription_integrity_error",
        )

    return OperationResult(
        applied=True,
        message=f"Subscribed {operation_input.channel_mention} to **{_community_label(operation_input.community_name, operation_input.actor_id)}**.",
    )


subscribe_operation = OperationDefinition(
    name="subscribe_channel",
    preconditions=(
        Precondition(
            name="channel_not_already_subscribed",
            message=_duplicate_message,
            predicate=lambda operation_input: operation_input.get_existing_subscription() is None,
        ),
    ),
    reject=_reject,
    body=_body,
)
