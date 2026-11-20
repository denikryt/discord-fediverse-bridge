"""Test public API exports."""


def test_public_api_imports():
    """All public API items are importable from discordops."""
    from discordops import (
        OperationDefinition,
        PolicyDefinition,
        Precondition,
        Operation,
        evaluate_policy,
        evaluate_policy_async,
        run_operation_definition,
        run_operation_definition_async,
        OperationResult,
        PolicyResult,
        gates,
    )

    assert OperationDefinition is not None
    assert PolicyDefinition is not None
    assert Precondition is not None
    assert Operation is not None
    assert evaluate_policy is not None
    assert evaluate_policy_async is not None
    assert run_operation_definition is not None
    assert run_operation_definition_async is not None
    assert OperationResult is not None
    assert PolicyResult is not None
    assert gates is not None


def test_gates_module_exports():
    """Gates module exports all gate functions."""
    from discordops import gates

    assert hasattr(gates, "has_actor_authority")
    assert hasattr(gates, "require_guild_context")
    assert hasattr(gates, "require_actor_authority")
