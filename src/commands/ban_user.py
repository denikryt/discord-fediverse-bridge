"""Discord slash command adapter for local-community user bans."""

from __future__ import annotations

import logging

import discord
from discord import app_commands

from ..config import Settings
from ..db import Database
from ..local_community_permissions import is_super_admin
from .guild_guard import GUILD_COMMAND_ACCESS, command_access_allows_autocomplete, reject_if_command_access_denied
from ..operations import BanUserInput, ban_user_operation

logger = logging.getLogger(__name__)


def _choice_label(label: str) -> str:
    """Return a Discord autocomplete label within the 100-character limit."""
    return label if len(label) <= 100 else f"{label[:97]}..."


def _community_label(community: object, *, include_guild: bool) -> str:
    """Build a short label for one `/ban-user community` choice."""
    slug = getattr(community, "slug")
    display_name = getattr(community, "display_name", None)
    pieces = [slug]
    if display_name and display_name != slug:
        pieces.append(display_name)
    if include_guild:
        pieces.append(f"guild {getattr(community, 'discord_guild_id', '')}")
    return _choice_label(" — ".join(str(piece) for piece in pieces if piece))


def _matches_current(value: str, current: str) -> bool:
    """Return whether an autocomplete candidate matches typed text."""
    return current.casefold() in value.casefold()


def _ban_community_autocomplete(database: Database, settings: Settings):
    """Build autocomplete for `/ban-user community` with owner/admin scope."""

    async def autocomplete(
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        """Return manageable active local communities for the invoking user."""
        try:
            if not await command_access_allows_autocomplete(interaction, definition=GUILD_COMMAND_ACCESS, settings=settings):
                return []
            discord_user_id = str(interaction.user.id)
            if is_super_admin(settings=settings, discord_user_id=discord_user_id):
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

            choices: list[app_commands.Choice[str]] = []
            for community in communities:
                slug = getattr(community, "slug")
                display_name = getattr(community, "display_name", "") or ""
                if not (_matches_current(slug, current) or _matches_current(display_name, current)):
                    continue
                # Submitted values stay as plain slugs because the schema keeps
                # local_communities.slug globally unique. Guild context belongs
                # only in the label for super-admin readability.
                choices.append(
                    app_commands.Choice(
                        name=_community_label(community, include_guild=include_guild),
                        value=slug,
                    )
                )
            return choices[:25]
        except Exception:
            logger.exception("Failed to autocomplete /ban-user communities")
            return []

    return autocomplete


def register(
    tree: app_commands.CommandTree,
    database: Database,
    settings: Settings,
) -> None:
    """Register the `/ban-user` command on the Discord application tree."""

    @tree.command(
        name="ban-user",
        description="Ban a local or remote user from a community or bridge-wide",
    )
    @app_commands.describe(
        community="Optional local community slug; omit for a global super-admin ban",
        user="Local or remote user handle, for example alice@example.com",
        reason="Optional moderation note",
    )
    @app_commands.autocomplete(
        community=_ban_community_autocomplete(database, settings),
    )
    async def ban_user(
        interaction: discord.Interaction,
        user: str,
        community: str | None = None,
        reason: str | None = None,
    ) -> None:
        """Run the moderation operation and return an ephemeral command reply."""
        # Preserve direct-callback compatibility with the previous positional
        # order used by project tests while Discord itself supplies named options.
        if community and "@" in community and "@" not in user:
            user, community = community, user
        if await reject_if_command_access_denied(interaction, definition=GUILD_COMMAND_ACCESS, settings=settings):
            return
        result = ban_user_operation(
            BanUserInput(
                database=database,
                settings=settings,
                discord_user_id=str(interaction.user.id),
                discord_guild_id=interaction.guild_id,
                community_slug=community,
                actor_handle=user,
                reason=reason,
            )
        )
        # Moderation output stays private. A newly activated local-user ban is
        # also notified by DM after the authoritative ban+audit transaction has
        # committed; Discord DM failures never roll back moderation state.
        await interaction.response.send_message(result.message, ephemeral=True)
        if result.applied and result.activation_kind in {"created", "reactivated"} and result.target_discord_user_id:
            from ..user_bans import BanDecision, canonical_local_community_handle, render_ban_message
            community_handle = None
            if result.scope == "community" and result.community_slug:
                community_handle = canonical_local_community_handle(slug=result.community_slug, settings=settings)
            decision = BanDecision(True, scope=result.scope, reason=result.stored_reason, community_handle=community_handle)
            try:
                target = await interaction.client.fetch_user(int(result.target_discord_user_id))
                await target.send(render_ban_message(decision))
            except Exception:
                logger.exception("Failed to DM newly banned Discord user %s", result.target_discord_user_id)
