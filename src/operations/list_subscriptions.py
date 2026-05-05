"""Framework-backed list policy for presenting active subscriptions."""

from __future__ import annotations

from dataclasses import dataclass, field

from discordops import OperationDefinition, OperationResult, Precondition

from ..db import Database


@dataclass
class ListSubscriptionsInput:
    # Listing only depends on the subscription repository.
    database: Database
    _subscriptions: list[object] | None = field(default=None, init=False, repr=False)

    def get_subscriptions(self) -> list[object]:
        # The empty-state check and success payload should see one cached
        # subscription snapshot for a single command invocation.
        if self._subscriptions is None:
            self._subscriptions = self.database.get_all_subscriptions()
        return self._subscriptions


def _load_subscriptions(operation_input: ListSubscriptionsInput) -> list[object]:
    # Both the precondition and success payload must see the same ordering
    # contract from the database repository.
    return operation_input.get_subscriptions()


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
    subscriptions = _load_subscriptions(operation_input)
    return OperationResult(
        applied=True,
        message="Loaded active subscriptions.",
        extra_kwargs={"subscriptions": subscriptions},
    )


list_subscriptions_operation = OperationDefinition(
    name="list_subscriptions",
    preconditions=(
        Precondition(
            name="subscriptions_exist",
            message="No active subscriptions.",
            predicate=lambda operation_input: bool(_load_subscriptions(operation_input)),
        ),
    ),
    reject=_reject,
    body=_body,
)
