"""Discord command and modal adapter for local-community creation."""

from __future__ import annotations

import logging
import re
from typing import Any

import discord
from discord import app_commands

from ..config import Settings
from ..db import Database
from ..discord_directory import record_discord_placement_snapshot
from ..discord_forum_placement import (
    ForumPlacement,
    ForumPlacementError,
    cleanup_created_forum_channel,
    resolve_optional_forum_channel,
)
from ..operations import CreateCommunityInput, create_community_operation
from .guild_guard import REGISTERED_GUILD_COMMAND_ACCESS, reject_if_command_access_denied

logger = logging.getLogger(__name__)

SLUG_PATTERN = re.compile(r"^[a-z]+(?:_[a-z]+)*$")
SLUG_RULE_MESSAGE = "Slug must use lowercase English letters only, with _ between words. No digits, spaces, hyphens, or symbols."
CHANNEL_DESCRIPTION = "Choose a free forum channel, or leave empty to create one named after the community slug."


def _text_input_value(input_item: Any) -> str:
    """Return a modal text-input value from real discord.py objects or tests."""
    return str(getattr(input_item, "value", None) or getattr(input_item, "_value", None) or "")


def _selected_channel_or_none(select_item: Any) -> Any | None:
    """Return the optional channel selected in the create-community modal."""
    values = getattr(select_item, "values", None) or getattr(select_item, "_values", None) or []
    return values[0] if values else None


def _add_labeled_item(modal: discord.ui.Modal, *, text: str, description: str | None, component: Any) -> None:
    """Add a modal component using the Discord Components v2 label wrapper.

    discord.py 2.6 requires modal inputs and selects to be children of
    ``discord.ui.Label``. Keeping the wrapper in one helper makes tests and the
    modal structure easier to scan while preserving the project's SDK contract.
    """
    modal.add_item(discord.ui.Label(text=text, description=description, component=component))


class CreateCommunityModal(discord.ui.Modal):
    """Collect local-community creation fields and submit the operation."""

    def __init__(self, *, database: Database, settings: Settings) -> None:
        """Build an empty creation modal that defers authorization until submit."""
        super().__init__(title="Create local community")
        self.database = database
        self.settings = settings
        self.slug_input = discord.ui.TextInput(
            placeholder="technology_news",
            required=True,
            min_length=1,
            max_length=64,
        )
        self.display_name_input = discord.ui.TextInput(
            placeholder="Technology News",
            required=True,
            min_length=1,
            max_length=128,
        )
        self.summary_input = discord.ui.TextInput(
            style=discord.TextStyle.long,
            placeholder="Optional description shown to Lemmy followers",
            required=False,
            max_length=4000,
        )
        self.channel_select = discord.ui.ChannelSelect(
            channel_types=[discord.ChannelType.forum],
            min_values=0,
            max_values=1,
            required=False,
        )

        _add_labeled_item(
            self,
            text="Slug",
            description="Use lowercase English letters only. Use _ between words. No digits, spaces, hyphens, or symbols.",
            component=self.slug_input,
        )
        _add_labeled_item(self, text="Display name", description=None, component=self.display_name_input)
        _add_labeled_item(self, text="Summary", description=None, component=self.summary_input)
        _add_labeled_item(self, text="Forum channel", description=CHANNEL_DESCRIPTION, component=self.channel_select)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        """Validate modal input, resolve channel placement, and create the community."""
        if await reject_if_command_access_denied(interaction, definition=REGISTERED_GUILD_COMMAND_ACCESS, settings=self.settings, database=self.database):
            return

        guild_id = interaction.guild_id
        if interaction.guild is None:
            await interaction.response.send_message(
                "This command can only be used inside an allowed Discord server.",
                ephemeral=True,
            )
            return

        slug = _text_input_value(self.slug_input).strip()
        name = _text_input_value(self.display_name_input).strip()
        summary = _text_input_value(self.summary_input).strip() or None
        if not SLUG_PATTERN.fullmatch(slug):
            # Slug validation is local and cheap, so it runs before any Discord
            # channel creation, DB mutation, directory snapshot, or success audit.
            await interaction.response.send_message(SLUG_RULE_MESSAGE, ephemeral=True)
            return

        placement: ForumPlacement | None = None
        try:
            placement = await resolve_optional_forum_channel(
                database=self.database,
                guild=interaction.guild,
                selected_channel=_selected_channel_or_none(self.channel_select),
                desired_name=slug,
                command_name="create_community",
            )
        except ForumPlacementError as error:
            await interaction.response.send_message(error.message, ephemeral=True)
            return

        try:
            result = create_community_operation(
                CreateCommunityInput(
                    database=self.database,
                    settings=self.settings,
                    discord_user_id=str(interaction.user.id),
                    discord_guild_id=guild_id,
                    discord_forum_channel_id=int(placement.channel.id),
                    slug=slug,
                    name=name,
                    description=summary,
                )
            )
        except Exception:
            await cleanup_created_forum_channel(
                placement,
                database=self.database,
                logger=logger,
                guild_id=guild_id,
                command_name="create_community",
                original_reason="unexpected_exception",
            )
            raise

        if not result.applied:
            await cleanup_created_forum_channel(
                placement,
                database=self.database,
                logger=logger,
                guild_id=guild_id,
                command_name="create_community",
                original_reason=result.reason,
            )
        else:
            # Snapshot only after the domain row exists. A rejected modal submit
            # must not make the dashboard advertise a nonexistent placement.
            record_discord_placement_snapshot(
                self.database,
                guild=interaction.guild,
                channel=placement.channel,
            )
        await interaction.response.send_message(result.message, ephemeral=not result.applied)


def register(
    tree: app_commands.CommandTree,
    database: Database,
    settings: Settings,
) -> None:
    """Register the `/create_community` modal launcher on the Discord tree."""

    @tree.command(
        name="create_community",
        description="Create a Discord-backed local federated community",
    )
    async def create_community(interaction: discord.Interaction) -> None:
        """Open the local-community creation modal for registered guild users."""
        if await reject_if_command_access_denied(interaction, definition=REGISTERED_GUILD_COMMAND_ACCESS, settings=settings, database=database):
            return
        await interaction.response.send_modal(CreateCommunityModal(database=database, settings=settings))
