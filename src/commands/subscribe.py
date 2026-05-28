"""Discord slash command adapter for channel subscription moderation."""

from __future__ import annotations

import logging
from urllib.parse import urlparse

import discord
from discord import app_commands
from discordops import run_operation_definition_async

from ..community_discovery import (
    CommunityResolutionError,
    autocomplete_communities,
    fetch_bridge_community_summaries,
    infer_reference_origin,
    is_bridge_origin,
    normalize_instance_domain,
    resolve_selected_community,
)
from ..config import Settings
from ..db import Database
from ..fedify_gateway_client import FedifyGatewayClient
from ..discord_directory import record_discord_placement_snapshot
from ..federation_policy import is_instance_allowed
from ..lemmy_client import LemmyClient
from ..operations import SubscribeInput, subscribe_operation
from ..operations.subscribe_local_community import (
    SubscribeLocalCommunityInput,
    subscribe_local_community_operation,
)

logger = logging.getLogger(__name__)


def register(
    tree: app_commands.CommandTree,
    database: Database,
    fedify_gateway: FedifyGatewayClient,
    settings: Settings | None = None,
) -> None:
    """Register the subscribe-channel slash command on the Discord tree."""
    allowlist = settings.federation_allowlist if settings is not None else []

    @tree.command(name="subscribe-channel", description="Subscribe a forum channel to a federated community")
    @app_commands.describe(
        instance_domain="Instance domain or URL (e.g. lemmy.world)",
        community="Community handle, URL, or autocomplete choice",
        channel="Forum channel to subscribe",
    )
    @app_commands.autocomplete(
        instance_domain=_instance_autocomplete(settings),
        community=_community_autocomplete(settings),
    )
    @app_commands.default_permissions(manage_channels=True)
    async def subscribe_channel(
        interaction: discord.Interaction,
        instance_domain: str,
        community: str,
        channel: discord.ForumChannel,
    ) -> None:
        # Remote hosts remain allowlist-gated, but the bridge's own origin is a
        # local routing surface and must stay usable even when federation is
        # restricted to an explicit remote allowlist.
        inferred_origin = infer_reference_origin(community)
        try:
            selected_origin = normalize_instance_domain(instance_domain)
        except CommunityResolutionError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return

        # Autocomplete payloads are already scoped to the selected instance, so
        # the allowlist message should continue to reference that chosen host.
        # Direct handles and actor URLs infer their own host instead.
        candidate_origin = selected_origin if "|" in community else (inferred_origin or selected_origin)
        if not is_bridge_origin(candidate_origin, settings) and not is_instance_allowed(candidate_origin, allowlist):
            hostname = urlparse(candidate_origin).hostname or candidate_origin
            await interaction.response.send_message(
                f"Instance **{hostname}** is not in the federation allowlist.",
                ephemeral=True,
            )
            return

        try:
            resolved = await resolve_selected_community(
                settings,
                instance_domain=instance_domain,
                community_value=community,
                fetch_bridge_communities=fetch_bridge_community_summaries,
                lemmy_client_cls=LemmyClient,
            )
        except CommunityResolutionError as error:
            logger.warning("Failed to resolve subscribe target %s: %s", community, error)
            await interaction.response.send_message(str(error), ephemeral=True)
            return

        if resolved.source == "local_bridge":
            result = await run_operation_definition_async(
                subscribe_local_community_operation,
                SubscribeLocalCommunityInput(
                    database=database,
                    discord_user_id=str(interaction.user.id),
                    guild_id=interaction.guild_id,
                    channel_id=channel.id,
                    channel_mention=channel.mention,
                    local_community_id=int(resolved.local_community_id),
                    local_community_name=resolved.name or resolved.handle,
                ),
            )
            if result.applied:
                # Only successful local subscriptions should become dashboard
                # placement rows; rejected attempts have no active routing state.
                record_discord_placement_snapshot(
                    database,
                    guild=interaction.guild,
                    channel=channel,
                )
            await interaction.response.send_message(result.message, ephemeral=not result.applied)
            return

        if resolved.source == "remote_lemmy" and resolved.numeric_id is None:
            # Direct remote Lemmy URLs/handles and legacy autocomplete payloads
            # may omit the numeric Lemmy id. Preserve the old contract by
            # resolving it lazily before the operation layer persists the row.
            tmp_client = LemmyClient(selected_origin)
            try:
                numeric_id = await tmp_client.resolve_community_id(
                    name=resolved.name or resolved.actor_id
                )
            except Exception:
                logger.exception("Failed to resolve community ID for %s", resolved.actor_id)
                await interaction.response.send_message(
                    "Could not resolve the Lemmy community ID. Please try again.",
                    ephemeral=True,
                )
                return
            finally:
                await tmp_client.close()
            resolved = type(resolved)(
                source=resolved.source,
                actor_id=resolved.actor_id,
                name=resolved.name,
                numeric_id=numeric_id,
                handle=resolved.handle,
                local_community_id=resolved.local_community_id,
                remote_software=resolved.remote_software,
            )

        result = await run_operation_definition_async(
            subscribe_operation,
            SubscribeInput(
                database=database,
                fedify_gateway=fedify_gateway,
                discord_user_id=str(interaction.user.id),
                guild_id=interaction.guild_id,
                channel_id=channel.id,
                channel_mention=channel.mention,
                actor_id=resolved.actor_id,
                community_name=resolved.name,
                numeric_id=resolved.numeric_id,
                community_handle=resolved.handle,
            ),
        )
        if result.reason == "follow_dispatch_failed":
            logger.error("Failed to send follow for community %s", resolved.actor_id)

        is_ephemeral = not result.applied
        await interaction.response.send_message(result.message, ephemeral=is_ephemeral)
        if result.applied:
            # The remote-subscription row is now committed, so expose its forum
            # placement through the dashboard snapshot cache.
            record_discord_placement_snapshot(
                database,
                guild=interaction.guild,
                channel=channel,
            )
            logger.info("Sent bridge follow for channel %s to community %s", channel.id, resolved.actor_id)


def _instance_autocomplete(settings: Settings | None):
    """Return allowlist entries as Discord choices; empty list when allowlist is open."""
    allowlist = settings.federation_allowlist if settings is not None else []

    async def autocomplete(
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        if not allowlist:
            return []
        choices = [
            app_commands.Choice(name=hostname, value=f"https://{hostname}")
            for hostname in allowlist
        ]
        # Same-instance local discovery should be discoverable from the same
        # command surface even when remote federation uses a restrictive list.
        if settings is not None:
            local_origin = getattr(settings, "normalized_public_bridge_base_url", "")
            local_hostname = urlparse(local_origin).hostname
            if local_origin and local_hostname and all(choice.name != local_hostname for choice in choices):
                choices.append(app_commands.Choice(name=local_hostname, value=local_origin))
        return choices

    return autocomplete


def _community_autocomplete(settings: Settings | None = None):
    """Build the Discord autocomplete callback for unified community discovery.

    The selected instance can resolve to this bridge, a remote bridge, or a
    normal Lemmy host. The callback keeps network failures non-fatal so Discord
    autocomplete degrades to an empty list instead of surfacing tracebacks.
    """
    allowlist = settings.federation_allowlist if settings is not None else []

    async def autocomplete(
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        instance_url: str | None = getattr(interaction.namespace, "instance_domain", None)

        if not instance_url:
            return []

        try:
            normalized_origin = normalize_instance_domain(instance_url)
        except CommunityResolutionError:
            return []
        if not is_bridge_origin(normalized_origin, settings) and not is_instance_allowed(normalized_origin, allowlist):
            return []
        try:
            raw_choices = await autocomplete_communities(
                settings,
                instance_domain=instance_url,
                current=current,
                fetch_bridge_communities=fetch_bridge_community_summaries,
                lemmy_client_cls=LemmyClient,
            )
        except Exception:
            logger.exception("Failed to autocomplete communities from %s", normalized_origin)
            return []
        return [app_commands.Choice(name=name, value=value) for name, value in raw_choices]

    return autocomplete
