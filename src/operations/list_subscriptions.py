"""Framework-backed list policy for presenting active subscriptions."""

from __future__ import annotations

from dataclasses import dataclass, field

from discordops import OperationDefinition, OperationResult, Precondition

from ..db import Database


@dataclass
class ListSubscriptionsInput:
    """Carry the guild context needed to scope the subscription list."""

    database: Database
    # None means no guild context — falls back to showing all subscriptions
    # (e.g. when called outside a guild or from legacy code paths).
    guild_id: int | None = None
    _remote_subscriptions: list[object] | None = field(default=None, init=False, repr=False)
    _local_subscribers: list[object] | None = field(default=None, init=False, repr=False)

    def get_remote_subscriptions(self) -> list[object]:
        """Load and memoize remote subscriptions scoped to the guild."""
        if self._remote_subscriptions is None:
            if self.guild_id is not None:
                self._remote_subscriptions = self.database.get_subscriptions_by_guild(self.guild_id)
            else:
                self._remote_subscriptions = self.database.get_all_subscriptions()
        return self._remote_subscriptions

    def get_local_subscribers(self) -> list[object]:
        """Load and memoize local subscribers scoped to the guild."""
        if self._local_subscribers is None:
            if self.guild_id is not None:
                self._local_subscribers = self.database.list_local_subscribers_by_guild(self.guild_id)
            else:
                self._local_subscribers = []
        return self._local_subscribers


def _load_remote_subscriptions(operation_input: ListSubscriptionsInput) -> list[object]:
    """Load the cached remote subscription snapshot for one operation run."""
    return operation_input.get_remote_subscriptions()


def _load_local_subscribers(operation_input: ListSubscriptionsInput) -> list[object]:
    """Load the cached local-subscriber snapshot for one operation run."""
    return operation_input.get_local_subscribers()


def _reject(
    operation_input: ListSubscriptionsInput,
    *,
    reason: str,
    message: str,
    **_: object,
) -> OperationResult:
    return OperationResult(applied=False, message=message, reason=reason)


def _body(operation_input: ListSubscriptionsInput) -> OperationResult:
    # The operation prepares structured payload data, while the command adapter
    # remains responsible for the actual Discord embed construction.
    remote_subscriptions = _load_remote_subscriptions(operation_input)
    local_subscribers = _load_local_subscribers(operation_input)
    return OperationResult(
        applied=True,
        message="Loaded active subscriptions.",
        extra_kwargs={
            "remote_subscriptions": remote_subscriptions,
            "local_subscribers": local_subscribers,
        },
    )


list_subscriptions_operation = OperationDefinition(
    name="list_subscriptions",
    preconditions=(
        Precondition(
            name="subscriptions_exist",
            message="No active subscriptions.",
            predicate=lambda operation_input: bool(_load_remote_subscriptions(operation_input))
            or bool(_load_local_subscribers(operation_input)),
        ),
    ),
    reject=_reject,
    body=_body,
)
