"""Declarative precondition framework for operations and body-less policies.

Operations and policies share the same ordered precondition evaluator. Policies
return neutral access results, while operations map failures through an
application-owned reject callback and execute a body only after all conditions
pass.
"""

from __future__ import annotations

import inspect
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Tuple

from .types import OperationResult, PolicyResult


@dataclass(frozen=True)
class Precondition:
    """Declare one named predicate that must hold before work may proceed."""

    name: str
    message: str | Callable[[Any], str]
    predicate: Callable[[Any], bool | Awaitable[bool]]
    reject_kwargs_factory: Callable[[Any], dict[str, Any] | Awaitable[dict[str, Any]]] | None = None


@dataclass(frozen=True)
class OperationDefinition:
    """Declare ordered preconditions, rejection mapping, and an operation body."""

    name: str
    preconditions: tuple[Precondition, ...]
    reject: Callable[..., OperationResult | Awaitable[OperationResult]]
    body: Callable[[Any], OperationResult | Awaitable[OperationResult]]


@dataclass(frozen=True)
class PolicyDefinition:
    """Declare ordered preconditions without an operation body or reject hook."""

    name: str
    preconditions: tuple[Precondition, ...]


@dataclass(frozen=True)
class _PreconditionEvaluation:
    """Carry the neutral result of evaluating an ordered precondition tuple."""

    allowed: bool
    reason: str | None = None
    message: str | None = None
    extra_kwargs: dict[str, Any] | None = None


def _raise_async_usage_error(callback_kind: str, owner_kind: str, owner_name: str, async_api: str) -> None:
    """Fail fast when an awaitable callback is used through a sync runner."""
    raise TypeError(f"{callback_kind} for {owner_kind} '{owner_name}' is async; use {async_api} instead.")


def _reject_awaitable_in_sync_runner(
    callback_kind: str,
    owner_kind: str,
    owner_name: str,
    awaitable: Awaitable[Any],
    async_api: str,
) -> None:
    """Close raw coroutines before reporting invalid sync-runner usage."""
    if inspect.iscoroutine(awaitable):
        awaitable.close()
    _raise_async_usage_error(callback_kind, owner_kind, owner_name, async_api)


def _evaluate_preconditions(
    owner_kind: str,
    owner_name: str,
    preconditions: tuple[Precondition, ...],
    operation_input: Any,
    *,
    async_api: str,
) -> _PreconditionEvaluation:
    """Evaluate sync preconditions in order and return the first rejection."""
    for precondition in preconditions:
        predicate_result = precondition.predicate(operation_input)
        if inspect.isawaitable(predicate_result):
            _reject_awaitable_in_sync_runner(
                "Precondition predicate", owner_kind, owner_name, predicate_result, async_api
            )

        if not predicate_result:
            message = precondition.message(operation_input) if callable(precondition.message) else precondition.message
            reject_kwargs: dict[str, Any] | None = None
            if precondition.reject_kwargs_factory is not None:
                reject_kwargs_result = precondition.reject_kwargs_factory(operation_input)
                if inspect.isawaitable(reject_kwargs_result):
                    _reject_awaitable_in_sync_runner(
                        "reject_kwargs_factory", owner_kind, owner_name, reject_kwargs_result, async_api
                    )
                reject_kwargs = reject_kwargs_result
            return _PreconditionEvaluation(
                allowed=False,
                reason=precondition.name,
                message=message,
                extra_kwargs=reject_kwargs,
            )

    return _PreconditionEvaluation(allowed=True)


async def _evaluate_preconditions_async(
    preconditions: tuple[Precondition, ...],
    operation_input: Any,
) -> _PreconditionEvaluation:
    """Evaluate mixed sync/async preconditions in order and short-circuit."""
    for precondition in preconditions:
        predicate_result = precondition.predicate(operation_input)
        if inspect.isawaitable(predicate_result):
            predicate_result = await predicate_result

        if not predicate_result:
            message = precondition.message(operation_input) if callable(precondition.message) else precondition.message
            reject_kwargs: dict[str, Any] | None = None
            if precondition.reject_kwargs_factory is not None:
                reject_kwargs_result = precondition.reject_kwargs_factory(operation_input)
                if inspect.isawaitable(reject_kwargs_result):
                    reject_kwargs_result = await reject_kwargs_result
                reject_kwargs = reject_kwargs_result
            return _PreconditionEvaluation(
                allowed=False,
                reason=precondition.name,
                message=message,
                extra_kwargs=reject_kwargs,
            )

    return _PreconditionEvaluation(allowed=True)


def evaluate_policy(definition: PolicyDefinition, policy_input: Any) -> PolicyResult:
    """Evaluate a body-less policy synchronously using ordered preconditions."""
    evaluation = _evaluate_preconditions(
        "policy",
        definition.name,
        definition.preconditions,
        policy_input,
        async_api="evaluate_policy_async()",
    )
    return PolicyResult(
        allowed=evaluation.allowed,
        reason=evaluation.reason,
        message=evaluation.message,
        extra_kwargs=evaluation.extra_kwargs,
    )


async def evaluate_policy_async(definition: PolicyDefinition, policy_input: Any) -> PolicyResult:
    """Evaluate a body-less policy with mixed sync and async callbacks."""
    evaluation = await _evaluate_preconditions_async(definition.preconditions, policy_input)
    return PolicyResult(
        allowed=evaluation.allowed,
        reason=evaluation.reason,
        message=evaluation.message,
        extra_kwargs=evaluation.extra_kwargs,
    )


def run_operation_definition(definition: OperationDefinition, operation_input: Any) -> OperationResult:
    """Evaluate sync preconditions, then reject or execute the operation body."""
    evaluation = _evaluate_preconditions(
        "operation",
        definition.name,
        definition.preconditions,
        operation_input,
        async_api="run_operation_definition_async()",
    )
    if not evaluation.allowed:
        reject_result = definition.reject(
            operation_input,
            reason=evaluation.reason,
            message=evaluation.message,
            **(evaluation.extra_kwargs or {}),
        )
        if inspect.isawaitable(reject_result):
            _reject_awaitable_in_sync_runner(
                "Reject handler", "operation", definition.name, reject_result, "run_operation_definition_async()"
            )
        return reject_result

    body_result = definition.body(operation_input)
    if inspect.isawaitable(body_result):
        _reject_awaitable_in_sync_runner(
            "Body handler", "operation", definition.name, body_result, "run_operation_definition_async()"
        )
    return body_result


async def run_operation_definition_async(definition: OperationDefinition, operation_input: Any) -> OperationResult:
    """Evaluate mixed sync/async preconditions, then reject or run the body."""
    evaluation = await _evaluate_preconditions_async(definition.preconditions, operation_input)
    if not evaluation.allowed:
        reject_result = definition.reject(
            operation_input,
            reason=evaluation.reason,
            message=evaluation.message,
            **(evaluation.extra_kwargs or {}),
        )
        if inspect.isawaitable(reject_result):
            reject_result = await reject_result
        return reject_result

    body_result = definition.body(operation_input)
    if inspect.isawaitable(body_result):
        body_result = await body_result
    return body_result


class Operation(ABC):
    """Base class exposing class-oriented operation definitions."""

    name: str
    preconditions: Tuple[Precondition, ...]

    @abstractmethod
    def reject(self, operation_input: Any, *, reason: str, message: str, **kwargs: Any) -> OperationResult:
        """Map one failed precondition to an application operation result."""
        raise NotImplementedError

    @abstractmethod
    def body(self, operation_input: Any) -> OperationResult:
        """Execute the operation after every declared precondition passes."""
        raise NotImplementedError

    @property
    def definition(self) -> OperationDefinition:
        """Build the immutable definition represented by this operation object."""
        return OperationDefinition(
            name=self.name,
            preconditions=self.preconditions,
            reject=self.reject,
            body=self.body,
        )

    def execute(self, operation_input: Any) -> OperationResult:
        """Execute this operation through the shared synchronous runner."""
        return run_operation_definition(self.definition, operation_input)

    async def execute_async(self, operation_input: Any) -> OperationResult:
        """Execute this operation through the shared asynchronous runner."""
        return await run_operation_definition_async(self.definition, operation_input)
