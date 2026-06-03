"""Discord command adapter tests for `/create_community`."""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from src.commands import create_community


@pytest.mark.asyncio
async def test_create_community_opens_blank_modal(command_tree, interaction, database) -> None:
    """The slash command is a launcher and no longer collects creation fields."""
    settings = SimpleNamespace(local_community_operator_allowlist=["1234567890"])
    interaction.response.send_modal = AsyncMock()

    create_community.register(command_tree, database, settings)
    command = command_tree.commands["create_community"]
    assert list(inspect.signature(command.callback).parameters) == ["interaction"]

    await command.callback(interaction)

    interaction.response.send_modal.assert_awaited_once()
    modal = interaction.response.send_modal.await_args.args[0]
    assert isinstance(modal, create_community.CreateCommunityModal)
    interaction.response.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_community_rejects_dm_before_modal(command_tree, interaction, database) -> None:
    """Guild context is required before Discord can choose/create a forum channel."""
    settings = SimpleNamespace(local_community_operator_allowlist=["1234567890"])
    interaction.guild_id = None
    interaction.response.send_modal = AsyncMock()

    create_community.register(command_tree, database, settings)
    command = command_tree.commands["create_community"]
    await command.callback(interaction)

    interaction.response.send_modal.assert_not_awaited()
    interaction.response.send_message.assert_awaited_once_with(
        "This command can only be used inside a guild.",
        ephemeral=True,
    )


@pytest.mark.asyncio
async def test_create_community_modal_rejects_invalid_slug_before_placement(interaction, database) -> None:
    """Invalid slug feedback happens before channel creation or operation calls."""
    settings = SimpleNamespace(local_community_operator_allowlist=["1234567890"])
    modal = create_community.CreateCommunityModal(database=database, settings=settings)
    modal.slug_input._value = "Tech-News2"
    modal.display_name_input._value = "Tech News"
    modal.summary_input._value = ""
    modal.channel_select._values = []

    with patch("src.commands.create_community.resolve_optional_forum_channel", new=AsyncMock()) as placement:
        await modal.on_submit(interaction)

    placement.assert_not_awaited()
    database.management_audit.community_create_forbidden.assert_not_called()
    interaction.response.send_message.assert_awaited_once_with(
        create_community.SLUG_RULE_MESSAGE,
        ephemeral=True,
    )


@pytest.mark.asyncio
async def test_create_community_modal_non_operator_audits_before_placement(interaction, database) -> None:
    """Unauthorized submit records forbidden audit and does not create channels."""
    settings = SimpleNamespace(local_community_operator_allowlist=[])
    modal = create_community.CreateCommunityModal(database=database, settings=settings)
    modal.slug_input._value = "technology_news"
    modal.display_name_input._value = "Technology News"
    modal.summary_input._value = ""
    modal.channel_select._values = []

    with patch("src.commands.create_community.resolve_optional_forum_channel", new=AsyncMock()) as placement:
        await modal.on_submit(interaction)

    placement.assert_not_awaited()
    database.management_audit.community_create_forbidden.assert_called_once_with(
        actor_discord_user_id="1234567890",
        attempted_slug="technology_news",
    )
    interaction.response.send_message.assert_awaited_once_with(
        "You are not allowed to create local communities with this bot.",
        ephemeral=True,
    )


@pytest.mark.asyncio
async def test_create_community_modal_selected_free_channel_snapshots_on_success(interaction, database, forum_channel) -> None:
    """Successful modal submit binds the selected channel and snapshots it."""
    settings = SimpleNamespace(local_community_operator_allowlist=["1234567890"])
    modal = create_community.CreateCommunityModal(database=database, settings=settings)
    modal.slug_input._value = "technology_news"
    modal.display_name_input._value = "Technology News"
    modal.summary_input._value = "A local forum"
    modal.channel_select._values = [forum_channel]
    result = SimpleNamespace(applied=True, message="created", reason="created")

    with patch("src.commands.create_community.create_community_operation", return_value=result) as operation:
        with patch("src.commands.create_community.record_discord_placement_snapshot") as snapshot:
            await modal.on_submit(interaction)

    operation.assert_called_once()
    submitted = operation.call_args.args[0]
    assert submitted.discord_forum_channel_id == forum_channel.id
    snapshot.assert_called_once_with(database, guild=interaction.guild, channel=forum_channel)
    interaction.response.send_message.assert_awaited_once_with("created", ephemeral=False)
