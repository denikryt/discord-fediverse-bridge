"""Discord command adapter tests for `/create_community`."""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.commands import create_community
from src.commands.guild_guard import GUILD_NOT_ALLOWED_MESSAGE, GUILD_ONLY_MESSAGE, REGISTRATION_REQUIRED_MESSAGE


def _settings() -> SimpleNamespace:
    """Return command settings with no guild restriction for create tests."""
    return SimpleNamespace(
        discord_guild_allowlist=[],
        local_community_operator_allowlist=[],
        normalized_fedify_origin="https://bridge.example",
    )


@pytest.mark.asyncio
async def test_create_community_opens_blank_modal_for_registered_user(command_tree, interaction, database) -> None:
    """The slash command is a launcher and no longer collects creation fields."""
    settings = _settings()
    database.users.get_user_by_discord_user_id.return_value = SimpleNamespace(id=1)
    interaction.response.send_modal = AsyncMock()

    create_community.register(command_tree, database, settings)
    command = command_tree.commands["create_community"]
    assert list(inspect.signature(command.callback).parameters) == ["interaction"]

    await command.callback(interaction)

    database.users.get_user_by_discord_user_id.assert_called_once_with("1234567890")
    interaction.response.send_modal.assert_awaited_once()
    modal = interaction.response.send_modal.await_args.args[0]
    assert isinstance(modal, create_community.CreateCommunityModal)
    interaction.response.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_community_rejects_dm_before_modal(command_tree, interaction, database) -> None:
    """Guild context is required before registration lookup or modal open."""
    settings = _settings()
    interaction.guild_id = None
    interaction.response.send_modal = AsyncMock()

    create_community.register(command_tree, database, settings)
    command = command_tree.commands["create_community"]
    await command.callback(interaction)

    database.users.get_user_by_discord_user_id.assert_not_called()
    interaction.response.send_modal.assert_not_awaited()
    interaction.response.send_message.assert_awaited_once_with(
        GUILD_ONLY_MESSAGE,
        ephemeral=True,
    )


@pytest.mark.asyncio
async def test_create_community_rejects_disallowed_guild_before_registration_lookup(command_tree, interaction, database) -> None:
    """Guild allowlist failures stop before onboarding checks or modal open."""
    settings = SimpleNamespace(
        discord_guild_allowlist=["111"],
        local_community_operator_allowlist=[],
        normalized_fedify_origin="https://bridge.example",
    )
    interaction.guild_id = 99999
    interaction.response.send_modal = AsyncMock()

    create_community.register(command_tree, database, settings)
    command = command_tree.commands["create_community"]
    await command.callback(interaction)

    database.users.get_user_by_discord_user_id.assert_not_called()
    interaction.response.send_modal.assert_not_awaited()
    interaction.response.send_message.assert_awaited_once_with(
        GUILD_NOT_ALLOWED_MESSAGE,
        ephemeral=True,
    )


@pytest.mark.asyncio
async def test_create_community_rejects_unregistered_user_before_modal(command_tree, interaction, database) -> None:
    """Unregistered callers should not open a modal or reach creation logic."""
    settings = _settings()
    database.users.get_user_by_discord_user_id.return_value = None
    interaction.response.send_modal = AsyncMock()

    create_community.register(command_tree, database, settings)
    command = command_tree.commands["create_community"]
    await command.callback(interaction)

    database.users.get_user_by_discord_user_id.assert_called_once_with("1234567890")
    interaction.response.send_modal.assert_not_awaited()
    interaction.response.send_message.assert_awaited_once_with(
        REGISTRATION_REQUIRED_MESSAGE,
        ephemeral=True,
    )


@pytest.mark.asyncio
async def test_create_community_modal_rejects_unregistered_user_before_slug_validation(interaction, database) -> None:
    """Modal submit repeats the registration guard before field processing."""
    settings = _settings()
    database.users.get_user_by_discord_user_id.return_value = None
    modal = create_community.CreateCommunityModal(database=database, settings=settings)
    modal.slug_input._value = "Tech-News2"
    modal.display_name_input._value = "Tech News"
    modal.summary_input._value = ""
    modal.channel_select._values = []

    with patch("src.commands.create_community.resolve_optional_forum_channel", new=AsyncMock()) as placement:
        await modal.on_submit(interaction)

    placement.assert_not_awaited()
    interaction.response.send_message.assert_awaited_once_with(
        REGISTRATION_REQUIRED_MESSAGE,
        ephemeral=True,
    )


@pytest.mark.asyncio
async def test_create_community_modal_rejects_invalid_slug_before_placement(interaction, database) -> None:
    """Invalid slug feedback happens before channel creation or operation calls."""
    settings = _settings()
    database.users.get_user_by_discord_user_id.return_value = SimpleNamespace(id=1)
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
async def test_create_community_modal_registered_user_reaches_placement(interaction, database) -> None:
    """Registered users may create communities without operator allowlist membership."""
    settings = _settings()
    database.users.get_user_by_discord_user_id.return_value = SimpleNamespace(id=1)
    modal = create_community.CreateCommunityModal(database=database, settings=settings)
    modal.slug_input._value = "technology_news"
    modal.display_name_input._value = "Technology News"
    modal.summary_input._value = ""
    modal.channel_select._values = []

    with patch("src.commands.create_community.resolve_optional_forum_channel", new=AsyncMock()) as placement:
        placement.side_effect = RuntimeError("stop after guard")
        with pytest.raises(RuntimeError):
            await modal.on_submit(interaction)

    placement.assert_awaited_once()
    database.management_audit.community_create_forbidden.assert_not_called()


@pytest.mark.asyncio
async def test_create_community_modal_selected_free_channel_snapshots_on_success(interaction, database, forum_channel) -> None:
    """Successful modal submit binds the selected channel and snapshots it."""
    settings = _settings()
    database.users.get_user_by_discord_user_id.return_value = SimpleNamespace(id=1)
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


def test_create_community_modal_label_descriptions_fit_discord_limit(database) -> None:
    """Modal Label descriptions must satisfy Discord's 1..100 length limit."""
    settings = _settings()
    modal = create_community.CreateCommunityModal(database=database, settings=settings)

    descriptions = [getattr(child, "description", None) for child in modal.children]
    descriptions = [description for description in descriptions if description is not None]

    assert descriptions
    assert all(1 <= len(description) <= 100 for description in descriptions)
    assert create_community.CHANNEL_DESCRIPTION in descriptions
