"""Command-level tests for guild deployment and registration guards."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from src.commands.guild_guard import (
    GUILD_NOT_ALLOWED_MESSAGE,
    GUILD_ONLY_MESSAGE,
    REGISTRATION_REQUIRED_MESSAGE,
    check_guild_allowed,
    reject_if_guild_not_allowed,
    reject_if_user_not_registered,
)


def test_check_guild_allowed_permits_any_guild_when_allowlist_empty() -> None:
    """Empty allowlists preserve unrestricted guild usage."""
    settings = SimpleNamespace(discord_guild_allowlist=[])

    result = check_guild_allowed(settings=settings, guild_id=123)

    assert result.allowed is True
    assert result.reason is None


def test_check_guild_allowed_rejects_dm_context() -> None:
    """All slash commands are guild-scoped and reject missing guild ids."""
    settings = SimpleNamespace(discord_guild_allowlist=[])

    result = check_guild_allowed(settings=settings, guild_id=None)

    assert result.allowed is False
    assert result.reason == "no_guild"
    assert result.message == GUILD_ONLY_MESSAGE


def test_check_guild_allowed_rejects_non_allowlisted_guild() -> None:
    """Configured allowlists restrict commands to explicit guild ids."""
    settings = SimpleNamespace(discord_guild_allowlist=["123"])

    result = check_guild_allowed(settings=settings, guild_id=456)

    assert result.allowed is False
    assert result.reason == "not_allowlisted"
    assert result.message == GUILD_NOT_ALLOWED_MESSAGE


@pytest.mark.asyncio
async def test_reject_if_guild_not_allowed_sends_ephemeral_message() -> None:
    """The async helper owns the user-visible rejection response."""
    interaction = AsyncMock()
    interaction.guild_id = 456
    interaction.response.send_message = AsyncMock()
    settings = SimpleNamespace(discord_guild_allowlist=["123"])

    rejected = await reject_if_guild_not_allowed(interaction, settings=settings)

    assert rejected is True
    interaction.response.send_message.assert_awaited_once_with(
        GUILD_NOT_ALLOWED_MESSAGE,
        ephemeral=True,
    )


@pytest.mark.asyncio
async def test_reject_if_user_not_registered_sends_registration_guidance() -> None:
    """Create-community uses the guard before any command-specific work."""
    interaction = AsyncMock()
    interaction.user.id = "1234567890"
    interaction.response.send_message = AsyncMock()
    database = Mock()
    database.users.get_user_by_discord_user_id.return_value = None

    rejected = await reject_if_user_not_registered(interaction, database=database)

    assert rejected is True
    database.users.get_user_by_discord_user_id.assert_called_once_with("1234567890")
    interaction.response.send_message.assert_awaited_once_with(
        REGISTRATION_REQUIRED_MESSAGE,
        ephemeral=True,
    )
