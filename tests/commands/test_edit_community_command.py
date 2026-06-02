"""Discord command adapter tests for `/edit-community`."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.commands import edit_community


@pytest.mark.asyncio
async def test_edit_command_opens_prefilled_modal_without_defer(command_tree, interaction, database) -> None:
    """Authorized callers receive a prefilled modal as the initial response."""
    settings = SimpleNamespace(local_community_operator_allowlist=[])
    database.local_communities.get_local_community_by_slug.return_value = SimpleNamespace(
        id=1,
        slug="cats",
        display_name="Cats",
        summary="Old summary",
        discord_guild_id=99999,
        created_by_discord_user_id="1234567890",
        status="active",
    )
    interaction.response.send_modal = AsyncMock()
    interaction.response.defer = AsyncMock()

    edit_community.register(command_tree, database, settings)
    command = command_tree.commands["edit-community"]
    await command.callback(interaction, "cats")

    interaction.response.defer.assert_not_called()
    interaction.response.send_modal.assert_awaited_once()
    modal = interaction.response.send_modal.await_args.args[0]
    assert modal.community_slug == "cats"
    assert str(modal.display_name_input.default) == "Cats"
    assert str(modal.summary_input.default) == "Old summary"
    assert [option.value for option in modal.status_select.options if option.default] == ["active"]
    interaction.response.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_edit_command_rejects_dm_before_modal(command_tree, interaction, database) -> None:
    """Guild context is required before the command opens any modal."""
    settings = SimpleNamespace(local_community_operator_allowlist=[])
    interaction.guild_id = None
    interaction.response.send_modal = AsyncMock()

    edit_community.register(command_tree, database, settings)
    command = command_tree.commands["edit-community"]
    await command.callback(interaction, "cats")

    interaction.response.send_modal.assert_not_awaited()
    interaction.response.send_message.assert_awaited_once_with(
        "This command can only be used inside a guild.",
        ephemeral=True,
    )
    database.local_communities.get_local_community_by_slug.assert_not_called()


@pytest.mark.asyncio
async def test_edit_command_rejects_unauthorized_without_exposing_modal(command_tree, interaction, database) -> None:
    """Unauthorized users must not see prefilled community metadata."""
    settings = SimpleNamespace(local_community_operator_allowlist=[])
    database.local_communities.get_local_community_by_slug.return_value = SimpleNamespace(
        id=1,
        slug="cats",
        display_name="Cats",
        summary="Secret summary",
        discord_guild_id=99999,
        created_by_discord_user_id="someone-else",
        status="active",
    )
    interaction.response.send_modal = AsyncMock()

    edit_community.register(command_tree, database, settings)
    command = command_tree.commands["edit-community"]
    await command.callback(interaction, "cats")

    interaction.response.send_modal.assert_not_awaited()
    interaction.response.send_message.assert_awaited_once_with(
        "You are not allowed to manage this local community.",
        ephemeral=True,
    )


@pytest.mark.asyncio
async def test_edit_modal_submit_delegates_to_operation_and_responds_ephemeral(interaction, database) -> None:
    """Modal submit reuses runtime operation behavior instead of inline writes."""
    settings = SimpleNamespace(local_community_operator_allowlist=[])
    database.local_communities.get_local_community_by_slug.return_value = SimpleNamespace(
        id=1,
        slug="cats",
        display_name="Cats",
        summary="Old summary",
        discord_guild_id=99999,
        created_by_discord_user_id="1234567890",
        status="active",
    )
    database.management_actions.update_local_community_settings.return_value = SimpleNamespace(
        slug="cats",
        display_name="New Cats",
        summary=None,
        status="disabled",
    )
    modal = edit_community.EditCommunityModal(
        database=database,
        settings=settings,
        community_slug="cats",
        display_name="Cats",
        summary="Old summary",
        status="active",
    )
    modal.display_name_input._value = "New Cats"
    modal.summary_input._value = "   "
    modal.status_select._values = ["disabled"]

    await modal.on_submit(interaction)

    database.management_actions.update_local_community_settings.assert_called_once_with(
        actor_discord_user_id="1234567890",
        local_community_id=1,
        display_name="New Cats",
        summary=None,
        status="disabled",
    )
    interaction.response.send_message.assert_awaited_once_with(
        (
            "Updated community cats.\n"
            "Display name: New Cats\n"
            "Summary: not specified\n"
            "Status: disabled\n"
            "New posts, comments, follows, and subscriptions are now blocked."
        ),
        ephemeral=True,
    )


@pytest.mark.asyncio
async def test_edit_community_autocomplete_matches_management_scope(interaction, database) -> None:
    """Autocomplete lists editable active communities for owners and admins."""
    owner_settings = SimpleNamespace(local_community_operator_allowlist=[])
    database.local_communities.list_manageable_local_communities_owned_by_user_in_guild.return_value = [
        SimpleNamespace(slug="cats", display_name="Cats", discord_guild_id=99999, status="active"),
        SimpleNamespace(slug="dogs", display_name="Dogs", discord_guild_id=99999, status="disabled"),
    ]

    owner_choices = await edit_community._edit_community_autocomplete(database, owner_settings)(interaction, "cat")

    assert [(choice.name, choice.value) for choice in owner_choices] == [("cats — Cats — active", "cats")]
    database.local_communities.list_manageable_local_communities_owned_by_user_in_guild.assert_called_once_with(
        discord_guild_id=99999,
        created_by_discord_user_id="1234567890",
    )


@pytest.mark.asyncio
async def test_edit_community_autocomplete_super_admin_sees_all_guilds(interaction, database) -> None:
    """Super-admin autocomplete includes guild context for cross-guild choices."""
    settings = SimpleNamespace(local_community_operator_allowlist=["1234567890"])
    database.local_communities.list_manageable_local_communities.return_value = [
        SimpleNamespace(slug="cats", display_name="Cats", discord_guild_id=10, status="active"),
        SimpleNamespace(slug="dogs", display_name="Dogs", discord_guild_id=20, status="disabled"),
    ]

    choices = await edit_community._edit_community_autocomplete(database, settings)(interaction, "")

    assert [(choice.name, choice.value) for choice in choices] == [
        ("cats — Cats — guild 10 — active", "cats"),
        ("dogs — Dogs — guild 20 — disabled", "dogs"),
    ]


@pytest.mark.asyncio
async def test_edit_community_autocomplete_returns_empty_on_error(interaction, database) -> None:
    """Autocomplete catches repository failures and returns no choices."""
    settings = SimpleNamespace(local_community_operator_allowlist=[])
    database.local_communities.list_manageable_local_communities_owned_by_user_in_guild.side_effect = RuntimeError("db down")

    choices = await edit_community._edit_community_autocomplete(database, settings)(interaction, "")

    assert choices == []
