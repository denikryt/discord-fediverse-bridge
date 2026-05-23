from __future__ import annotations

import logging

import discord
from discord import app_commands
from discordops import run_operation_definition_async

from ..db import Database
from ..config import Settings
from ..fedify_gateway_client import FedifyGatewayClient
from ..operations import UnsubscribeInput, unsubscribe_operation

logger = logging.getLogger(__name__)


def register(
    tree: app_commands.CommandTree,
    database: Database,
    fedify_gateway: FedifyGatewayClient,
    settings: Settings | None = None,
) -> None:
    """Register the unsubscribe-channel slash command on the given command tree."""
    # The command no longer needs Settings directly, but older tests and
    # call-sites still register it with the pre-settings contract. Keeping the
    # argument optional preserves that behavior while the command remains a
    # thin adapter around the operation layer.
    # The registered slash command adapts Discord input into the operation
    # contract and leaves policy decisions to the framework-backed layer.
    @tree.command(name="unsubscribe-channel", description="Remove a forum channel's Lemmy subscription")
    @app_commands.describe(channel="Forum channel to unsubscribe")
    @app_commands.default_permissions(manage_channels=True)
    async def unsubscribe_channel(
        interaction: discord.Interaction,
        channel: discord.ForumChannel,
    ) -> None:
        """Handle the /unsubscribe-channel slash command."""
        # The command adapter only supplies Discord-facing context; the
        # operation decides whether deletion is allowed and what result to show.
        result = await run_operation_definition_async(
            unsubscribe_operation,
            UnsubscribeInput(
                database=database,
                fedify_gateway=fedify_gateway,
                channel_id=channel.id,
                channel_mention=channel.mention,
            ),
        )
        await interaction.response.send_message(result.message, ephemeral=not result.applied)
        if result.applied:
            logger.info("Unsubscribed channel %s", channel.id)
