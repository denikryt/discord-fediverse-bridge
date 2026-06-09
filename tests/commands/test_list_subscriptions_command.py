from __future__ import annotations

from types import SimpleNamespace

import discord
import pytest

from src.commands import list_subs
from src.bridge_policy import BridgePolicyService
from tests_constants import LEMMY_EXAMPLE_DOMAIN



def _policy_service(database, settings):
    """Build the explicit policy dependency used by production composition."""
    return BridgePolicyService(settings=settings, repository=database.bridge_policy_entries)

@pytest.mark.asyncio
async def test_list_subscriptions_rejects_empty_state(command_tree, interaction, database):
    # The empty-state branch should stay private to avoid spamming the channel
    # with administrative noise when nothing is configured yet.
    database.remote_subscriptions.get_subscriptions_by_guild.return_value = []
    database.local_subscribers.list_local_subscribers_by_guild.return_value = []

    settings = SimpleNamespace(discord_guild_allowlist=[])
    list_subs.register(command_tree, database, settings, _policy_service(database, settings))

    command = command_tree.commands["list-subscriptions"]
    await command.callback(interaction)

    database.remote_subscriptions.get_subscriptions_by_guild.assert_called_once_with(interaction.guild_id)
    interaction.response.send_message.assert_awaited_once_with(
        "No active subscriptions.",
        ephemeral=True,
    )


@pytest.mark.asyncio
async def test_list_subscriptions_returns_embed_with_expected_items(command_tree, interaction, database):
    # The adapter is responsible for presentation, so the test checks the embed
    # contract the user sees rather than internal list-building details.
    hackers_actor_url = f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers"
    void_actor_url = f"https://{LEMMY_EXAMPLE_DOMAIN}/c/void"
    database.remote_subscriptions.get_subscriptions_by_guild.return_value = [
        SimpleNamespace(
            discord_channel_id=111,
            lemmy_community_name="hackers",
            lemmy_community_actor_id=hackers_actor_url,
        ),
        SimpleNamespace(
            discord_channel_id=222,
            lemmy_community_name=None,
            lemmy_community_actor_id=void_actor_url,
        ),
    ]
    database.local_subscribers.list_local_subscribers_by_guild.return_value = []

    settings = SimpleNamespace(discord_guild_allowlist=[])
    list_subs.register(command_tree, database, settings, _policy_service(database, settings))

    command = command_tree.commands["list-subscriptions"]
    await command.callback(interaction)

    send_call = interaction.response.send_message.await_args
    embed = send_call.kwargs["embed"]

    assert isinstance(embed, discord.Embed)
    assert embed.title == "Active Subscriptions"
    assert "Remote community subscriptions" in embed.description
    assert "• <#111> → **hackers@lemmy.example**" in embed.description
    assert "• <#222> → **void@lemmy.example**" in embed.description
    assert embed.footer.text == "2 subscription(s)"
    assert send_call.kwargs["ephemeral"] is True
