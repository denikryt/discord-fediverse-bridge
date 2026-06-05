"""Discord slash command adapter for listing local-community active bans."""

from __future__ import annotations

import logging

import discord
from discord import app_commands

from ..config import Settings
from ..db import Database
from .guild_guard import GUILD_COMMAND_ACCESS, command_access_allows_autocomplete, reject_if_command_access_denied
from ..operations import ListBannedUsersInput, list_banned_users_operation

logger = logging.getLogger(__name__)


def _choice_label(label: str) -> str:
    """Return a Discord autocomplete label within the 100-character limit."""
    return label if len(label) <= 100 else f"{label[:97]}..."


def _matches_current(value: str, current: str) -> bool:
    """Return whether a candidate should be shown for the typed text."""
    return current.casefold() in value.casefold()


def _list_community_autocomplete(database: Database, settings: Settings):
    """Build autocomplete for `/list-banned-users community` in one guild."""

    async def autocomplete(
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        """Return active local communities in the current Discord guild."""
        try:
            if not await command_access_allows_autocomplete(interaction, definition=GUILD_COMMAND_ACCESS, settings=settings):
                return []
            if interaction.guild_id is None:
                return []
            communities = database.local_communities.list_active_local_communities_by_guild(
                discord_guild_id=interaction.guild_id,
            )
            choices: list[app_commands.Choice[str]] = []
            for community in communities:
                slug = getattr(community, "slug")
                display_name = getattr(community, "display_name", None)
                if not (_matches_current(slug, current) or _matches_current(display_name or "", current)):
                    continue
                label = slug if not display_name or display_name == slug else f"{slug} — {display_name}"
                choices.append(app_commands.Choice(name=_choice_label(label), value=slug))
            return choices[:25]
        except Exception:
            logger.exception("Failed to autocomplete /list-banned-users communities")
            return []

    return autocomplete


def register(
    tree: app_commands.CommandTree,
    database: Database,
    settings: Settings,
) -> None:
    """Register the `/list-banned-users` command on the command tree."""

    @tree.command(
        name="list-banned-users",
        description="List active remote user bans for a local community",
    )
    @app_commands.describe(community="Local community slug")
    @app_commands.autocomplete(community=_list_community_autocomplete(database, settings))
    async def list_banned_users(
        interaction: discord.Interaction,
        community: str,
    ) -> None:
        """Run the list operation and return an ephemeral command reply."""
        if await reject_if_command_access_denied(interaction, definition=GUILD_COMMAND_ACCESS, settings=settings):
            return
        result = list_banned_users_operation(
            ListBannedUsersInput(
                database=database,
                settings=settings,
                discord_user_id=str(interaction.user.id),
                discord_guild_id=interaction.guild_id,
                community_slug=community,
            )
        )
        await interaction.response.send_message(result.message, ephemeral=True)
