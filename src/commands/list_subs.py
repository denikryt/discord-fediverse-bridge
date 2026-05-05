from __future__ import annotations

import discord
from discord import app_commands
from discordops import run_operation_definition_async

from ..db import Database
from ..operations import ListSubscriptionsInput, list_subscriptions_operation


def register(tree: app_commands.CommandTree, database: Database) -> None:
    # The registered slash command delegates empty-state policy to the
    # operation layer and keeps Discord embed rendering in the adapter.
    @tree.command(name="list-subscriptions", description="List all active channel-community subscriptions")
    async def list_subscriptions(interaction: discord.Interaction) -> None:
        # The operation determines whether the list is empty; the command keeps
        # ownership of Discord embed rendering for successful responses.
        result = await run_operation_definition_async(
            list_subscriptions_operation,
            ListSubscriptionsInput(database=database),
        )
        if not result.applied:
            await interaction.response.send_message(result.message, ephemeral=True)
            return

        subscriptions = result.extra_kwargs["subscriptions"] if result.extra_kwargs is not None else []
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
