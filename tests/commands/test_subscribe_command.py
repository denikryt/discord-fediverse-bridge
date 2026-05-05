from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.commands import subscribe


@pytest.mark.asyncio
async def test_subscribe_channel_success(command_tree, interaction, forum_channel, database, lemmy):
    # A successful subscription should resolve the community ID when needed,
    # write one DB row, and send a public confirmation message.
    database.get_subscription_by_channel.return_value = None
    lemmy.resolve_community_id.return_value = 777

    subscribe.register(command_tree, database, lemmy)

    command = command_tree.commands["subscribe-channel"]
    await command.callback(
        interaction,
        forum_channel,
        "https://lemmy.example/c/hackers|hackers|",
    )

    database.get_subscription_by_channel.assert_called_once_with(forum_channel.id)
    lemmy.resolve_community_id.assert_awaited_once_with(name="hackers")
    database.create_subscription.assert_called_once_with(
        discord_channel_id=forum_channel.id,
        lemmy_community_actor_id="https://lemmy.example/c/hackers",
        lemmy_community_name="hackers",
        lemmy_community_id=777,
    )
    send_call = interaction.response.send_message.await_args
    assert send_call.args == ("Subscribed <#12345> to **hackers**.",)
    assert send_call.kwargs.get("ephemeral", False) is False


@pytest.mark.asyncio
async def test_subscribe_channel_rejects_duplicate(command_tree, interaction, forum_channel, database, lemmy):
    # Duplicate subscriptions stay read-only and tell the user which community
    # already owns the channel mapping.
    database.get_subscription_by_channel.return_value = SimpleNamespace(
        lemmy_community_name="hackers",
        lemmy_community_actor_id="https://lemmy.example/c/hackers",
    )

    subscribe.register(command_tree, database, lemmy)

    command = command_tree.commands["subscribe-channel"]
    await command.callback(
        interaction,
        forum_channel,
        "https://lemmy.example/c/hackers|hackers|777",
    )

    database.create_subscription.assert_not_called()
    lemmy.resolve_community_id.assert_not_awaited()
    interaction.response.send_message.assert_awaited_once_with(
        "Channel <#12345> is already subscribed to **hackers**. Use `/unsubscribe-channel` first.",
        ephemeral=True,
    )


@pytest.mark.asyncio
async def test_subscribe_channel_rejects_when_community_resolution_fails(
    command_tree,
    interaction,
    forum_channel,
    database,
    lemmy,
):
    # Manual text input can omit the numeric ID, so a Lemmy resolution failure
    # must stop the flow before any DB mutation is attempted.
    database.get_subscription_by_channel.return_value = None
    lemmy.resolve_community_id.side_effect = RuntimeError("boom")

    subscribe.register(command_tree, database, lemmy)

    command = command_tree.commands["subscribe-channel"]
    await command.callback(
        interaction,
        forum_channel,
        "https://lemmy.example/c/hackers|hackers|",
    )

    database.create_subscription.assert_not_called()
    interaction.response.send_message.assert_awaited_once_with(
        "Could not resolve the Lemmy community ID. Please try again.",
        ephemeral=True,
    )
