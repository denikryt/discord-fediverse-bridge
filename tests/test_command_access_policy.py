"""Behavior tests for reusable Discord command-access policies."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from discordops import evaluate_policy

from src.command_access import (
    GUILD_COMMAND_ACCESS,
    REGISTERED_GUILD_COMMAND_ACCESS,
    CommandAccessInput,
)
from src.operations.common_preconditions import DISCORD_USER_REGISTERED, REGISTRATION_REQUIRED_MESSAGE
from src.operations.subscribe import subscribe_operation
from src.operations.subscribe_local_community import subscribe_local_community_operation


def _input(*, guild_id: int | None, allowlist: list[str], database: object | None = None) -> CommandAccessInput:
    """Build one policy input with lightweight settings and user identity."""
    return CommandAccessInput(
        settings=SimpleNamespace(discord_guild_allowlist=allowlist),
        database=database,
        discord_guild_id=guild_id,
        discord_user_id="123",
    )


def test_missing_guild_short_circuits_before_registration_lookup() -> None:
    """DM access fails with the stable guild reason before repository work."""
    database = MagicMock()
    result = evaluate_policy(REGISTERED_GUILD_COMMAND_ACCESS, _input(guild_id=None, allowlist=[], database=database))
    assert result.reason == "no_guild"
    database.users.get_user_by_discord_user_id.assert_not_called()


def test_non_allowlisted_guild_short_circuits_before_registration_lookup() -> None:
    """Deployment denial occurs before registration state is inspected."""
    database = MagicMock()
    result = evaluate_policy(REGISTERED_GUILD_COMMAND_ACCESS, _input(guild_id=2, allowlist=["1"], database=database))
    assert result.reason == "not_allowlisted"
    database.users.get_user_by_discord_user_id.assert_not_called()


def test_empty_allowlist_allows_any_non_null_guild_without_database() -> None:
    """An empty allowlist preserves unrestricted deployment compatibility."""
    result = evaluate_policy(GUILD_COMMAND_ACCESS, _input(guild_id=2, allowlist=[]))
    assert result.allowed is True


def test_matching_allowlist_permits_configured_guild() -> None:
    """Configured guild membership permits command ingress."""
    result = evaluate_policy(GUILD_COMMAND_ACCESS, _input(guild_id=2, allowlist=["2"]))
    assert result.allowed is True


def test_registered_policy_rejects_unknown_user_with_existing_message() -> None:
    """Registration denial preserves the existing reason and user guidance."""
    database = MagicMock()
    database.users.get_user_by_discord_user_id.return_value = None
    result = evaluate_policy(REGISTERED_GUILD_COMMAND_ACCESS, _input(guild_id=2, allowlist=[], database=database))
    assert result.reason == "discord_user_is_registered"
    assert result.message == REGISTRATION_REQUIRED_MESSAGE


def test_registered_policy_permits_known_user_and_memoizes_lookup() -> None:
    """One command input performs at most one repository lookup."""
    database = MagicMock()
    database.users.get_user_by_discord_user_id.return_value = SimpleNamespace(id=1)
    value = _input(guild_id=2, allowlist=[], database=database)
    assert value.get_bridge_user() is value.get_bridge_user()
    result = evaluate_policy(REGISTERED_GUILD_COMMAND_ACCESS, value)
    assert result.allowed is True
    database.users.get_user_by_discord_user_id.assert_called_once_with("123")


def test_subscribe_operations_share_exact_registration_precondition() -> None:
    """Remote, local, and ingress policy reuse one immutable condition object."""
    assert subscribe_operation.preconditions[0] is DISCORD_USER_REGISTERED
    assert subscribe_local_community_operation.preconditions[0] is DISCORD_USER_REGISTERED
    assert REGISTERED_GUILD_COMMAND_ACCESS.preconditions[-1] is DISCORD_USER_REGISTERED
