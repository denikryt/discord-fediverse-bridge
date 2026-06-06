"""Discord command adapter for publishing a guild invite on the dashboard."""

from __future__ import annotations

import discord
from discord import app_commands

from ..config import Settings
from ..db import Database
from ..discord_directory import record_discord_placement_snapshot
from ..operations.publish_guild_invite import PublishGuildInviteInput, run_publish_guild_invite
from .guild_guard import MANAGE_GUILD_COMMAND_ACCESS, evaluate_command_access, send_command_access_rejection


def register(tree: app_commands.CommandTree, database: Database, settings: Settings) -> None:
    """Register the guild invite publication command."""

    @tree.command(name="publish-guild-invite", description="Publish a server invite on the bridge dashboard")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    @app_commands.describe(channel="Public channel hosting an active local community")
    async def publish_command(interaction: discord.Interaction, channel: discord.abc.GuildChannel) -> None:
        """Authorize, validate, create, persist, and publish one guild invite."""
        access = await evaluate_command_access(interaction, definition=MANAGE_GUILD_COMMAND_ACCESS, settings=settings)
        if not access.allowed:
            if access.reason == "missing_manage_guild" and interaction.guild_id is not None:
                database.management_audit.guild_invite_forbidden(
                    actor_discord_user_id=str(interaction.user.id),
                    discord_guild_id=int(interaction.guild_id),
                    removing=False,
                )
            await send_command_access_rejection(interaction, access)
            return
        result = await run_publish_guild_invite(
            PublishGuildInviteInput(
                database=database,
                client=interaction.client,
                guild=interaction.guild,
                channel=channel,
                actor_discord_user_id=str(interaction.user.id),
            )
        )
        if result.applied:
            record_discord_placement_snapshot(database, guild=interaction.guild, channel=channel)
            await interaction.response.send_message(result.message, ephemeral=True)
            return
        await interaction.response.send_message(result.message, ephemeral=True)
