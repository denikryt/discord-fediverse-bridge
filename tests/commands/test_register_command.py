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
        normalized_public_bridge_base_url="https://discord-bridge.example.com"
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
