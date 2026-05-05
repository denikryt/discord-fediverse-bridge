from __future__ import annotations

import logging

import discord
from discord import app_commands

from ..db import Database

logger = logging.getLogger(__name__)


def register(tree: app_commands.CommandTree, database: Database) -> None:
    @tree.command(name="unsubscribe-channel", description="Remove a forum channel's Lemmy subscription")
    @app_commands.describe(channel="Forum channel to unsubscribe")
    @app_commands.default_permissions(manage_channels=True)
    async def unsubscribe_channel(
        interaction: discord.Interaction,
        channel: discord.ForumChannel,
    ) -> None:
        # Read the subscription first so we can show the community name in the
        # confirmation message before deleting it.
        subscription = database.get_subscription_by_channel(channel.id)
        if subscription is None:
            await interaction.response.send_message(
                f"Channel {channel.mention} has no active subscription.",
                ephemeral=True,
            )
            return

        community_label = subscription.lemmy_community_name or subscription.lemmy_community_actor_id
        database.delete_subscription(channel.id)
        await interaction.response.send_message(
            f"Unsubscribed {channel.mention} from **{community_label}**.",
        )
        logger.info("Unsubscribed channel %s from community %s", channel.id, subscription.lemmy_community_actor_id)
