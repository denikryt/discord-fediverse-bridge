from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.commands import unsubscribe
from tests_constants import LEMMY_EXAMPLE_DOMAIN


@pytest.mark.asyncio
async def test_unsubscribe_channel_success(command_tree, interaction, forum_channel, database):
    # The command shows the existing community label in the success message and
    # only deletes after confirming the mapping exists.
    database.get_subscription_by_channel.return_value = SimpleNamespace(
        lemmy_community_name="hackers",
        lemmy_community_actor_id=f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers",
    )

    unsubscribe.register(command_tree, database)

    command = command_tree.commands["unsubscribe-channel"]
    await command.callback(interaction, forum_channel)

    database.get_subscription_by_channel.assert_called_once_with(forum_channel.id)
    database.delete_subscription.assert_called_once_with(forum_channel.id)
    send_call = interaction.response.send_message.await_args
    assert send_call.args == ("Unsubscribed <#12345> from **hackers**.",)
    assert send_call.kwargs.get("ephemeral", False) is False


@pytest.mark.asyncio
async def test_unsubscribe_channel_rejects_missing_subscription(command_tree, interaction, forum_channel, database):
    # Missing mappings are reported ephemerally and must not trigger a delete call.
    database.get_subscription_by_channel.return_value = None

    unsubscribe.register(command_tree, database)

    command = command_tree.commands["unsubscribe-channel"]
    await command.callback(interaction, forum_channel)

    database.delete_subscription.assert_not_called()
    interaction.response.send_message.assert_awaited_once_with(
        "Channel <#12345> has no active subscription.",
        ephemeral=True,
    )
