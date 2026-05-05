"""Test Precondition behavior."""

from dataclasses import dataclass

from discordops.framework import Precondition


def test_precondition_passes_when_predicate_true():
    """Precondition with True predicate passes."""
    pc = Precondition(
        name="test_gate",
        message="Test failed",
        predicate=lambda x: True,
    )
    assert pc.predicate({}) is True


def test_precondition_fails_when_predicate_false():
    """Precondition with False predicate fails."""
    pc = Precondition(
        name="test_gate",
        message="Test failed",
        predicate=lambda x: False,
    )
    assert pc.predicate({}) is False


def test_precondition_message_can_be_static_string():
    """Precondition message can be a static string."""
    pc = Precondition(
        name="test",
        message="Static message",
        predicate=lambda x: True,
    )
    assert pc.message == "Static message"


def test_precondition_message_can_be_callable():
    """Precondition message can be a callable."""

    @dataclass
    class TestInput:
        value: int

    def dynamic_message(inp):
        return f"Value {inp.value} is not valid"

    pc = Precondition(
        name="value_check",
        message=dynamic_message,
        predicate=lambda x: x.value > 0,
    )

    # Verify message is callable
    assert callable(pc.message)
    # Verify it returns correct message when called
    assert pc.message(TestInput(value=-5)) == "Value -5 is not valid"
