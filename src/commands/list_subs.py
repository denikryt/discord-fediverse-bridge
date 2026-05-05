from __future__ import annotations

import logging

import discord
from discord import app_commands

from ..db import Database

logger = logging.getLogger(__name__)


def register(tree: app_commands.CommandTree, database: Database) -> None:
    @tree.command(name="list-subscriptions", description="List all active channel-community subscriptions")
    async def list_subscriptions(interaction: discord.Interaction) -> None:
        subscriptions = database.get_all_subscriptions()

        if not subscriptions:
            await interaction.response.send_message("No active subscriptions.", ephemeral=True)
            return

        lines = []
        for sub in subscriptions:
            # Use channel mention so Discord renders it as a clickable link.
            channel_mention = f"<#{sub.discord_channel_id}>"
            community_label = sub.lemmy_community_name or sub.lemmy_community_actor_id
            lines.append(f"• {channel_mention} → **{community_label}**")

        embed = discord.Embed(
            title="Active Subscriptions",
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=f"{len(subscriptions)} subscription(s)")
        # ephemeral=True keeps the output visible only to the invoking user to
        # avoid cluttering the channel with a potentially long list.
        await interaction.response.send_message(embed=embed, ephemeral=True)
