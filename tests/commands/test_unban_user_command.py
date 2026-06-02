"""Discord command adapter tests for `/unban-user`."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.commands import unban_user


@pytest.mark.asyncio
async def test_unban_user_command_passes_user_and_guild_and_returns_ephemeral(command_tree, interaction, database) -> None:
    """The adapter passes Discord caller/guild context into runtime policy."""
    settings = SimpleNamespace(local_community_operator_allowlist=[])
    database.local_communities.get_local_community_by_slug.return_value = SimpleNamespace(
        id=1,
        slug="cats",
        discord_guild_id=99999,
        created_by_discord_user_id="1234567890",
        status="active",
    )
    database.community_actor_bans.get_active_ban_by_handle.return_value = SimpleNamespace(
        id=10,
        actor_handle="alice@example.com",
        status="active",
    )

    unban_user.register(command_tree, database, settings)
    command = command_tree.commands["unban-user"]
    await command.callback(interaction, "cats", "alice@example.com")

    database.community_actor_bans.deactivate_active_ban_by_handle_with_audit.assert_called_once_with(
        local_community_id=1,
        actor_handle="alice@example.com",
        actor_discord_user_id="1234567890",
        audit_repository=database.management_audit_events,
    )
    interaction.response.send_message.assert_awaited_once_with(
        "Unbanned alice@example.com from community cats.",
        ephemeral=True,
    )


@pytest.mark.asyncio
async def test_unban_community_autocomplete_owner_sees_only_owned_current_guild(interaction, database) -> None:
    """Owner autocomplete is scoped to owned active communities in this guild."""
    settings = SimpleNamespace(local_community_operator_allowlist=[])
    database.local_communities.list_active_local_communities_owned_by_user_in_guild.return_value = [
        SimpleNamespace(slug="cats", display_name="Cats", discord_guild_id=99999),
    ]

    choices = await unban_user._unban_community_autocomplete(database, settings)(interaction, "cat")

    assert [(choice.name, choice.value) for choice in choices] == [("cats — Cats", "cats")]
    database.local_communities.list_active_local_communities_owned_by_user_in_guild.assert_called_once_with(
        discord_guild_id=99999,
        created_by_discord_user_id="1234567890",
    )


@pytest.mark.asyncio
async def test_unban_community_autocomplete_super_admin_sees_all_guilds(interaction, database) -> None:
    """Super-admin autocomplete lists all active communities across guilds."""
    settings = SimpleNamespace(local_community_operator_allowlist=["1234567890"])
    database.local_communities.list_active_local_communities.return_value = [
        SimpleNamespace(slug="cats", display_name="Cats", discord_guild_id=10),
        SimpleNamespace(slug="dogs", display_name="Dogs", discord_guild_id=20),
    ]

    choices = await unban_user._unban_community_autocomplete(database, settings)(interaction, "")

    assert [(choice.name, choice.value) for choice in choices] == [
        ("cats — Cats — guild 10", "cats"),
        ("dogs — Dogs — guild 20", "dogs"),
    ]


@pytest.mark.asyncio
async def test_unban_user_autocomplete_filters_selected_manageable_community(interaction, database) -> None:
    """User autocomplete shows active bans in the selected manageable community."""
    settings = SimpleNamespace(local_community_operator_allowlist=[])
    interaction.namespace = SimpleNamespace(community="cats")
    database.local_communities.get_local_community_by_slug.return_value = SimpleNamespace(
        id=1,
        slug="cats",
        discord_guild_id=99999,
        created_by_discord_user_id="1234567890",
        status="active",
    )
    database.community_actor_bans.list_active_bans_for_community.return_value = [
        SimpleNamespace(actor_handle="alice@example.com", reason="spam"),
        SimpleNamespace(actor_handle="bob@example.org", reason=None),
    ]

    choices = await unban_user._unban_user_autocomplete(database, settings)(interaction, "example")

    assert [(choice.name, choice.value) for choice in choices] == [
        ("alice@example.com — spam", "alice@example.com"),
        ("bob@example.org — reason not specified", "bob@example.org"),
    ]


@pytest.mark.asyncio
async def test_unban_user_autocomplete_returns_empty_for_inaccessible_community(interaction, database) -> None:
    """User autocomplete must not expose bans for inaccessible communities."""
    settings = SimpleNamespace(local_community_operator_allowlist=[])
    interaction.namespace = SimpleNamespace(community="dogs")
    database.local_communities.get_local_community_by_slug.return_value = SimpleNamespace(
        id=2,
        slug="dogs",
        discord_guild_id=22222,
        created_by_discord_user_id="someone-else",
    )

    choices = await unban_user._unban_user_autocomplete(database, settings)(interaction, "")

    assert choices == []
    database.community_actor_bans.list_active_bans_for_community.assert_not_called()


@pytest.mark.asyncio
async def test_unban_user_autocomplete_caps_at_twenty_five(interaction, database) -> None:
    """Discord autocomplete choices are capped at 25 entries."""
    settings = SimpleNamespace(local_community_operator_allowlist=[])
    interaction.namespace = SimpleNamespace(community="cats")
    database.local_communities.get_local_community_by_slug.return_value = SimpleNamespace(
        id=1,
        slug="cats",
        discord_guild_id=99999,
        created_by_discord_user_id="1234567890",
        status="active",
    )
    database.community_actor_bans.list_active_bans_for_community.return_value = [
        SimpleNamespace(actor_handle=f"user{index:02d}@example.com", reason="spam")
        for index in range(30)
    ]

    choices = await unban_user._unban_user_autocomplete(database, settings)(interaction, "")

    assert len(choices) == 25
