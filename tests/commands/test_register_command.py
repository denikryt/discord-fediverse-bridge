from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.commands import register


@pytest.mark.asyncio
async def test_register_command_returns_ephemeral_registration_link(
    command_tree, interaction
) -> None:
    """The slash command should only hand the user the web registration URL."""
    settings = SimpleNamespace(
        normalized_public_bridge_base_url="https://discord-bridge.example.com",
        discord_guild_allowlist=[],
    )

    register.register(command_tree, settings)

    command = command_tree.commands["register"]
    await command.callback(interaction)

    interaction.response.defer.assert_awaited_once_with(
        ephemeral=True,
        thinking=False,
    )
    interaction.followup.send.assert_awaited_once_with(
        "Register your ActivityPub identity here:\nhttps://discord-bridge.example.com/register",
        ephemeral=True,
    )
    interaction.response.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_register_command_rejects_dm_context(command_tree, interaction) -> None:
    """Register is now a guild-only slash command, not a DM onboarding flow."""
    settings = SimpleNamespace(
        normalized_public_bridge_base_url="https://discord-bridge.example.com",
        discord_guild_allowlist=[],
    )
    interaction.guild_id = None

    register.register(command_tree, settings)

    command = command_tree.commands["register"]
    await command.callback(interaction)

    interaction.response.send_message.assert_awaited_once_with(
        "This command can only be used inside an allowed Discord server.",
        ephemeral=True,
    )
    interaction.response.defer.assert_not_awaited()
    interaction.followup.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_register_command_rejects_non_allowlisted_guild(command_tree, interaction) -> None:
    """Register respects deployment allowlists before returning the web URL."""
    settings = SimpleNamespace(
        normalized_public_bridge_base_url="https://discord-bridge.example.com",
        discord_guild_allowlist=["111"],
    )
    interaction.guild_id = 99999

    register.register(command_tree, settings)

    command = command_tree.commands["register"]
    await command.callback(interaction)

    interaction.response.send_message.assert_awaited_once_with(
        "This Discord server is not allowed to use this bridge bot.",
        ephemeral=True,
    )
    interaction.response.defer.assert_not_awaited()
    interaction.followup.send.assert_not_awaited()
