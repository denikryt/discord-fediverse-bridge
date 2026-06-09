"""Discord command adapter for removing the published guild invite."""

from __future__ import annotations

import discord
from discord import app_commands

from ..config import Settings
from ..bridge_policy import BridgePolicyService
from ..db import Database
from ..operations.remove_guild_invite import RemoveGuildInviteInput, run_remove_guild_invite
from .guild_guard import MANAGE_GUILD_COMMAND_ACCESS, evaluate_command_access, send_command_access_rejection


def register(tree: app_commands.CommandTree, database: Database, settings: Settings, policy_service: BridgePolicyService) -> None:
    """Register the published guild invite removal command."""

    @tree.command(name="remove-guild-invite", description="Remove the server invite from the bridge dashboard")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def remove_command(interaction: discord.Interaction) -> None:
        """Authorize and remove the current guild invite publication."""
        access = await evaluate_command_access(interaction, definition=MANAGE_GUILD_COMMAND_ACCESS, settings=settings, database=database, policy_service=policy_service)
        if not access.allowed:
            if access.reason == "missing_manage_guild" and interaction.guild_id is not None:
                database.management_audit.guild_invite_forbidden(
                    actor_discord_user_id=str(interaction.user.id),
                    discord_guild_id=int(interaction.guild_id),
                    removing=True,
                )
            await send_command_access_rejection(interaction, access)
            return
        result = await run_remove_guild_invite(
            RemoveGuildInviteInput(
                database=database,
                client=interaction.client,
                guild=interaction.guild,
                actor_discord_user_id=str(interaction.user.id),
            )
        )
        await interaction.response.send_message(result.message, ephemeral=True)
