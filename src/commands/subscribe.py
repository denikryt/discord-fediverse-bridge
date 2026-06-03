"""Discord slash command adapter for community subscription moderation."""

from __future__ import annotations

import logging
from urllib.parse import urlparse

import discord
from discord import app_commands

from ..community_discovery import (
    CommunityResolutionError,
    autocomplete_communities,
    fetch_bridge_community_summaries,
    is_bridge_origin,
    normalize_instance_domain,
    resolve_selected_community,
)
from ..config import Settings
from ..db import Database
from ..fedify_gateway_client import FedifyGatewayClient
from ..federation_policy import is_instance_allowed
from ..lemmy_client import LemmyClient
from ..lemmyverse_communities import (
    LemmyverseCommunityCache,
    autocomplete_lemmyverse_communities,
)
from .subscribe_community_handler import SubscribeCommunityCommandHandler

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
    public feed repeatedly. The callback delegates submit handling to a command
    handler, keeping this module focused on Discord registration and autocomplete.
    """
    cache = lemmyverse_cache or LemmyverseCommunityCache()
    handler = SubscribeCommunityCommandHandler(
        database=database,
        fedify_gateway=fedify_gateway,
        settings=settings,
        lemmy_client_cls_getter=lambda: LemmyClient,
        resolve_selected_community_getter=lambda: resolve_selected_community,
        fetch_bridge_communities_getter=lambda: fetch_bridge_community_summaries,
    )

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
        """Delegate /subscribe-community submit handling to the command handler."""
        await handler.handle(
            interaction=interaction,
            community=community,
            channel=channel,
            instance_domain=instance_domain,
        )


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
