"""Test Operation execution and dispatch."""

import pytest
from dataclasses import dataclass

from discordops.framework import (
    Precondition,
    OperationDefinition,
    run_operation_definition,
    run_operation_definition_async,
)
from discordops.types import OperationResult


def test_operation_executes_body_when_preconditions_pass():
    """OperationDefinition executes body when all preconditions pass."""
    pc = Precondition(
        name="always_pass",
        message="Should not appear",
        predicate=lambda x: True,
    )

    def reject_handler(ctx, reason, message, **kwargs):
        return OperationResult(applied=False, message=message, reason=reason)

    def body_handler(ctx):
        return OperationResult(applied=True, message="Success")

    op = OperationDefinition(
        name="test_op",
        preconditions=(pc,),
        reject=reject_handler,
        body=body_handler,
    )

    result = run_operation_definition(op, {})
    assert result.applied is True
    assert result.message == "Success"


def test_operation_calls_reject_when_precondition_fails():
    """OperationDefinition calls reject when precondition fails."""
    pc = Precondition(
        name="always_fail",
        message="Precondition failed",
        predicate=lambda x: False,
    )

    def reject_handler(ctx, reason, message, **kwargs):
        return OperationResult(applied=False, message=message, reason=reason)

    def body_handler(ctx):
        return OperationResult(applied=True, message="Should not execute")

    op = OperationDefinition(
        name="test_op",
        preconditions=(pc,),
        reject=reject_handler,
        body=body_handler,
    )

    result = run_operation_definition(op, {})
    assert result.applied is False
    assert result.message == "Precondition failed"
    assert result.reason == "always_fail"


def test_operation_short_circuits_on_first_failing_precondition():
    """OperationDefinition short-circuits on first failing precondition."""
    rejected_reasons = []

    def reject_handler(ctx, reason, message, **kwargs):
        rejected_reasons.append(reason)
        return OperationResult(applied=False, message=message, reason=reason)

    def body_handler(ctx):
        return OperationResult(applied=True, message="Should not execute")

    op = OperationDefinition(
        name="test_op",
        preconditions=(
            Precondition(
                name="first_fails",
                message="First failed",
                predicate=lambda x: False,
            ),
            Precondition(
                name="second_fails",
                message="Second failed",
                predicate=lambda x: False,
            ),
        ),
        reject=reject_handler,
        body=body_handler,
    )

    result = run_operation_definition(op, {})
    assert result.reason == "first_fails"
    assert rejected_reasons == ["first_fails"]  # Only first rejection called


def test_operation_with_reject_kwargs_factory():
    """OperationDefinition passes reject_kwargs_factory results to reject handler."""

    @dataclass
    class TestInput:
        value: int

    def reject_handler(ctx, reason, message, space_id=None, **kwargs):
        return OperationResult(
            applied=False,
            message=message,
            reason=reason,
            extra_kwargs={"space_id": space_id},
        )

    def reject_kwargs_factory(ctx):
        return {"space_id": f"space-{ctx.value}"}

    op = OperationDefinition(
        name="test_op",
        preconditions=(
            Precondition(
                name="value_positive",
                message="Value must be positive",
                predicate=lambda x: x.value > 0,
                reject_kwargs_factory=reject_kwargs_factory,
            ),
        ),
        reject=reject_handler,
        body=lambda x: OperationResult(applied=True, message="OK"),
    )

    result = run_operation_definition(op, TestInput(value=-5))
    assert result.applied is False
    assert result.extra_kwargs["space_id"] == "space--5"


def test_operation_message_evaluated_at_rejection_time():
    """Precondition message is evaluated only when precondition fails."""

    @dataclass
    class TestInput:
        value: int

    call_count = [0]

    def dynamic_message(inp):
        call_count[0] += 1
        return f"Value {inp.value} is invalid"

    def reject_handler(ctx, reason, message, **kwargs):
        return OperationResult(applied=False, message=message, reason=reason)

    op = OperationDefinition(
        name="test_op",
        preconditions=(
            Precondition(
                name="check",
                message=dynamic_message,
                predicate=lambda x: x.value > 0,
            ),
        ),
        reject=reject_handler,
        body=lambda x: OperationResult(applied=True, message="OK"),
    )

    # Success case — message should not be called
    call_count[0] = 0
    result = run_operation_definition(op, TestInput(value=5))
    assert result.applied is True
    assert call_count[0] == 0

    # Failure case — message should be called once
    call_count[0] = 0
    result = run_operation_definition(op, TestInput(value=-1))
    assert result.applied is False
    assert call_count[0] == 1


@pytest.mark.asyncio
async def test_async_operation_executes_async_body_when_preconditions_pass():
    """Async runner awaits async body after sync preconditions pass."""
    pc = Precondition(
        name="always_pass",
        message="Should not appear",
        predicate=lambda x: True,
    )

    def reject_handler(ctx, reason, message, **kwargs):
        return OperationResult(applied=False, message=message, reason=reason)

    async def body_handler(ctx):
        return OperationResult(applied=True, message="Async success")

    op = OperationDefinition(
        name="test_async_op",
        preconditions=(pc,),
        reject=reject_handler,
        body=body_handler,
    )

    result = await run_operation_definition_async(op, {})
    assert result.applied is True
    assert result.message == "Async success"


@pytest.mark.asyncio
async def test_async_operation_awaits_async_precondition_and_reject_kwargs():
    """Async runner awaits async preconditions and reject kwargs factories."""

    @dataclass
    class TestInput:
        value: int

    async def async_predicate(inp):
        return inp.value > 0

    async def async_reject_kwargs(inp):
        return {"space_id": f"space-{inp.value}"}

    def reject_handler(ctx, reason, message, space_id=None, **kwargs):
        return OperationResult(
            applied=False,
            message=message,
            reason=reason,
            extra_kwargs={"space_id": space_id},
        )

    op = OperationDefinition(
        name="test_async_reject",
        preconditions=(
            Precondition(
                name="value_positive",
                message=lambda inp: f"Value {inp.value} must be positive",
                predicate=async_predicate,
                reject_kwargs_factory=async_reject_kwargs,
            ),
        ),
        reject=reject_handler,
        body=lambda x: OperationResult(applied=True, message="OK"),
    )

    result = await run_operation_definition_async(op, TestInput(value=-3))
    assert result.applied is False
    assert result.reason == "value_positive"
    assert result.extra_kwargs == {"space_id": "space--3"}


@pytest.mark.asyncio
async def test_async_operation_supports_async_reject_handler():
    """Async runner awaits async reject handlers on failed preconditions."""
    pc = Precondition(
        name="always_fail",
        message="No access",
        predicate=lambda x: False,
    )

    async def reject_handler(ctx, reason, message, **kwargs):
        return OperationResult(applied=False, message=message, reason=reason)

    op = OperationDefinition(
        name="test_async_reject_handler",
        preconditions=(pc,),
        reject=reject_handler,
        body=lambda x: OperationResult(applied=True, message="Should not execute"),
    )

    result = await run_operation_definition_async(op, {})
    assert result.applied is False
    assert result.reason == "always_fail"


def test_sync_runner_rejects_async_callbacks():
    """Sync runner rejects async callbacks to avoid returning raw coroutines."""

    async def async_predicate(ctx):
        return True

    op = OperationDefinition(
        name="test_sync_guard",
        preconditions=(
            Precondition(
                name="async_precondition",
                message="Should not appear",
                predicate=async_predicate,
            ),
        ),
        reject=lambda ctx, reason, message, **kwargs: OperationResult(
            applied=False,
            message=message,
            reason=reason,
        ),
        body=lambda ctx: OperationResult(applied=True, message="OK"),
    )

    with pytest.raises(TypeError, match="async"):
        run_operation_definition(op, {})
