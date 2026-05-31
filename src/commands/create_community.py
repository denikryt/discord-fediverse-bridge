"""Discord slash command adapter for local-community creation."""

from __future__ import annotations

import discord
from discord import app_commands

from ..config import Settings
from ..db import Database
from ..operations import CreateCommunityInput, create_community_operation
from ..discord_directory import record_discord_placement_snapshot


def register(
    tree: app_commands.CommandTree,
    database: Database,
    settings: Settings,
) -> None:
    """Register the `/create_community` command on the Discord tree."""

    @tree.command(
        name="create_community",
        description="Create a Discord-backed local federated community",
    )
    @app_commands.describe(
        slug="Stable community slug used in handles and URLs",
        name="Human-readable community name",
        description="Optional community description shown to Lemmy followers",
        channel="Forum channel to bind to this local community",
    )
    @app_commands.default_permissions(manage_channels=True)
    async def create_community(
        interaction: discord.Interaction,
        slug: str,
        name: str,
        channel: discord.ForumChannel,
        description: str | None = None,
    ) -> None:
        guild_id = interaction.guild_id
        if guild_id is None:
            await interaction.response.send_message(
                "This command can only be used inside a guild.",
                ephemeral=True,
            )
            return

        result = create_community_operation(
            CreateCommunityInput(
                database=database,
                settings=settings,
                discord_user_id=str(interaction.user.id),
                discord_guild_id=guild_id,
                discord_forum_channel_id=channel.id,
                slug=slug,
                name=name,
                description=description,
            )
        )
        if result.applied:
            # Snapshot only committed moderation actions. Rejected attempts should
            # not make the public dashboard claim a channel hosts a community.
            record_discord_placement_snapshot(
                database,
                guild=interaction.guild,
                channel=channel,
            )
        await interaction.response.send_message(
            result.message,
            ephemeral=not result.applied,
        )
