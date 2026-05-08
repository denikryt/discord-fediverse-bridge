from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.commands import subscribe
from tests.constants import BRIDGE_EXAMPLE_DOMAIN, LEMMY_EXAMPLE_DOMAIN


@pytest.mark.asyncio
async def test_subscribe_channel_success(
    command_tree, interaction, forum_channel, database, lemmy, fedify_gateway
):
    # A successful subscription should resolve the community, send a real
    # bridge Follow, persist the pending lifecycle state, and return the
    # moderator-facing pending message.
    community_actor_url = f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers"
    database.get_subscription_by_channel.return_value = None
    lemmy.resolve_community_id.return_value = 777
    fedify_gateway.follow_community.return_value = SimpleNamespace(
        community_actor_url=community_actor_url,
        community_inbox_url=f"{community_actor_url}/inbox",
        follow_activity_id=f"https://{BRIDGE_EXAMPLE_DOMAIN}/activities/follow/1",
    )

    subscribe.register(command_tree, database, lemmy, fedify_gateway)

    command = command_tree.commands["subscribe-channel"]
    await command.callback(
        interaction,
        forum_channel,
        f"{community_actor_url}|hackers|",
    )

    database.get_subscription_by_channel.assert_called_once_with(forum_channel.id)
    lemmy.resolve_community_id.assert_awaited_once_with(name="hackers")
    database.create_subscription.assert_called_once_with(
        discord_channel_id=forum_channel.id,
        lemmy_community_actor_id=community_actor_url,
        lemmy_community_name="hackers",
        lemmy_community_id=777,
        community_handle=f"!hackers@{LEMMY_EXAMPLE_DOMAIN}",
        community_inbox_url=f"{community_actor_url}/inbox",
        follow_activity_id=f"https://{BRIDGE_EXAMPLE_DOMAIN}/activities/follow/1",
        status="pending",
    )
    fedify_gateway.follow_community.assert_awaited_once_with(community_actor_url)
    send_call = interaction.response.send_message.await_args
    assert send_call.args == (
        "Sent a bridge follow for <#12345> -> **hackers**. Waiting for federation acceptance.",
    )
    assert send_call.kwargs.get("ephemeral", False) is False


@pytest.mark.asyncio
async def test_subscribe_channel_rejects_duplicate_accepted(
    command_tree, interaction, forum_channel, database, lemmy, fedify_gateway
):
    # Accepted subscriptions do not trigger a second Follow and return an
    # ephemeral message that the channel is already active.
    database.get_subscription_by_channel.return_value = SimpleNamespace(
        status="accepted",
        community_handle=f"!hackers@{LEMMY_EXAMPLE_DOMAIN}",
        lemmy_community_name="hackers",
        lemmy_community_actor_id=f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers",
    )

    subscribe.register(command_tree, database, lemmy, fedify_gateway)

    command = command_tree.commands["subscribe-channel"]
    await command.callback(
        interaction,
        forum_channel,
        f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers|hackers|777",
    )

    database.create_subscription.assert_not_called()
    lemmy.resolve_community_id.assert_not_awaited()
    fedify_gateway.follow_community.assert_not_awaited()
    interaction.response.send_message.assert_awaited_once_with(
        f"Channel <#12345> is already subscribed to **!hackers@{LEMMY_EXAMPLE_DOMAIN}**.",
        ephemeral=True,
    )


@pytest.mark.asyncio
async def test_subscribe_channel_rejects_duplicate_pending(
    command_tree, interaction, forum_channel, database, lemmy, fedify_gateway
):
    # Pending subscriptions do not trigger a second Follow and tell the
    # moderator that federation acceptance is still outstanding.
    database.get_subscription_by_channel.return_value = SimpleNamespace(
        status="pending",
        community_handle=f"!hackers@{LEMMY_EXAMPLE_DOMAIN}",
        lemmy_community_name="hackers",
        lemmy_community_actor_id=f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers",
    )

    subscribe.register(command_tree, database, lemmy, fedify_gateway)

    command = command_tree.commands["subscribe-channel"]
    await command.callback(
        interaction,
        forum_channel,
        f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers|hackers|777",
    )

    database.create_subscription.assert_not_called()
    fedify_gateway.follow_community.assert_not_awaited()
    interaction.response.send_message.assert_awaited_once_with(
        f"Channel <#12345> is still waiting for **!hackers@{LEMMY_EXAMPLE_DOMAIN}** to accept the bridge follow.",
        ephemeral=True,
    )


@pytest.mark.asyncio
async def test_subscribe_channel_rejects_when_community_resolution_fails(
    command_tree,
    interaction,
    forum_channel,
    database,
    lemmy,
    fedify_gateway,
):
    # Manual text input can omit the numeric ID, so a Lemmy resolution failure
    # must stop the flow before any DB mutation is attempted.
    database.get_subscription_by_channel.return_value = None
    lemmy.resolve_community_id.side_effect = RuntimeError("boom")

    subscribe.register(command_tree, database, lemmy, fedify_gateway)

    command = command_tree.commands["subscribe-channel"]
    await command.callback(
        interaction,
        forum_channel,
        f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers|hackers|",
    )

    database.create_subscription.assert_not_called()
    interaction.response.send_message.assert_awaited_once_with(
        "Could not resolve the Lemmy community ID. Please try again.",
        ephemeral=True,
    )


@pytest.mark.asyncio
async def test_subscribe_channel_marks_failed_when_follow_dispatch_fails(
    command_tree, interaction, forum_channel, database, lemmy, fedify_gateway
):
    # Follow dispatch failures must create a failed subscription row so retries
    # are explicit instead of leaving a fake pending state behind.
    community_actor_url = f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers"
    database.get_subscription_by_channel.return_value = None
    lemmy.resolve_community_id.return_value = 777
    fedify_gateway.follow_community.side_effect = RuntimeError("boom")

    subscribe.register(command_tree, database, lemmy, fedify_gateway)

    command = command_tree.commands["subscribe-channel"]
    await command.callback(
        interaction,
        forum_channel,
        f"{community_actor_url}|hackers|",
    )

    database.create_subscription.assert_called_once_with(
        discord_channel_id=forum_channel.id,
        lemmy_community_actor_id=community_actor_url,
        lemmy_community_name="hackers",
        lemmy_community_id=777,
        community_handle=f"!hackers@{LEMMY_EXAMPLE_DOMAIN}",
        community_inbox_url=None,
        follow_activity_id=None,
        status="failed",
    )
    interaction.response.send_message.assert_awaited_once_with(
        "Could not subscribe <#12345> to **hackers** because the bridge Follow request failed.",
        ephemeral=True,
    )


@pytest.mark.asyncio
async def test_subscribe_channel_retries_failed_subscription(
    command_tree, interaction, forum_channel, database, lemmy, fedify_gateway
):
    # Failed subscriptions are retriable. The old failed row is removed before
    # the new pending attempt is written.
    community_actor_url = f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers"
    database.get_subscription_by_channel.return_value = SimpleNamespace(
        status="failed",
        community_handle=f"!hackers@{LEMMY_EXAMPLE_DOMAIN}",
        lemmy_community_name="hackers",
        lemmy_community_actor_id=community_actor_url,
    )
    fedify_gateway.follow_community.return_value = SimpleNamespace(
        community_actor_url=community_actor_url,
        community_inbox_url=f"{community_actor_url}/inbox",
        follow_activity_id=f"https://{BRIDGE_EXAMPLE_DOMAIN}/activities/follow/2",
    )

    subscribe.register(command_tree, database, lemmy, fedify_gateway)

    command = command_tree.commands["subscribe-channel"]
    await command.callback(
        interaction,
        forum_channel,
        f"{community_actor_url}|hackers|777",
    )

    database.delete_subscription.assert_called_once_with(forum_channel.id)
    database.create_subscription.assert_called_once_with(
        discord_channel_id=forum_channel.id,
        lemmy_community_actor_id=community_actor_url,
        lemmy_community_name="hackers",
        lemmy_community_id=777,
        community_handle=f"!hackers@{LEMMY_EXAMPLE_DOMAIN}",
        community_inbox_url=f"{community_actor_url}/inbox",
        follow_activity_id=f"https://{BRIDGE_EXAMPLE_DOMAIN}/activities/follow/2",
        status="pending",
    )
