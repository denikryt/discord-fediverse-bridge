"""Discord adapter tests for command-access policy presentation."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.commands.guild_guard import (
    GUILD_COMMAND_ACCESS,
    REGISTERED_GUILD_COMMAND_ACCESS,
    command_access_allows_autocomplete,
    reject_if_command_access_denied,
)


def _interaction(*, guild_id: int | None = 123) -> SimpleNamespace:
    """Build a lightweight Discord interaction double for policy presentation."""
    response = SimpleNamespace(send_message=AsyncMock(), is_done=MagicMock(return_value=False))
    return SimpleNamespace(
        guild_id=guild_id,
        user=SimpleNamespace(id=999),
        response=response,
        followup=SimpleNamespace(send=AsyncMock()),
    )


@pytest.mark.asyncio
async def test_denied_command_sends_initial_ephemeral_response() -> None:
    """Unacknowledged commands receive the policy message privately."""
    interaction = _interaction(guild_id=None)
    stopped = await reject_if_command_access_denied(
        interaction,
        definition=GUILD_COMMAND_ACCESS,
        settings=SimpleNamespace(discord_guild_allowlist=[]),
    )
    assert stopped is True
    interaction.response.send_message.assert_awaited_once_with(
        "This command can only be used inside an allowed Discord server.",
        ephemeral=True,
    )


@pytest.mark.asyncio
async def test_denied_acknowledged_command_uses_ephemeral_followup() -> None:
    """Already acknowledged interactions avoid a second initial response."""
    interaction = _interaction(guild_id=5)
    interaction.response.is_done.return_value = True
    stopped = await reject_if_command_access_denied(
        interaction,
        definition=GUILD_COMMAND_ACCESS,
        settings=SimpleNamespace(discord_guild_allowlist=["4"]),
    )
    assert stopped is True
    interaction.followup.send.assert_awaited_once_with(
        "This Discord server is not allowed to use this bridge bot.",
        ephemeral=True,
    )


@pytest.mark.asyncio
async def test_allowed_command_returns_false_without_response() -> None:
    """Allowed ingress leaves response ownership with the command handler."""
    interaction = _interaction(guild_id=5)
    stopped = await reject_if_command_access_denied(
        interaction,
        definition=GUILD_COMMAND_ACCESS,
        settings=SimpleNamespace(discord_guild_allowlist=[]),
    )
    assert stopped is False
    interaction.response.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_autocomplete_denial_is_quiet_and_skips_registration_lookup() -> None:
    """Autocomplete returns a decision without sending interaction responses."""
    interaction = _interaction(guild_id=None)
    database = MagicMock()
    allowed = await command_access_allows_autocomplete(
        interaction,
        definition=REGISTERED_GUILD_COMMAND_ACCESS,
        settings=SimpleNamespace(discord_guild_allowlist=[]),
        database=database,
    )
    assert allowed is False
    database.users.get_user_by_discord_user_id.assert_not_called()
    interaction.response.send_message.assert_not_awaited()
