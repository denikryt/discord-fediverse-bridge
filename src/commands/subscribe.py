"""Discord slash command adapter for channel subscription moderation."""

from __future__ import annotations

import logging
from urllib.parse import urlparse

import discord
from discord import app_commands
from discordops import run_operation_definition_async

from ..config import Settings
from ..db import Database
from ..fedify_gateway_client import FedifyGatewayClient
from ..federation_policy import is_instance_allowed
from ..lemmy_client import LemmyClient
from ..operations import SubscribeInput, subscribe_operation

logger = logging.getLogger(__name__)


def register(
    tree: app_commands.CommandTree,
    database: Database,
    fedify_gateway: FedifyGatewayClient,
    settings: Settings | None = None,
) -> None:
    """Register the subscribe-channel slash command on the Discord tree."""
    allowlist = settings.federation_allowlist if settings is not None else []

    @tree.command(name="subscribe-channel", description="Subscribe a forum channel to a Lemmy community")
    @app_commands.describe(
        lemmy_instance="Lemmy instance URL (e.g. https://lemmy.world)",
        community="Lemmy community",
        channel="Forum channel to subscribe",
    )
    @app_commands.autocomplete(
        lemmy_instance=_instance_autocomplete(settings),
        community=_community_autocomplete(settings),
    )
    @app_commands.default_permissions(manage_channels=True)
    async def subscribe_channel(
        interaction: discord.Interaction,
        lemmy_instance: str,
        community: str,
        channel: discord.ForumChannel,
    ) -> None:
        if not lemmy_instance.startswith(("http://", "https://")):
            lemmy_instance = "https://" + lemmy_instance
        if not is_instance_allowed(lemmy_instance, allowlist):
            hostname = urlparse(lemmy_instance).hostname or lemmy_instance
            await interaction.response.send_message(
                f"Instance **{hostname}** is not in the federation allowlist.",
                ephemeral=True,
            )
            return

        actor_id, community_name, community_id_str = _parse_community_value(community)
        numeric_id: int | None = int(community_id_str) if community_id_str else None

        if numeric_id is None:
            tmp_client = LemmyClient(lemmy_instance)
            try:
                numeric_id = await tmp_client.resolve_community_id(name=community_name or actor_id)
            except Exception:
                logger.exception("Failed to resolve community ID for %s", actor_id)
                await interaction.response.send_message(
                    "Could not resolve the Lemmy community ID. Please try again.",
                    ephemeral=True,
                )
                return
            finally:
                await tmp_client.close()

        community_handle = _build_community_handle(actor_id, community_name or None)
        result = await run_operation_definition_async(
            subscribe_operation,
            SubscribeInput(
                database=database,
                fedify_gateway=fedify_gateway,
                discord_user_id=str(interaction.user.id),
                guild_id=interaction.guild_id,
                channel_id=channel.id,
                channel_mention=channel.mention,
                actor_id=actor_id,
                community_name=community_name or None,
                numeric_id=numeric_id,
                community_handle=community_handle,
            ),
        )
        if result.reason == "follow_dispatch_failed":
            logger.error("Failed to send follow for community %s", actor_id)

        is_ephemeral = not result.applied
        await interaction.response.send_message(result.message, ephemeral=is_ephemeral)
        if result.applied:
            logger.info("Sent bridge follow for channel %s to community %s", channel.id, actor_id)


def _instance_autocomplete(settings: Settings | None):
    """Return allowlist entries as Discord choices; empty list when allowlist is open."""
    allowlist = settings.federation_allowlist if settings is not None else []

    async def autocomplete(
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        if not allowlist:
            return []
        return [
            app_commands.Choice(name=hostname, value=f"https://{hostname}")
            for hostname in allowlist
        ]

    return autocomplete


def _community_autocomplete(settings: Settings | None = None):
    """Build the Discord autocomplete callback for Lemmy communities.

    When lemmy_instance is set and allowed, a temporary LemmyClient is created
    for that instance URL and closed after the query so no connection is leaked.
    When lemmy_instance is absent, returns [] — the user must pick an instance first.
    """
    allowlist = settings.federation_allowlist if settings is not None else []

    async def autocomplete(
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        instance_url: str | None = getattr(interaction.namespace, "lemmy_instance", None)

        if not instance_url:
            return []

        if not instance_url.startswith(("http://", "https://")):
            instance_url = "https://" + instance_url

        if not is_instance_allowed(instance_url, allowlist):
            return []

        remote_client = LemmyClient(instance_url)
        try:
            communities = await remote_client.list_communities(limit=50, type_="Local")
        except Exception:
            logger.exception("Failed to fetch communities from %s for autocomplete", instance_url)
            return []
        finally:
            await remote_client.close()

        choices = []
        for item in communities:
            community = item.get("community", {})
            name: str = community.get("name", "")
            title: str = community.get("title", "") or name
            actor_id: str = community.get("actor_id", "")
            numeric_id: int | None = community.get("id")

            if current and current.lower() not in name.lower() and current.lower() not in title.lower():
                continue

            value = f"{actor_id}|{name}|{numeric_id or ''}"
            choices.append(app_commands.Choice(name=f"{title} ({name})", value=value))
            if len(choices) >= 25:
                break

        return choices

    return autocomplete


def _parse_community_value(value: str) -> tuple[str, str, str]:
    """Decode the autocomplete payload into actor ID, short name, and numeric ID."""
    parts = value.split("|", 2)
    actor_id = parts[0] if len(parts) > 0 else value
    name = parts[1] if len(parts) > 1 else ""
    numeric_id_str = parts[2] if len(parts) > 2 else ""
    return actor_id, name, numeric_id_str


def _build_community_handle(actor_id: str, community_name: str | None) -> str:
    """Build a human-readable handle for one selected Lemmy community."""
    parsed = urlparse(actor_id)
    hostname = parsed.hostname
    if community_name and hostname:
        return f"!{community_name}@{hostname}"
    return actor_id
