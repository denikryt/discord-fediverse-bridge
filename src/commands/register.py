"""Discord slash command that starts the web registration flow."""

from __future__ import annotations

import discord
from discord import app_commands

from ..config import Settings
from .guild_guard import GUILD_COMMAND_ACCESS, reject_if_command_access_denied


def register(tree: app_commands.CommandTree, settings: Settings) -> None:
    """Register the `/register` slash command for user self-service onboarding."""

    # The bot does not create accounts directly. It only hands users the
    # bridge-owned web URL where Discord OAuth and username validation happen.
    @tree.command(name="register", description="Get a link to register your ActivityPub identity")
    async def register_identity(interaction: discord.Interaction) -> None:
        if await reject_if_command_access_denied(interaction, definition=GUILD_COMMAND_ACCESS, settings=settings):
            return
        await interaction.response.defer(ephemeral=True, thinking=False)

        registration_url = f"{settings.normalized_public_bridge_base_url}/register"
        await interaction.followup.send(
            f"Register your ActivityPub identity here:\n{registration_url}",
            ephemeral=True,
        )
