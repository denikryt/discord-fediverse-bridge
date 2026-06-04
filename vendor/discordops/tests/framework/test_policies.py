"""Behavior tests for body-less DiscordOps policy evaluation."""

from __future__ import annotations

import pytest

from discordops import PolicyDefinition, Precondition, evaluate_policy, evaluate_policy_async


def test_sync_policy_returns_allowed_when_all_preconditions_pass() -> None:
    """All passing conditions produce a metadata-free allowed result."""
    policy = PolicyDefinition(
        name="access",
        preconditions=(Precondition(name="ok", message="unused", predicate=lambda _: True),),
    )

    result = evaluate_policy(policy, object())

    assert result.allowed is True
    assert result.reason is None
    assert result.message is None
    assert result.extra_kwargs is None


def test_sync_policy_returns_first_failure_and_resolved_metadata() -> None:
    """The first failure resolves callable messages and rejection kwargs once."""
    calls: list[str] = []
    policy = PolicyDefinition(
        name="access",
        preconditions=(
            Precondition(
                name="blocked",
                message=lambda value: f"blocked:{value}",
                predicate=lambda _: False,
                reject_kwargs_factory=lambda value: {"value": value},
            ),
            Precondition(name="later", message="later", predicate=lambda _: calls.append("later") or False),
        ),
    )

    result = evaluate_policy(policy, "input")

    assert result.allowed is False
    assert result.reason == "blocked"
    assert result.message == "blocked:input"
    assert result.extra_kwargs == {"value": "input"}
    assert calls == []


@pytest.mark.asyncio
async def test_async_policy_supports_mixed_sync_and_async_callbacks() -> None:
    """Async evaluation awaits only callbacks that return awaitables."""
    async def predicate(_: object) -> bool:
        return False

    async def kwargs_factory(_: object) -> dict[str, object]:
        return {"source": "async"}

    policy = PolicyDefinition(
        name="access",
        preconditions=(
            Precondition(
                name="blocked",
                message="No access",
                predicate=predicate,
                reject_kwargs_factory=kwargs_factory,
            ),
        ),
    )

    result = await evaluate_policy_async(policy, object())

    assert result.allowed is False
    assert result.reason == "blocked"
    assert result.extra_kwargs == {"source": "async"}


def test_sync_policy_rejects_async_predicate_with_policy_api_guidance() -> None:
    """Sync evaluation closes async predicates and points to the async policy API."""
    async def predicate(_: object) -> bool:
        return True

    policy = PolicyDefinition(
        name="access",
        preconditions=(Precondition(name="async", message="unused", predicate=predicate),),
    )

    with pytest.raises(TypeError, match=r"evaluate_policy_async\(\)"):
        evaluate_policy(policy, object())


def test_sync_policy_rejects_async_kwargs_factory() -> None:
    """Sync evaluation rejects awaitable rejection metadata factories."""
    async def kwargs_factory(_: object) -> dict[str, object]:
        return {}

    policy = PolicyDefinition(
        name="access",
        preconditions=(
            Precondition(
                name="blocked",
                message="No access",
                predicate=lambda _: False,
                reject_kwargs_factory=kwargs_factory,
            ),
        ),
    )

    with pytest.raises(TypeError, match=r"evaluate_policy_async\(\)"):
        evaluate_policy(policy, object())
