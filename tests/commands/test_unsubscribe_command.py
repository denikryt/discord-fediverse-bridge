from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.commands import unsubscribe
from tests_constants import BRIDGE_EXAMPLE_DOMAIN, LEMMY_EXAMPLE_DOMAIN


@pytest.mark.asyncio
async def test_unsubscribe_channel_success(command_tree, interaction, forum_channel, database, fedify_gateway):
    # The command shows the existing community label in the success message and
    # only deletes after confirming the mapping exists.
    community_actor_url = f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers"
    database.remote_subscriptions.get_subscription_by_channel.return_value = SimpleNamespace(
        lemmy_community_name="hackers",
        lemmy_community_actor_id=community_actor_url,
    )
    database.remote_subscriptions.delete_subscription.return_value = True
    # The operation reads the count before deletion, so two rows means one
    # other channel still remains subscribed afterward.
    database.remote_subscriptions.count_subscriptions_for_community.return_value = 2

    unsubscribe.register(command_tree, database, fedify_gateway)

    command = command_tree.commands["unsubscribe-channel"]
    await command.callback(interaction, forum_channel)

    assert database.remote_subscriptions.get_subscription_by_channel.call_count >= 1
    database.remote_subscriptions.delete_subscription.assert_called_once_with(forum_channel.id)
    send_call = interaction.response.send_message.await_args
    assert send_call.args == ("Unsubscribed <#12345> from **hackers**.",)
    assert send_call.kwargs.get("ephemeral", False) is False


@pytest.mark.asyncio
async def test_unsubscribe_channel_rejects_missing_subscription(command_tree, interaction, forum_channel, database, fedify_gateway):
    # Missing mappings are reported ephemerally and must not trigger a delete call.
    database.remote_subscriptions.get_subscription_by_channel.return_value = None
    database.local_subscribers.get_local_subscriber_by_channel.return_value = None

    unsubscribe.register(command_tree, database, fedify_gateway)

    command = command_tree.commands["unsubscribe-channel"]
    await command.callback(interaction, forum_channel)

    database.remote_subscriptions.delete_subscription.assert_not_called()
    interaction.response.send_message.assert_awaited_once_with(
        "Channel <#12345> has no active subscription.",
        ephemeral=True,
    )
