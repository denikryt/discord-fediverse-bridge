"""Discord slash command and modal adapter for local-community metadata edits."""

from __future__ import annotations

import logging

import discord
from discord import app_commands

from ..config import Settings
from ..db import Database
from ..local_community_permissions import (
    can_access_local_community_from_guild,
    can_manage_local_community,
    is_super_admin,
)
from .guild_guard import GUILD_COMMAND_ACCESS, command_access_allows_autocomplete, reject_if_command_access_denied
from ..operations import EditCommunityInput, edit_community_operation

logger = logging.getLogger(__name__)


def _choice_label(label: str) -> str:
    """Return a Discord autocomplete label within the 100-character limit."""
    return label if len(label) <= 100 else f"{label[:97]}..."


def _community_label(community: object, *, include_guild: bool, include_status: bool = False) -> str:
    """Build the display label for one community autocomplete choice."""
    slug = getattr(community, "slug")
    display_name = getattr(community, "display_name", None)
    pieces = [slug]
    if display_name and display_name != slug:
        pieces.append(display_name)
    if include_guild:
        pieces.append(f"guild {getattr(community, 'discord_guild_id', '')}")
    if include_status:
        pieces.append(getattr(community, "status", "active"))
    return _choice_label(" — ".join(str(piece) for piece in pieces if piece))


def _matches_current(value: str, current: str) -> bool:
    """Return whether an autocomplete candidate matches typed text."""
    return current.casefold() in value.casefold()


def _edit_community_autocomplete(database: Database, settings: Settings):
    """Build `/edit-community community` autocomplete with management scope."""

    async def autocomplete(
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        """Return editable active communities for this Discord caller."""
        try:
            if not await command_access_allows_autocomplete(interaction, definition=GUILD_COMMAND_ACCESS, settings=settings):
                return []
            discord_user_id = str(interaction.user.id)
            if is_super_admin(settings=settings, discord_user_id=discord_user_id):
                communities = database.local_communities.list_manageable_local_communities()
                include_guild = True
            elif interaction.guild_id is not None:
                communities = database.local_communities.list_manageable_local_communities_owned_by_user_in_guild(
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
                # Slug remains the submitted value because the data model keeps
                # local_communities.slug globally unique across all guilds.
                choices.append(
                    app_commands.Choice(
                        name=_community_label(community, include_guild=include_guild, include_status=True),
                        value=slug,
                    )
                )
            return choices[:25]
        except Exception:
            logger.exception("Failed to autocomplete /edit-community communities")
            return []

    return autocomplete


class EditCommunityModal(discord.ui.Modal):
    """Discord modal that collects local-community display metadata."""

    def __init__(
        self,
        *,
        database: Database,
        settings: Settings,
        community_slug: str,
        display_name: str,
        summary: str | None,
        status: str = "active",
    ) -> None:
        """Create prefilled TextInput fields from the selected community row."""
        super().__init__(title=f"Edit {community_slug}")
        self.database = database
        self.settings = settings
        self.community_slug = community_slug
        # Discord modal defaults must be supplied when constructing the inputs,
        # so fields are added dynamically instead of as static class attrs.
        self.display_name_input = discord.ui.TextInput(
            label="Display name",
            style=discord.TextStyle.short,
            default=display_name,
            required=True,
            max_length=100,
        )
        self.summary_input = discord.ui.TextInput(
            label="Summary",
            style=discord.TextStyle.paragraph,
            default=summary or "",
            required=False,
            max_length=1000,
        )
        active_default = status == "active"
        self.status_select = discord.ui.Select(
            custom_id="local_community_status",
            placeholder="Status",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label="Active", value="active", default=active_default),
                discord.SelectOption(label="Disabled", value="disabled", default=not active_default),
            ],
        )
        self.add_item(self.display_name_input)
        self.add_item(self.summary_input)
        # New Discord modal select components are wrapped in Label components.
        # Keep the raw UI concern here so the operation layer stays SDK-free.
        self.status_label = discord.ui.Label(text="Status", component=self.status_select)
        self.add_item(self.status_label)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        """Re-run runtime authorization and persist the submitted metadata."""
        if await reject_if_command_access_denied(interaction, definition=GUILD_COMMAND_ACCESS, settings=self.settings):
            return
        result = edit_community_operation(
            EditCommunityInput(
                database=self.database,
                settings=self.settings,
                discord_user_id=str(interaction.user.id),
                discord_guild_id=interaction.guild_id,
                community_slug=self.community_slug,
                display_name=str(self.display_name_input.value),
                summary=str(self.summary_input.value),
                status=self._submitted_status(),
            )
        )
        await interaction.response.send_message(result.message, ephemeral=True)

    def _submitted_status(self) -> str:
        """Return the selected status, falling back to the default option.

        Discord populates `values` during real modal submit. Tests that call
        `on_submit` directly may leave it empty, so the selected default keeps
        the modal's unchanged-save behavior deterministic.
        """
        if self.status_select.values:
            return str(self.status_select.values[0])
        for option in self.status_select.options:
            if option.default:
                return str(option.value)
        return ""


def _can_open_edit_modal(
    *,
    database: Database,
    settings: Settings,
    discord_user_id: str,
    discord_guild_id: int | None,
    community_slug: str,
) -> tuple[object | None, str | None]:
    """Return the community row or the command-visible precheck error.

    This precheck prevents exposing existing metadata in a modal to callers that
    cannot manage the selected community. The modal submit still re-checks the
    same security boundary before mutating state.
    """
    if discord_guild_id is None:
        return None, "This command can only be used inside a guild."
    normalized_slug = community_slug.strip()
    community = database.local_communities.get_local_community_by_slug(normalized_slug)
    if community is None or not can_access_local_community_from_guild(
        settings=settings,
        discord_user_id=discord_user_id,
        discord_guild_id=discord_guild_id,
        local_community=community,
        include_disabled=True,
    ):
        return None, f"Unknown or inaccessible local community: {normalized_slug}"
    if not can_manage_local_community(
        settings=settings,
        discord_user_id=discord_user_id,
        local_community=community,
    ):
        return None, "You are not allowed to manage this local community."
    return community, None


def register(
    tree: app_commands.CommandTree,
    database: Database,
    settings: Settings,
) -> None:
    """Register the `/edit-community` command on the Discord application tree."""

    @tree.command(
        name="edit-community",
        description="Edit local community display metadata",
    )
    @app_commands.describe(
        community="Local community slug, for example cats",
    )
    @app_commands.autocomplete(
        community=_edit_community_autocomplete(database, settings),
    )
    async def edit_community(
        interaction: discord.Interaction,
        community: str,
    ) -> None:
        """Open a prefilled edit modal or return a private rejection message."""
        if await reject_if_command_access_denied(interaction, definition=GUILD_COMMAND_ACCESS, settings=settings):
            return
        community_row, error = _can_open_edit_modal(
            database=database,
            settings=settings,
            discord_user_id=str(interaction.user.id),
            discord_guild_id=interaction.guild_id,
            community_slug=community,
        )
        if error is not None or community_row is None:
            await interaction.response.send_message(error or "Unable to edit community.", ephemeral=True)
            return

        modal = EditCommunityModal(
            database=database,
            settings=settings,
            community_slug=getattr(community_row, "slug"),
            display_name=getattr(community_row, "display_name"),
            summary=getattr(community_row, "summary", None),
            status=getattr(community_row, "status", "active"),
        )
        # Discord requires modals to be the initial interaction response. Do not
        # defer or open the modal through follow-up messages.
        await interaction.response.send_modal(modal)
