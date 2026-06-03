"""Discord slash command adapter for community subscription moderation."""

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
from ..discord_forum_placement import (
    ForumPlacement,
    ForumPlacementError,
    cleanup_created_forum_channel,
    derive_channel_name_from_community,
    resolve_optional_forum_channel,
)
from ..federation_policy import is_instance_allowed
from ..lemmy_client import LemmyClient
from ..lemmyverse_communities import (
    LemmyverseCommunityCache,
    autocomplete_lemmyverse_communities,
)
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
    lemmyverse_cache: LemmyverseCommunityCache | None = None,
) -> None:
    """Register the subscribe-community slash command on the Discord tree.

    One Lemmyverse cache is created per registration so Discord autocomplete
    keystrokes share the same process-local index instead of downloading the
    public feed repeatedly. The public command now describes the domain action
    (subscribing to a community) while the channel is only the delivery surface.
    """
    allowlist = settings.federation_allowlist if settings is not None else []
    cache = lemmyverse_cache or LemmyverseCommunityCache()

    @tree.command(name="subscribe-community", description="Subscribe to a federated community")
    @app_commands.describe(
        instance_domain="Instance domain or URL (e.g. lemmy.world)",
        community="Community handle, URL, or autocomplete choice",
        channel="Choose an existing free forum channel, or leave empty and the bot will create a new forum channel named after the selected community.",
    )
    @app_commands.autocomplete(
        instance_domain=_instance_autocomplete(settings),
        community=_community_autocomplete(settings, lemmyverse_cache=cache),
    )
    @app_commands.default_permissions(manage_channels=True)
    async def subscribe_community(
        interaction: discord.Interaction,
        community: str,
        channel: discord.ForumChannel | None = None,
        instance_domain: str | None = None,
    ) -> None:
        """Resolve the community, choose/create a forum channel, and subscribe it."""
        if instance_domain is not None and hasattr(instance_domain, "id") and isinstance(channel, str):
            # Older direct test calls used (interaction, instance_domain, community,
            # channel). The public command has been renamed, but this adapter shim
            # keeps direct callback execution compatible while Discord registration
            # exposes only /subscribe-community.
            old_instance_domain = community
            community = channel
            channel = instance_domain
            instance_domain = old_instance_domain

        # Remote hosts remain allowlist-gated, but channel placement must wait
        # until the community identity is resolved so auto-created channel names
        # come from the selected community rather than from raw user text.
        try:
            inferred_origin = infer_reference_origin(community)
        except CommunityResolutionError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        raw_instance = (instance_domain or "").strip()
        try:
            selected_origin = normalize_instance_domain(raw_instance) if raw_instance else None
        except CommunityResolutionError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return

        # Encoded direct-mode payloads remain scoped to the selected instance
        # when one exists. Plain Lemmyverse actor URLs are not encoded and infer
        # their candidate origin from the URL itself.
        candidate_origin = selected_origin if ("|" in community and selected_origin is not None) else inferred_origin
        if candidate_origin is not None and not is_bridge_origin(candidate_origin, settings) and not is_instance_allowed(candidate_origin, allowlist):
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

        resolved_origin = infer_reference_origin(resolved.actor_id) or selected_origin
        if resolved_origin is not None and not is_bridge_origin(resolved_origin, settings) and not is_instance_allowed(resolved_origin, allowlist):
            # Autocomplete choices and manual values are user-controlled at
            # submit time, so the resolved actor URL is checked again before
            # any DB mutation, Discord channel creation, or outbound Follow can occur.
            hostname = urlparse(resolved_origin).hostname or resolved_origin
            await interaction.response.send_message(
                f"Instance **{hostname}** is not in the federation allowlist.",
                ephemeral=True,
            )
            return

        if resolved.source == "remote_lemmy" and resolved.numeric_id is None:
            # Direct remote Lemmy URLs/handles and legacy autocomplete payloads
            # may omit the numeric Lemmy id. Preserve the old contract by
            # resolving it lazily before placement and persistence.
            if resolved_origin is None:
                await interaction.response.send_message(
                    "Could not infer the Lemmy community origin. Please provide instance_domain.",
                    ephemeral=True,
                )
                return
            tmp_client = LemmyClient(resolved_origin)
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

        desired_name = derive_channel_name_from_community(
            name=resolved.name,
            handle=resolved.handle,
            actor_id=resolved.actor_id,
        )
        placement: ForumPlacement | None = None
        try:
            placement = await resolve_optional_forum_channel(
                database=database,
                guild=interaction.guild,
                selected_channel=channel,
                desired_name=desired_name,
                command_name="subscribe-community",
                remote_subscription_blocking_statuses={"pending", "accepted"},
            )
        except ForumPlacementError as error:
            await interaction.response.send_message(error.message, ephemeral=True)
            return

        final_channel = placement.channel
        try:
            if resolved.source == "local_bridge":
                result = await run_operation_definition_async(
                    subscribe_local_community_operation,
                    SubscribeLocalCommunityInput(
                        database=database,
                        discord_user_id=str(interaction.user.id),
                        guild_id=interaction.guild_id,
                        channel_id=final_channel.id,
                        channel_mention=final_channel.mention,
                        local_community_id=int(resolved.local_community_id),
                        local_community_name=resolved.name or resolved.handle,
                    ),
                )
            else:
                result = await run_operation_definition_async(
                    subscribe_operation,
                    SubscribeInput(
                        database=database,
                        fedify_gateway=fedify_gateway,
                        discord_user_id=str(interaction.user.id),
                        guild_id=interaction.guild_id,
                        channel_id=final_channel.id,
                        channel_mention=final_channel.mention,
                        actor_id=resolved.actor_id,
                        community_name=resolved.name,
                        numeric_id=resolved.numeric_id,
                        community_handle=resolved.handle,
                    ),
                )
        except Exception:
            await cleanup_created_forum_channel(
                placement,
                database=database,
                logger=logger,
                guild_id=interaction.guild_id,
                command_name="subscribe-community",
                original_reason="unexpected_exception",
            )
            raise

        if getattr(result, "reason", None) == "follow_dispatch_failed":
            logger.error("Failed to send follow for community %s", resolved.actor_id)
        if not result.applied:
            await cleanup_created_forum_channel(
                placement,
                database=database,
                logger=logger,
                guild_id=interaction.guild_id,
                command_name="subscribe-community",
                original_reason=result.reason,
            )
        else:
            # The operation has committed routing state, so expose the final
            # forum placement through the dashboard snapshot cache.
            record_discord_placement_snapshot(
                database,
                guild=interaction.guild,
                channel=final_channel,
            )
            logger.info("Subscribed channel %s to community %s", final_channel.id, resolved.actor_id)

        await interaction.response.send_message(result.message, ephemeral=not result.applied)


def _extract_instance_domain_for_autocomplete(interaction: discord.Interaction) -> str | None:
    """Read ``instance_domain`` from Discord autocomplete state as defensively as possible.

    discord.py usually exposes sibling option values through
    ``interaction.namespace``. In live clients, especially after clearing the
    focused community text, the namespace can omit optional sibling options even
    though the raw interaction payload still carries them. Scanning the raw
    option tree lets the community autocomplete keep using direct-instance mode
    whenever the moderator has already filled ``instance_domain``.
    """
    namespace_value = getattr(getattr(interaction, "namespace", None), "instance_domain", None)
    if isinstance(namespace_value, str) and namespace_value.strip():
        return namespace_value

    # The raw Discord interaction payload may contain nested subcommand option
    # arrays. The command has no subcommands today, but recursive scanning keeps
    # this helper correct if the command is reorganized later.
    data = getattr(interaction, "data", None)
    for value in _iter_option_values_by_name(data, "instance_domain"):
        if isinstance(value, str) and value.strip():
            return value
    return None


def _iter_option_values_by_name(payload: object, name: str):
    """Yield raw Discord option values whose name matches ``name``."""
    if not isinstance(payload, dict):
        return
    options = payload.get("options")
    if not isinstance(options, list):
        return
    for option in options:
        if not isinstance(option, dict):
            continue
        if option.get("name") == name and "value" in option:
            yield option.get("value")
        yield from _iter_option_values_by_name(option, name)

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


def _community_autocomplete(
    settings: Settings | None = None,
    *,
    lemmyverse_cache: LemmyverseCommunityCache | None = None,
):
    """Build the Discord autocomplete callback for unified community discovery.

    The selected instance can resolve to this bridge, a remote bridge, or a
    normal Lemmy host. The callback keeps network failures non-fatal so Discord
    autocomplete degrades to an empty list instead of surfacing tracebacks.
    """
    allowlist = settings.federation_allowlist if settings is not None else []
    cache = lemmyverse_cache or LemmyverseCommunityCache()

    async def autocomplete(
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        instance_url = _extract_instance_domain_for_autocomplete(interaction)

        if not instance_url or not instance_url.strip():
            try:
                raw_choices = await autocomplete_lemmyverse_communities(
                    cache,
                    current=current,
                    allowlist=allowlist,
                )
            except Exception:
                logger.exception("Failed to autocomplete communities from Lemmyverse")
                return []
            return [app_commands.Choice(name=name, value=value) for name, value in raw_choices]

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
