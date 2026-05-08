"""Discord slash command adapter for channel subscription moderation."""

from __future__ import annotations

import logging
from urllib.parse import urlparse

import discord
from discord import app_commands
from discordops import run_operation_definition_async

from ..db import Database
from ..fedify_gateway_client import FedifyGatewayClient
from ..lemmy_client import LemmyClient
from ..operations import SubscribeInput, subscribe_operation

logger = logging.getLogger(__name__)


def register(
    tree: app_commands.CommandTree,
    database: Database,
    lemmy: LemmyClient,
    fedify_gateway: FedifyGatewayClient,
) -> None:
    """Register the subscribe-channel slash command on the Discord tree."""
    # The registered slash command stays focused on Discord parsing and Lemmy
    # lookup, while the operation layer owns registration and follow lifecycle
    # policy so runtime decisions stay testable without Discord adapters.
    @tree.command(name="subscribe-channel", description="Subscribe a forum channel to a Lemmy community")
    @app_commands.describe(channel="Forum channel to subscribe", community="Lemmy community")
    @app_commands.autocomplete(community=_community_autocomplete(lemmy))
    @app_commands.default_permissions(manage_channels=True)
    async def subscribe_channel(
        interaction: discord.Interaction,
        channel: discord.ForumChannel,
        community: str,
    ) -> None:
        # The autocomplete payload encodes both the actor ID and the numeric
        # community ID so successful selections avoid a second Lemmy round-trip.
        actor_id, community_name, community_id_str = _parse_community_value(community)
        numeric_id: int | None = int(community_id_str) if community_id_str else None

        # If the user typed a value manually instead of picking from autocomplete,
        # the numeric ID may be missing and must be resolved from Lemmy.
        if numeric_id is None:
            try:
                numeric_id = await lemmy.resolve_community_id(name=community_name or actor_id)
            except Exception:
                logger.exception("Failed to resolve community ID for %s", actor_id)
                await interaction.response.send_message(
                    "Could not resolve the Lemmy community ID. Please try again.",
                    ephemeral=True,
                )
                return

        community_handle = _build_community_handle(actor_id, community_name or None)
        result = await run_operation_definition_async(
            subscribe_operation,
            SubscribeInput(
                database=database,
                fedify_gateway=fedify_gateway,
                discord_user_id=str(interaction.user.id),
                channel_id=channel.id,
                channel_mention=channel.mention,
                actor_id=actor_id,
                community_name=community_name or None,
                numeric_id=numeric_id,
                community_handle=community_handle,
            ),
        )
        if result.reason == "follow_dispatch_failed":
            # The operation converts gateway failure into explicit failed state.
            # The command logs the failure here because the exception does not
            # escape the operation body anymore.
            logger.error("Failed to send follow for community %s", actor_id)

        is_ephemeral = not result.applied
        await interaction.response.send_message(result.message, ephemeral=is_ephemeral)
        if result.applied:
            logger.info("Sent bridge follow for channel %s to community %s", channel.id, actor_id)


def _community_autocomplete(lemmy: LemmyClient):
    """Build the Discord autocomplete callback for Lemmy communities."""
    # Autocomplete keeps the existing compact encoding so the command handler
    # can recover actor ID, short name, and numeric ID from one selection.
    async def autocomplete(
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        try:
            communities = await lemmy.list_communities(limit=50)
        except Exception:
            logger.exception("Failed to fetch communities for autocomplete")
            return []

        choices = []
        for item in communities:
            community = item.get("community", {})
            name: str = community.get("name", "")
            title: str = community.get("title", "") or name
            actor_id: str = community.get("actor_id", "")
            numeric_id: int | None = community.get("id")

            if current and current.lower() not in name.lower() and current.lower() not in title.lower():
                continue

            # Encode actor_id, name, and numeric ID into the choice value so the
            # command handler has everything it needs without an extra Lemmy call.
            value = f"{actor_id}|{name}|{numeric_id or ''}"
            choices.append(app_commands.Choice(name=f"{title} ({name})", value=value))
            if len(choices) >= 25:
                break

        return choices

    return autocomplete


def _parse_community_value(value: str) -> tuple[str, str, str]:
    """Decode the autocomplete payload into actor ID, short name, and numeric ID."""
    # Splits the encoded autocomplete value back into its three parts.
    # Falls back gracefully if the user typed a raw actor_id instead of picking
    # from the dropdown.
    parts = value.split("|", 2)
    actor_id = parts[0] if len(parts) > 0 else value
    name = parts[1] if len(parts) > 1 else ""
    numeric_id_str = parts[2] if len(parts) > 2 else ""
    return actor_id, name, numeric_id_str


def _build_community_handle(actor_id: str, community_name: str | None) -> str:
    """Build a human-readable handle for one selected Lemmy community."""
    # The local DB stores a human-readable handle so duplicate and lifecycle
    # messages can use the same label even before another Lemmy round-trip.
    parsed = urlparse(actor_id)
    hostname = parsed.hostname
    if community_name and hostname:
        return f"!{community_name}@{hostname}"
    return actor_id
