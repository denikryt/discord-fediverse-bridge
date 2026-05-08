"""Declarative precondition framework for operations.

An OperationDefinition lists named Preconditions in evaluation order. The
runner short-circuits on the first failing predicate, calling the reject
helper with the precondition name and message. If all pass, the body runs.

This separation keeps the ordered validation contract visible and testable
independently of any specific operation's business logic.
"""

from __future__ import annotations

import inspect
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Tuple

from .types import OperationResult


@dataclass(frozen=True)
class Precondition:
    """Named predicate that must hold before an operation body may run."""

    name: str
    message: str | Callable[[Any], str]
    predicate: Callable[[Any], bool | Awaitable[bool]]
    # Some rejections must include DB-derived fields such as the current space id.
    reject_kwargs_factory: Callable[[Any], dict[str, Any] | Awaitable[dict[str, Any]]] | None = None


@dataclass(frozen=True)
class OperationDefinition:
    """Declarative contract for one governance operation."""

    name: str
    preconditions: tuple[Precondition, ...]
    reject: Callable[..., OperationResult | Awaitable[OperationResult]]
    body: Callable[[Any], OperationResult | Awaitable[OperationResult]]


def _raise_async_usage_error(callback_kind: str, owner_name: str) -> None:
    """Fail fast when async callbacks are passed into the sync runner.

    Returning a raw coroutine from the sync path would create a silent and
    confusing contract violation for framework consumers. The sync runner stays
    strict and points callers at the async API explicitly.
    """
    raise TypeError(
        f"{callback_kind} for operation '{owner_name}' is async; "
        "use run_operation_definition_async() instead."
    )


def _reject_awaitable_in_sync_runner(
    callback_kind: str,
    owner_name: str,
    awaitable: Awaitable[Any],
) -> None:
    """Close raw coroutine objects before raising from the sync runner.

    Async functions create coroutine objects immediately on call. When the sync
    runner rejects them, explicitly closing the coroutine avoids noisy runtime
    warnings during tests and consumer execution.
    """
    if inspect.iscoroutine(awaitable):
        awaitable.close()
    _raise_async_usage_error(callback_kind, owner_name)


def run_operation_definition(
    definition: OperationDefinition,
    operation_input: Any,
) -> OperationResult:
    """Evaluate ordered preconditions, then execute the operation body.

    Short-circuits on the first failing precondition, calling the reject
    handler with the precondition name, message, and any extra kwargs.
    If all preconditions pass, executes the body.

    Args:
        definition: OperationDefinition to execute.
        operation_input: Input object passed to preconditions and body.

    Returns:
        OperationResult from either reject handler or body.
    """
    for precondition in definition.preconditions:
        predicate_result = precondition.predicate(operation_input)
        if inspect.isawaitable(predicate_result):
            _reject_awaitable_in_sync_runner(
                "Precondition predicate",
                definition.name,
                predicate_result,
            )

        if not predicate_result:
            message = precondition.message
            if callable(message):
                message = message(operation_input)

            reject_kwargs: dict[str, Any] = {}
            if precondition.reject_kwargs_factory is not None:
                reject_kwargs_result = precondition.reject_kwargs_factory(operation_input)
                if inspect.isawaitable(reject_kwargs_result):
                    _reject_awaitable_in_sync_runner(
                        "reject_kwargs_factory",
                        definition.name,
                        reject_kwargs_result,
                    )
                reject_kwargs = reject_kwargs_result

            reject_result = definition.reject(
                operation_input,
                reason=precondition.name,
                message=message,
                **reject_kwargs,
            )
            if inspect.isawaitable(reject_result):
                _reject_awaitable_in_sync_runner(
                    "Reject handler",
                    definition.name,
                    reject_result,
                )
            return reject_result

    body_result = definition.body(operation_input)
    if inspect.isawaitable(body_result):
        _reject_awaitable_in_sync_runner(
            "Body handler",
            definition.name,
            body_result,
        )
    return body_result


async def run_operation_definition_async(
    definition: OperationDefinition,
    operation_input: Any,
) -> OperationResult:
    """Async operation runner supporting mixed sync/async framework callbacks.

    This runner accepts sync or async precondition predicates, reject kwargs
    factories, reject handlers, and operation bodies. Each hook is awaited only
    when needed, which keeps the contract compatible with existing sync
    operations while allowing async integrations.
    """
    for precondition in definition.preconditions:
        predicate_result = precondition.predicate(operation_input)
        if inspect.isawaitable(predicate_result):
            predicate_result = await predicate_result

        if not predicate_result:
            message = precondition.message
            if callable(message):
                message = message(operation_input)

            reject_kwargs: dict[str, Any] = {}
            if precondition.reject_kwargs_factory is not None:
                reject_kwargs_result = precondition.reject_kwargs_factory(operation_input)
                if inspect.isawaitable(reject_kwargs_result):
                    reject_kwargs_result = await reject_kwargs_result
                reject_kwargs = reject_kwargs_result

            reject_result = definition.reject(
                operation_input,
                reason=precondition.name,
                message=message,
                **reject_kwargs,
            )
            if inspect.isawaitable(reject_result):
                reject_result = await reject_result
            return reject_result

    body_result = definition.body(operation_input)
    if inspect.isawaitable(body_result):
        body_result = await body_result
    return body_result


class Operation(ABC):
    """Base class for declarative operation definitions.

    Subclasses must define:
    - name: operation identifier (str)
    - preconditions: tuple of Precondition instances
    - reject: method handling rejection
    - body: method with operation logic

    Example:
        class CreateSpaceOperation(Operation):
            name = "create_space"
            preconditions = (
                Precondition(
                    name="actor_authority",
                    message="You must have permission.",
                    predicate=lambda inp: inp.has_authority,
                ),
            )

            def reject(self, input, *, reason, message, **kwargs):
                return OperationResult(applied=False, message=message, reason=reason)

            def body(self, input):
                # Business logic
                return OperationResult(applied=True, message="Success")

        op = CreateSpaceOperation()
        result = op.execute(input_data)
    """

    name: str
    preconditions: Tuple[Precondition, ...]

    @abstractmethod
    def reject(
        self,
        operation_input: Any,
        *,
        reason: str,
        message: str,
        **kwargs,
    ) -> OperationResult:
        """Handle rejection when a precondition fails.

        Called by framework with:
        - operation_input: original input
        - reason: name of failed precondition
        - message: message from precondition
        - **kwargs: extra fields from precondition.reject_kwargs_factory

        Args:
            operation_input: The input that failed preconditions.
            reason: Name of the failed precondition.
            message: Message to show to user.
            **kwargs: Extra fields from reject_kwargs_factory.

        Returns:
            OperationResult with applied=False.
        """
        pass

    @abstractmethod
    def body(self, operation_input: Any) -> OperationResult:
        """Execute operation logic when all preconditions pass.

        Args:
            operation_input: The validated input.

        Returns:
            OperationResult with the operation outcome.
        """
        pass

    @property
    def definition(self) -> OperationDefinition:
        """Return the OperationDefinition for this operation.

        This property dynamically constructs an OperationDefinition
        from the class attributes and methods.

        Returns:
            OperationDefinition configured with this operation's contract.
        """
        return OperationDefinition(
            name=self.name,
            preconditions=self.preconditions,
            reject=self.reject,
            body=self.body,
        )

    def execute(self, operation_input: Any) -> OperationResult:
        """Execute this operation with the given input.

        Runs preconditions in order, short-circuiting on first failure.
        If all preconditions pass, executes the body.

        Args:
            operation_input: Input object for the operation.

        Returns:
            OperationResult from reject handler or body.
        """
        return run_operation_definition(self.definition, operation_input)

    async def execute_async(self, operation_input: Any) -> OperationResult:
        """Execute this operation through the async runner.

        This supports mixed sync/async preconditions and handlers while keeping
        the base class API symmetrical with execute().
        """
        return await run_operation_definition_async(self.definition, operation_input)
