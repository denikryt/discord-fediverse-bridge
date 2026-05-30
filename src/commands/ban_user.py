"""Discord slash command adapter for local-community user bans."""

from __future__ import annotations

import discord
from discord import app_commands

from ..config import Settings
from ..db import Database
from ..operations import BanUserInput, ban_user_operation


def register(
    tree: app_commands.CommandTree,
    database: Database,
    settings: Settings,
) -> None:
    """Register the `/ban-user` command on the Discord application tree."""

    @tree.command(
        name="ban-user",
        description="Ban a remote user from a local community",
    )
    @app_commands.describe(
        community="Local community slug, for example cats",
        user="Remote author handle exactly as shown in Discord, for example alice@example.com",
        reason="Optional moderation note",
    )
    @app_commands.default_permissions(manage_channels=True)
    async def ban_user(
        interaction: discord.Interaction,
        community: str,
        user: str,
        reason: str | None = None,
    ) -> None:
        """Run the moderation operation and return an ephemeral command reply."""
        result = ban_user_operation(
            BanUserInput(
                database=database,
                settings=settings,
                discord_user_id=str(interaction.user.id),
                community_slug=community,
                actor_handle=user,
                reason=reason,
            )
        )
        # Moderation actions and duplicate/error details stay private in v1 so
        # channels are not spammed with operational state or ban reasons.
        await interaction.response.send_message(result.message, ephemeral=True)
