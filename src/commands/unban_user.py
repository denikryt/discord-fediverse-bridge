"""Discord slash command adapter for local-community user unbans."""

from __future__ import annotations

import logging

import discord
from discord import app_commands

from ..config import Settings
from ..bridge_policy import BridgePolicyService
from ..db import Database
from ..local_community_permissions import can_manage_local_community, is_super_admin
from .guild_guard import GUILD_COMMAND_ACCESS, command_access_allows_autocomplete, reject_if_command_access_denied
from ..operations import UnbanUserInput, unban_user_operation

logger = logging.getLogger(__name__)


def _choice_label(label: str) -> str:
    """Return a Discord autocomplete label within the 100-character limit."""
    return label if len(label) <= 100 else f"{label[:97]}..."


def _community_label(community: object, *, include_guild: bool) -> str:
    """Build a stable short label for a local-community autocomplete choice."""
    slug = getattr(community, "slug")
    display_name = getattr(community, "display_name", None)
    pieces = [slug]
    if display_name and display_name != slug:
        pieces.append(display_name)
    if include_guild:
        pieces.append(f"guild {getattr(community, 'discord_guild_id', '')}")
    return _choice_label(" — ".join(str(piece) for piece in pieces if piece))


def _matches_current(value: str, current: str) -> bool:
    """Return whether an autocomplete candidate matches the typed text."""
    return current.casefold() in value.casefold()


def _unban_community_autocomplete(database: Database, settings: Settings, policy_service: BridgePolicyService):
    """Build autocomplete for `/unban-user community` with owner/admin scope."""

    async def autocomplete(
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        """Return manageable active communities for the invoking user."""
        try:
            if not await command_access_allows_autocomplete(interaction, definition=GUILD_COMMAND_ACCESS, settings=settings, database=database):
                return []
            discord_user_id = str(interaction.user.id)
            if is_super_admin(policy_snapshot=policy_service.snapshot(), discord_user_id=discord_user_id):
                communities = database.local_communities.list_active_local_communities()
                include_guild = True
            elif interaction.guild_id is not None:
                communities = database.local_communities.list_active_local_communities_owned_by_user_in_guild(
                    discord_guild_id=interaction.guild_id,
                    created_by_discord_user_id=discord_user_id,
                )
                include_guild = False
            else:
                communities = []
                include_guild = False
            choices = [
                app_commands.Choice(
                    name=_community_label(community, include_guild=include_guild),
                    value=getattr(community, "slug"),
                )
                for community in communities
                if _matches_current(getattr(community, "slug"), current)
                or _matches_current(getattr(community, "display_name", ""), current)
            ]
            return choices[:25]
        except Exception:
            logger.exception("Failed to autocomplete /unban-user communities")
            return []

    return autocomplete


def _unban_user_autocomplete(database: Database, settings: Settings, policy_service: BridgePolicyService):
    """Build autocomplete for `/unban-user user` from selected community bans."""

    async def autocomplete(
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        """Return active banned handles for a manageable selected community."""
        try:
            if not await command_access_allows_autocomplete(interaction, definition=GUILD_COMMAND_ACCESS, settings=settings, database=database):
                return []
            namespace = getattr(interaction, "namespace", None)
            community_slug = getattr(namespace, "community", None) if namespace is not None else None
            if not community_slug:
                return []
            community = database.local_communities.get_local_community_by_slug(str(community_slug).strip())
            if community is None:
                return []
            discord_user_id = str(interaction.user.id)
            if interaction.guild_id is None and not is_super_admin(policy_snapshot=policy_service.snapshot(), discord_user_id=discord_user_id):
                return []
            if getattr(community, "discord_guild_id", None) != interaction.guild_id and not is_super_admin(
                policy_snapshot=policy_service.snapshot(),
                discord_user_id=discord_user_id,
            ):
                return []
            if not can_manage_local_community(
                policy_snapshot=policy_service.snapshot(),
                discord_user_id=discord_user_id,
                local_community=community,
            ):
                return []
            bans = database.community_actor_bans.list_active_bans_for_community(
                local_community_id=getattr(community, "id"),
                limit=None,
            )
            choices: list[app_commands.Choice[str]] = []
            for ban in bans:
                reason = getattr(ban, "reason", None) or "reason not specified"
                searchable = f"{ban.actor_handle} {reason}"
                if not _matches_current(searchable, current):
                    continue
                choices.append(
                    app_commands.Choice(
                        name=_choice_label(f"{ban.actor_handle} — {reason}"),
                        value=ban.actor_handle,
                    )
                )
            return choices[:25]
        except Exception:
            logger.exception("Failed to autocomplete /unban-user users")
            return []

    return autocomplete


def register(
    tree: app_commands.CommandTree,
    database: Database,
    settings: Settings,
    policy_service: BridgePolicyService,
) -> None:
    """Register the `/unban-user` command on the Discord command tree."""

    @tree.command(
        name="unban-user",
        description="Remove an active community or global user ban",
    )
    @app_commands.describe(
        community="Optional local community slug; omit for a global super-admin unban",
        user="Banned local or remote user handle",
    )
    @app_commands.autocomplete(
        community=_unban_community_autocomplete(database, settings, policy_service),
        user=_unban_user_autocomplete(database, settings, policy_service),
    )
    async def unban_user(
        interaction: discord.Interaction,
        user: str,
        community: str | None = None,
    ) -> None:
        """Run the unban operation and return an ephemeral command reply."""
        # Preserve the previous direct-callback positional order used by tests.
        if community and "@" in community and "@" not in user:
            user, community = community, user
        if await reject_if_command_access_denied(interaction, definition=GUILD_COMMAND_ACCESS, settings=settings, database=database):
            return
        result = unban_user_operation(
            UnbanUserInput(
                database=database,
                policy_snapshot=policy_service.snapshot(),
                discord_user_id=str(interaction.user.id),
                discord_guild_id=interaction.guild_id,
                community_slug=community,
                actor_handle=user,
            )
        )
        await interaction.response.send_message(result.message, ephemeral=True)
