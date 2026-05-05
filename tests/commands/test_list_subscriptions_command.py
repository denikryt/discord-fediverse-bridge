from __future__ import annotations

from types import SimpleNamespace

import discord
import pytest

from src.commands import list_subs


@pytest.mark.asyncio
async def test_list_subscriptions_rejects_empty_state(command_tree, interaction, database):
    # The empty-state branch should stay private to avoid spamming the channel
    # with administrative noise when nothing is configured yet.
    database.get_all_subscriptions.return_value = []

    list_subs.register(command_tree, database)

    command = command_tree.commands["list-subscriptions"]
    await command.callback(interaction)

    database.get_all_subscriptions.assert_called_once_with()
    interaction.response.send_message.assert_awaited_once_with(
        "No active subscriptions.",
        ephemeral=True,
    )


@pytest.mark.asyncio
async def test_list_subscriptions_returns_embed_with_expected_items(command_tree, interaction, database):
    # The adapter is responsible for presentation, so the test checks the embed
    # contract the user sees rather than internal list-building details.
    database.get_all_subscriptions.return_value = [
        SimpleNamespace(
            discord_channel_id=111,
            lemmy_community_name="hackers",
            lemmy_community_actor_id="https://lemmy.example/c/hackers",
        ),
        SimpleNamespace(
            discord_channel_id=222,
            lemmy_community_name=None,
            lemmy_community_actor_id="https://lemmy.example/c/void",
        ),
    ]

    list_subs.register(command_tree, database)

    command = command_tree.commands["list-subscriptions"]
    await command.callback(interaction)

    send_call = interaction.response.send_message.await_args
    embed = send_call.kwargs["embed"]

    assert isinstance(embed, discord.Embed)
    assert embed.title == "Active Subscriptions"
    assert "• <#111> → **hackers**" in embed.description
    assert "• <#222> → **https://lemmy.example/c/void**" in embed.description
    assert embed.footer.text == "2 subscription(s)"
    assert send_call.kwargs["ephemeral"] is True
