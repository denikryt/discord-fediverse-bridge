"""Test Operation base class."""

import pytest
from dataclasses import dataclass

from discordops.framework import Operation, Precondition
from discordops.types import OperationResult


@dataclass
class OperationInput:
    value: int


class TestOperation(Operation):
    """Test operation for verification."""

    name = "test"
    preconditions = (
        Precondition(
            name="value_positive",
            message="Value must be positive",
            predicate=lambda inp: inp.value > 0,
        ),
    )

    def reject(self, input, *, reason, message, **kwargs):
        return OperationResult(applied=False, message=message, reason=reason)

    def body(self, input):
        return OperationResult(applied=True, message=f"Value is {input.value}")


def test_operation_base_class_has_definition_property():
    """Operation base class has definition property."""
    op = TestOperation()
    assert op.definition is not None
    assert op.definition.name == "test"


def test_operation_definition_has_correct_preconditions():
    """Operation.definition includes all preconditions."""
    op = TestOperation()
    assert len(op.definition.preconditions) == 1
    assert op.definition.preconditions[0].name == "value_positive"


def test_operation_definition_has_reject_and_body():
    """Operation.definition includes reject and body methods."""
    op = TestOperation()
    assert callable(op.definition.reject)
    assert callable(op.definition.body)


def test_operation_execute_method_success():
    """Operation.execute() works end-to-end on success."""
    op = TestOperation()
    result = op.execute(OperationInput(value=5))
    assert result.applied is True
    assert result.message == "Value is 5"


def test_operation_execute_method_failure():
    """Operation.execute() works end-to-end on failure."""
    op = TestOperation()
    result = op.execute(OperationInput(value=-1))
    assert result.applied is False
    assert result.reason == "value_positive"
    assert result.message == "Value must be positive"


def test_operation_execute_with_multiple_preconditions():
    """Operation.execute() evaluates multiple preconditions in order."""

    @dataclass
    class MultiCheckInput:
        value: int
        is_admin: bool

    class MultiCheckOperation(Operation):
        name = "multi_check"
        preconditions = (
            Precondition(
                name="admin_only",
                message="Must be admin",
                predicate=lambda inp: inp.is_admin,
            ),
            Precondition(
                name="positive_value",
                message="Value must be positive",
                predicate=lambda inp: inp.value > 0,
            ),
        )

        def reject(self, input, *, reason, message, **kwargs):
            return OperationResult(applied=False, message=message, reason=reason)

        def body(self, input):
            return OperationResult(applied=True, message="OK")

    op = MultiCheckOperation()

    # Both pass
    result = op.execute(MultiCheckInput(value=5, is_admin=True))
    assert result.applied is True

    # First fails
    result = op.execute(MultiCheckInput(value=5, is_admin=False))
    assert result.applied is False
    assert result.reason == "admin_only"

    # First passes, second fails
    result = op.execute(MultiCheckInput(value=-1, is_admin=True))
    assert result.applied is False
    assert result.reason == "positive_value"


@pytest.mark.asyncio
async def test_operation_execute_async_supports_async_body_and_reject():
    """Operation.execute_async works end-to-end with async hooks."""

    @dataclass
    class AsyncInput:
        value: int

    class AsyncOperation(Operation):
        name = "async_test"
        preconditions = (
            Precondition(
                name="value_positive",
                message="Value must be positive",
                predicate=lambda inp: inp.value > 0,
            ),
        )

        async def reject(self, input, *, reason, message, **kwargs):
            return OperationResult(applied=False, message=message, reason=reason)

        async def body(self, input):
            return OperationResult(applied=True, message=f"Async value is {input.value}")

    op = AsyncOperation()

    success = await op.execute_async(AsyncInput(value=7))
    assert success.applied is True
    assert success.message == "Async value is 7"

    failure = await op.execute_async(AsyncInput(value=-1))
    assert failure.applied is False
    assert failure.reason == "value_positive"
