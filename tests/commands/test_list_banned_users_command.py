"""Discord command adapter tests for `/list-banned-users`."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.commands import list_banned_users
from src.bridge_policy import BridgePolicyService



def _policy_service(database, settings):
    """Build the explicit policy dependency used by production composition."""
    return BridgePolicyService(settings=settings, repository=database.bridge_policy_entries)

@pytest.mark.asyncio
async def test_list_banned_users_command_passes_user_and_guild_and_returns_ephemeral(command_tree, interaction, database) -> None:
    """The adapter keeps list output private and passes guild context."""
    settings = SimpleNamespace(discord_guild_allowlist=[], bridge_super_admin_user_ids=[])
    database.local_communities.get_local_community_by_slug.return_value = SimpleNamespace(
        id=1,
        slug="cats",
        discord_guild_id=99999,
        created_by_discord_user_id="111",
        status="active",
    )
    database.community_actor_bans.count_active_bans_for_community.return_value = 0

    list_banned_users.register(command_tree, database, settings, _policy_service(database, settings))
    command = command_tree.commands["list-banned-users"]
    await command.callback(interaction, "cats")

    interaction.response.send_message.assert_awaited_once_with(
        "Community cats has no active bans.",
        ephemeral=True,
    )


@pytest.mark.asyncio
async def test_list_banned_users_community_autocomplete_shows_current_guild_active_communities(interaction, database) -> None:
    """Public list autocomplete includes all current-guild active communities."""
    database.local_communities.list_active_local_communities_by_guild.return_value = [
        SimpleNamespace(slug="cats", display_name="Cats"),
        SimpleNamespace(slug="dogs", display_name="Dogs"),
    ]

    choices = await list_banned_users._list_community_autocomplete(
        database,
        (settings := SimpleNamespace(discord_guild_allowlist=[])),
        _policy_service(database, settings),
    )(interaction, "")

    assert [(choice.name, choice.value) for choice in choices] == [
        ("cats — Cats", "cats"),
        ("dogs — Dogs", "dogs"),
    ]
    database.local_communities.list_active_local_communities_by_guild.assert_called_once_with(
        discord_guild_id=99999,
    )


@pytest.mark.asyncio
async def test_list_banned_users_community_autocomplete_returns_empty_without_guild(interaction, database) -> None:
    """DM autocomplete cannot infer a current guild and returns no choices."""
    interaction.guild_id = None

    choices = await list_banned_users._list_community_autocomplete(
        database,
        (settings := SimpleNamespace(discord_guild_allowlist=[])),
        _policy_service(database, settings),
    )(interaction, "")

    assert choices == []
    database.local_communities.list_active_local_communities_by_guild.assert_not_called()


@pytest.mark.asyncio
async def test_list_banned_users_community_autocomplete_caps_at_twenty_five(interaction, database) -> None:
    """The public community autocomplete respects Discord's choice cap."""
    database.local_communities.list_active_local_communities_by_guild.return_value = [
        SimpleNamespace(slug=f"community{index:02d}", display_name=f"Community {index:02d}")
        for index in range(30)
    ]

    choices = await list_banned_users._list_community_autocomplete(
        database,
        (settings := SimpleNamespace(discord_guild_allowlist=[])),
        _policy_service(database, settings),
    )(interaction, "")

    assert len(choices) == 25
