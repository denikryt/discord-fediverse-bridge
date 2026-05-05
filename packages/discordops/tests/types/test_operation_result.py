"""Test OperationResult type."""

import pytest
from dataclasses import FrozenInstanceError

from discordops.types import OperationResult


def test_operation_result_creation_with_required_fields():
    """OperationResult can be created with required fields."""
    result = OperationResult(applied=True, message="Success")
    assert result.applied is True
    assert result.message == "Success"


def test_operation_result_creation_with_all_fields():
    """OperationResult can be created with all fields."""
    result = OperationResult(
        applied=False,
        message="Failed",
        reason="validation_error",
        extra_kwargs={"field": "value"},
    )
    assert result.applied is False
    assert result.message == "Failed"
    assert result.reason == "validation_error"
    assert result.extra_kwargs == {"field": "value"}


def test_operation_result_is_frozen():
    """OperationResult is frozen (immutable)."""
    result = OperationResult(applied=True, message="Success")
    with pytest.raises(FrozenInstanceError):
        result.applied = False


def test_operation_result_reason_is_optional():
    """OperationResult reason field is optional."""
    result = OperationResult(applied=True, message="Success")
    assert result.reason is None


def test_operation_result_extra_kwargs_is_optional():
    """OperationResult extra_kwargs field is optional."""
    result = OperationResult(applied=True, message="Success")
    assert result.extra_kwargs is None


def test_operation_result_with_reason_only():
    """OperationResult can have reason without extra_kwargs."""
    result = OperationResult(
        applied=False,
        message="Not allowed",
        reason="actor_authority_missing",
    )
    assert result.reason == "actor_authority_missing"
    assert result.extra_kwargs is None


def test_operation_result_with_extra_kwargs():
    """OperationResult can have extra_kwargs with multiple fields."""
    result = OperationResult(
        applied=False,
        message="Failed",
        reason="validation_error",
        extra_kwargs={"space_id": "space-123", "user_id": "user-456"},
    )
    assert result.extra_kwargs["space_id"] == "space-123"
    assert result.extra_kwargs["user_id"] == "user-456"


def test_operation_result_equality():
    """OperationResult instances with same values are equal."""
    result1 = OperationResult(applied=True, message="Success")
    result2 = OperationResult(applied=True, message="Success")
    assert result1 == result2


def test_operation_result_inequality():
    """OperationResult instances with different values are not equal."""
    result1 = OperationResult(applied=True, message="Success")
    result2 = OperationResult(applied=False, message="Failure")
    assert result1 != result2
