"""Discord command adapter tests for `/ban-user`."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.commands import ban_user
from src.bridge_policy import BridgePolicyService



def _policy_service(database, settings):
    """Build the explicit policy dependency used by production composition."""
    return BridgePolicyService(settings=settings, repository=database.bridge_policy_entries)

@pytest.mark.asyncio
async def test_ban_user_command_passes_user_and_guild_and_returns_ephemeral(command_tree, interaction, database) -> None:
    """The adapter passes Discord caller/guild context into runtime policy."""
    settings = SimpleNamespace(discord_guild_allowlist=[], bridge_super_admin_user_ids=[])
    database.local_communities.get_local_community_by_slug.return_value = SimpleNamespace(
        id=1,
        slug="cats",
        discord_guild_id=99999,
        created_by_discord_user_id="1234567890",
        status="active",
    )
    database.community_actor_bans.get_active_ban_by_handle.return_value = None

    ban_user.register(command_tree, database, settings, _policy_service(database, settings))
    command = command_tree.commands["ban-user"]
    await command.callback(interaction, "cats", "alice@example.com", "spam")

    database.management_actions.create_or_reactivate_ban.assert_called_once_with(
        actor_discord_user_id="1234567890",
        local_community_id=1,
        actor_handle="alice@example.com",
        actor_url=None,
        reason="spam",
    )
    interaction.response.send_message.assert_awaited_once_with(
        "Banned alice@example.com from community cats.\nReason: spam",
        ephemeral=True,
    )


@pytest.mark.asyncio
async def test_ban_user_command_rejection_stays_ephemeral(command_tree, interaction, database) -> None:
    """Runtime rejection details stay private in the invoking user's response."""
    settings = SimpleNamespace(discord_guild_allowlist=[], bridge_super_admin_user_ids=[])
    database.local_communities.get_local_community_by_slug.return_value = SimpleNamespace(
        id=1,
        slug="cats",
        discord_guild_id=99999,
        created_by_discord_user_id="someone-else",
        status="active",
    )

    ban_user.register(command_tree, database, settings, _policy_service(database, settings))
    command = command_tree.commands["ban-user"]
    await command.callback(interaction, "cats", "alice@example.com", "spam")

    interaction.response.send_message.assert_awaited_once_with(
        "You are not allowed to manage this local community.",
        ephemeral=True,
    )
    database.management_actions.create_or_reactivate_ban.assert_not_called()


@pytest.mark.asyncio
async def test_ban_community_autocomplete_owner_sees_owned_current_guild(interaction, database) -> None:
    """Owner autocomplete is scoped to owned active communities in this guild."""
    settings = SimpleNamespace(discord_guild_allowlist=[], bridge_super_admin_user_ids=[])
    database.local_communities.list_active_local_communities_owned_by_user_in_guild.return_value = [
        SimpleNamespace(slug="cats", display_name="Cats", discord_guild_id=99999),
    ]

    choices = await ban_user._ban_community_autocomplete(database, settings, _policy_service(database, settings))(interaction, "cat")

    assert [(choice.name, choice.value) for choice in choices] == [("cats — Cats", "cats")]
    database.local_communities.list_active_local_communities_owned_by_user_in_guild.assert_called_once_with(
        discord_guild_id=99999,
        created_by_discord_user_id="1234567890",
    )
    database.local_communities.list_active_local_communities.assert_not_called()


@pytest.mark.asyncio
async def test_ban_community_autocomplete_super_admin_sees_all_guilds(interaction, database) -> None:
    """Super-admin autocomplete lists active communities across guilds."""
    settings = SimpleNamespace(discord_guild_allowlist=[], bridge_super_admin_user_ids=["1234567890"])
    database.local_communities.list_active_local_communities.return_value = [
        SimpleNamespace(slug="cats", display_name="Cats", discord_guild_id=10),
        SimpleNamespace(slug="dogs", display_name="Dogs", discord_guild_id=20),
    ]

    choices = await ban_user._ban_community_autocomplete(database, settings, _policy_service(database, settings))(interaction, "")

    assert [(choice.name, choice.value) for choice in choices] == [
        ("cats — Cats — guild 10", "cats"),
        ("dogs — Dogs — guild 20", "dogs"),
    ]
    database.local_communities.list_active_local_communities.assert_called_once_with()


@pytest.mark.asyncio
async def test_ban_community_autocomplete_filters_by_slug_or_display_name(interaction, database) -> None:
    """Typed text matches either stable slug or human-readable display name."""
    settings = SimpleNamespace(discord_guild_allowlist=[], bridge_super_admin_user_ids=[])
    database.local_communities.list_active_local_communities_owned_by_user_in_guild.return_value = [
        SimpleNamespace(slug="cats", display_name="Cats", discord_guild_id=99999),
        SimpleNamespace(slug="bird-watch", display_name="Bird Watch", discord_guild_id=99999),
        SimpleNamespace(slug="dogs", display_name="Dogs", discord_guild_id=99999),
    ]

    choices = await ban_user._ban_community_autocomplete(database, settings, _policy_service(database, settings))(interaction, "bird")

    assert [(choice.name, choice.value) for choice in choices] == [("bird-watch — Bird Watch", "bird-watch")]


@pytest.mark.asyncio
async def test_ban_community_autocomplete_caps_at_twenty_five(interaction, database) -> None:
    """Discord autocomplete choices are capped at 25 entries."""
    settings = SimpleNamespace(discord_guild_allowlist=[], bridge_super_admin_user_ids=["1234567890"])
    database.local_communities.list_active_local_communities.return_value = [
        SimpleNamespace(slug=f"community-{index:02d}", display_name=f"Community {index:02d}", discord_guild_id=99999)
        for index in range(30)
    ]

    choices = await ban_user._ban_community_autocomplete(database, settings, _policy_service(database, settings))(interaction, "")

    assert len(choices) == 25
    assert choices[0].value == "community-00"
    assert choices[-1].value == "community-24"


@pytest.mark.asyncio
async def test_ban_community_autocomplete_returns_empty_for_guildless_owner(interaction, database) -> None:
    """Non-admin owners need guild context before autocomplete can list rows."""
    settings = SimpleNamespace(discord_guild_allowlist=[], bridge_super_admin_user_ids=[])
    interaction.guild_id = None

    choices = await ban_user._ban_community_autocomplete(database, settings, _policy_service(database, settings))(interaction, "")

    assert choices == []
    database.local_communities.list_active_local_communities_owned_by_user_in_guild.assert_not_called()
    database.local_communities.list_active_local_communities.assert_not_called()


@pytest.mark.asyncio
async def test_ban_community_autocomplete_supports_guildless_super_admin(interaction, database) -> None:
    """Super-admin autocomplete is not tied to a single guild context."""
    settings = SimpleNamespace(discord_guild_allowlist=[], bridge_super_admin_user_ids=["1234567890"])
    interaction.guild_id = None
    database.local_communities.list_active_local_communities.return_value = [
        SimpleNamespace(slug="cats", display_name="Cats", discord_guild_id=10),
    ]

    choices = await ban_user._ban_community_autocomplete(database, settings, _policy_service(database, settings))(interaction, "")

    assert choices == []
    database.local_communities.list_active_local_communities.assert_not_called()


@pytest.mark.asyncio
async def test_ban_community_autocomplete_returns_empty_on_repository_error(interaction, database) -> None:
    """Autocomplete failures are caught so Discord receives an empty list."""
    settings = SimpleNamespace(discord_guild_allowlist=[], bridge_super_admin_user_ids=[])
    database.local_communities.list_active_local_communities_owned_by_user_in_guild.side_effect = RuntimeError(
        "database unavailable"
    )

    choices = await ban_user._ban_community_autocomplete(database, settings, _policy_service(database, settings))(interaction, "")

    assert choices == []
