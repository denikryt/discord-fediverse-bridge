"""Operation-level tests for the subscribe channel lifecycle."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import pytest

from discordops import run_operation_definition_async

from src.operations import SubscribeInput, subscribe_operation
from tests_constants import BRIDGE_EXAMPLE_DOMAIN, LEMMY_EXAMPLE_DOMAIN


@pytest.mark.asyncio
async def test_subscribe_operation_rejects_unregistered_user() -> None:
    """Subscribe requires a registered Discord user before any follow work runs."""
    community_actor_url = f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers"
    database = Mock()
    database.users.get_user_by_discord_user_id.return_value = None
    fedify_gateway = AsyncMock()

    result = await run_operation_definition_async(
        subscribe_operation,
        SubscribeInput(
            database=database,
            fedify_gateway=fedify_gateway,
            discord_user_id="1234567890",
            channel_id=123,
            channel_mention="<#123>",
            actor_id=community_actor_url,
            community_name="hackers",
            numeric_id=777,
            community_handle=f"!hackers@{LEMMY_EXAMPLE_DOMAIN}",
        ),
    )

    assert result.applied is False
    assert result.reason == "discord_user_not_registered"
    assert result.message == (
        "You must register with the bridge before using this command. Use `/register` first."
    )
    fedify_gateway.follow_community.assert_not_awaited()
    database.remote_subscriptions.create_subscription.assert_not_called()


@pytest.mark.asyncio
async def test_subscribe_operation_rejects_accepted_subscription() -> None:
    """Accepted subscriptions short-circuit before another bridge follow."""
    community_actor_url = f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers"
    database = Mock()
    database.users.get_user_by_discord_user_id.return_value = SimpleNamespace(id=1)
    database.remote_subscriptions.get_subscription_by_channel.return_value = SimpleNamespace(
        status="accepted",
        community_handle=f"!hackers@{LEMMY_EXAMPLE_DOMAIN}",
        lemmy_community_name="hackers",
        lemmy_community_actor_id=community_actor_url,
    )
    fedify_gateway = AsyncMock()

    result = await run_operation_definition_async(
        subscribe_operation,
        SubscribeInput(
            database=database,
            fedify_gateway=fedify_gateway,
            discord_user_id="1234567890",
            channel_id=123,
            channel_mention="<#123>",
            actor_id=community_actor_url,
            community_name="hackers",
            numeric_id=777,
            community_handle=f"!hackers@{LEMMY_EXAMPLE_DOMAIN}",
        ),
    )

    assert result.applied is False
    assert result.reason == "channel_subscription_already_accepted"
    assert result.message == f"Channel <#123> is already subscribed to **!hackers@{LEMMY_EXAMPLE_DOMAIN}**."
    fedify_gateway.follow_community.assert_not_awaited()
    database.remote_subscriptions.create_subscription.assert_not_called()


@pytest.mark.asyncio
async def test_subscribe_operation_rejects_pending_subscription() -> None:
    """Pending subscriptions surface their waiting state without a second follow."""
    community_actor_url = f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers"
    database = Mock()
    database.users.get_user_by_discord_user_id.return_value = SimpleNamespace(id=1)
    database.remote_subscriptions.get_subscription_by_channel.return_value = SimpleNamespace(
        status="pending",
        community_handle=f"!hackers@{LEMMY_EXAMPLE_DOMAIN}",
        lemmy_community_name="hackers",
        lemmy_community_actor_id=community_actor_url,
    )
    fedify_gateway = AsyncMock()

    result = await run_operation_definition_async(
        subscribe_operation,
        SubscribeInput(
            database=database,
            fedify_gateway=fedify_gateway,
            discord_user_id="1234567890",
            channel_id=123,
            channel_mention="<#123>",
            actor_id=community_actor_url,
            community_name="hackers",
            numeric_id=777,
            community_handle=f"!hackers@{LEMMY_EXAMPLE_DOMAIN}",
        ),
    )

    assert result.applied is False
    assert result.reason == "channel_subscription_already_pending"
    assert result.message == (
        f"Channel <#123> is still waiting for **!hackers@{LEMMY_EXAMPLE_DOMAIN}** to accept the bridge follow."
    )
    fedify_gateway.follow_community.assert_not_awaited()
    database.remote_subscriptions.create_subscription.assert_not_called()


@pytest.mark.asyncio
async def test_subscribe_creates_bridge_follow_when_none_exists() -> None:
    """First subscription for a community sends Follow and creates bridge_actor_follows row."""
    community_actor_url = f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers"
    database = Mock()
    database.users.get_user_by_discord_user_id.return_value = SimpleNamespace(id=1)
    database.remote_subscriptions.get_subscription_by_channel.return_value = None
    # No existing bridge-actor follow for this community.
    database.bridge_actor_follows.get_bridge_actor_follow.return_value = None
    fedify_gateway = AsyncMock()
    fedify_gateway.follow_community.return_value = SimpleNamespace(
        community_actor_url=community_actor_url,
        community_inbox_url=f"{community_actor_url}/inbox",
        follow_activity_id=f"https://{BRIDGE_EXAMPLE_DOMAIN}/activities/follow/1",
    )

    result = await run_operation_definition_async(
        subscribe_operation,
        SubscribeInput(
            database=database,
            fedify_gateway=fedify_gateway,
            discord_user_id="1234567890",
            channel_id=123,
            channel_mention="<#123>",
            actor_id=community_actor_url,
            community_name="hackers",
            numeric_id=777,
            community_handle=f"!hackers@{LEMMY_EXAMPLE_DOMAIN}",
        ),
    )

    assert result.applied is True
    assert "Waiting for federation acceptance" in result.message
    fedify_gateway.follow_community.assert_awaited_once_with(community_actor_url)
    # Both the bridge follow row and the channel subscription row must be created.
    database.bridge_actor_follows.create_bridge_actor_follow.assert_called_once_with(
        community_actor_id=community_actor_url,
        follow_activity_id=f"https://{BRIDGE_EXAMPLE_DOMAIN}/activities/follow/1",
        community_inbox_url=f"{community_actor_url}/inbox",
        status="pending",
    )
    database.remote_subscriptions.create_subscription.assert_called_once_with(
        discord_channel_id=123,
        discord_guild_id=None,
        lemmy_community_actor_id=community_actor_url,
        lemmy_community_name="hackers",
        lemmy_community_id=777,
        community_handle=f"!hackers@{LEMMY_EXAMPLE_DOMAIN}",
        community_inbox_url=f"{community_actor_url}/inbox",
        follow_activity_id=f"https://{BRIDGE_EXAMPLE_DOMAIN}/activities/follow/1",
        initiated_by_discord_user_id="1234567890",
        status="pending",
    )


@pytest.mark.asyncio
async def test_subscribe_reuses_existing_bridge_follow_when_accepted() -> None:
    """Second channel subscribing to an already-accepted community skips Follow."""
    community_actor_url = f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers"
    database = Mock()
    database.users.get_user_by_discord_user_id.return_value = SimpleNamespace(id=1)
    # No channel subscription for this specific channel yet.
    database.remote_subscriptions.get_subscription_by_channel.return_value = None
    # Bridge-actor follow already accepted for this community.
    database.bridge_actor_follows.get_bridge_actor_follow.return_value = SimpleNamespace(
        community_actor_id=community_actor_url,
        status="accepted",
        follow_activity_id=f"https://{BRIDGE_EXAMPLE_DOMAIN}/activities/follow/1",
        community_inbox_url=f"{community_actor_url}/inbox",
    )
    fedify_gateway = AsyncMock()

    result = await run_operation_definition_async(
        subscribe_operation,
        SubscribeInput(
            database=database,
            fedify_gateway=fedify_gateway,
            discord_user_id="1234567890",
            channel_id=456,
            channel_mention="<#456>",
            actor_id=community_actor_url,
            community_name="hackers",
            numeric_id=777,
            community_handle=f"!hackers@{LEMMY_EXAMPLE_DOMAIN}",
        ),
    )

    assert result.applied is True
    # No new Follow should be sent — bridge is already federated.
    fedify_gateway.follow_community.assert_not_awaited()
    # No new bridge_actor_follows row — existing one is reused.
    database.bridge_actor_follows.create_bridge_actor_follow.assert_not_called()
    # Only a channel subscription row is created, already as accepted.
    database.remote_subscriptions.create_subscription.assert_called_once_with(
        discord_channel_id=456,
        discord_guild_id=None,
        lemmy_community_actor_id=community_actor_url,
        lemmy_community_name="hackers",
        lemmy_community_id=777,
        community_handle=f"!hackers@{LEMMY_EXAMPLE_DOMAIN}",
        community_inbox_url=f"{community_actor_url}/inbox",
        follow_activity_id=f"https://{BRIDGE_EXAMPLE_DOMAIN}/activities/follow/1",
        initiated_by_discord_user_id="1234567890",
        status="accepted",
    )


@pytest.mark.asyncio
async def test_subscribe_reuses_existing_bridge_follow_when_pending() -> None:
    """Second channel subscribing to a pending-follow community also waits for Accept."""
    community_actor_url = f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers"
    database = Mock()
    database.users.get_user_by_discord_user_id.return_value = SimpleNamespace(id=1)
    database.remote_subscriptions.get_subscription_by_channel.return_value = None
    # Bridge-actor follow is still pending for this community.
    database.bridge_actor_follows.get_bridge_actor_follow.return_value = SimpleNamespace(
        community_actor_id=community_actor_url,
        status="pending",
        follow_activity_id=f"https://{BRIDGE_EXAMPLE_DOMAIN}/activities/follow/1",
        community_inbox_url=f"{community_actor_url}/inbox",
    )
    fedify_gateway = AsyncMock()

    result = await run_operation_definition_async(
        subscribe_operation,
        SubscribeInput(
            database=database,
            fedify_gateway=fedify_gateway,
            discord_user_id="9999",
            channel_id=789,
            channel_mention="<#789>",
            actor_id=community_actor_url,
            community_name="hackers",
            numeric_id=777,
            community_handle=f"!hackers@{LEMMY_EXAMPLE_DOMAIN}",
        ),
    )

    assert result.applied is True
    assert "Waiting for federation acceptance" in result.message
    # No second Follow dispatched — the in-flight one is shared.
    fedify_gateway.follow_community.assert_not_awaited()
    database.bridge_actor_follows.create_bridge_actor_follow.assert_not_called()
    # Channel subscription is created as pending — it will activate when Accept arrives.
    database.remote_subscriptions.create_subscription.assert_called_once_with(
        discord_channel_id=789,
        discord_guild_id=None,
        lemmy_community_actor_id=community_actor_url,
        lemmy_community_name="hackers",
        lemmy_community_id=777,
        community_handle=f"!hackers@{LEMMY_EXAMPLE_DOMAIN}",
        community_inbox_url=f"{community_actor_url}/inbox",
        follow_activity_id=f"https://{BRIDGE_EXAMPLE_DOMAIN}/activities/follow/1",
        initiated_by_discord_user_id="9999",
        status="pending",
    )


@pytest.mark.asyncio
async def test_subscribe_operation_marks_failed_when_follow_dispatch_fails() -> None:
    """Gateway follow failures become explicit failed rows for moderator retries."""
    community_actor_url = f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers"
    database = Mock()
    database.users.get_user_by_discord_user_id.return_value = SimpleNamespace(id=1)
    database.remote_subscriptions.get_subscription_by_channel.return_value = None
    database.bridge_actor_follows.get_bridge_actor_follow.return_value = None
    fedify_gateway = AsyncMock()
    fedify_gateway.follow_community.side_effect = RuntimeError("boom")

    result = await run_operation_definition_async(
        subscribe_operation,
        SubscribeInput(
            database=database,
            fedify_gateway=fedify_gateway,
            discord_user_id="1234567890",
            channel_id=123,
            channel_mention="<#123>",
            actor_id=community_actor_url,
            community_name="hackers",
            numeric_id=777,
            community_handle=f"!hackers@{LEMMY_EXAMPLE_DOMAIN}",
        ),
    )

    assert result.applied is False
    assert result.reason == "follow_dispatch_failed"
    assert result.message == "Could not subscribe <#123> to **hackers** because the bridge Follow request failed."
    # Both a failed bridge follow row and a failed channel subscription row are written.
    database.bridge_actor_follows.create_bridge_actor_follow.assert_called_once_with(
        community_actor_id=community_actor_url,
        follow_activity_id=None,
        community_inbox_url=None,
        status="failed",
    )
    database.remote_subscriptions.create_subscription.assert_called_once_with(
        discord_channel_id=123,
        discord_guild_id=None,
        lemmy_community_actor_id=community_actor_url,
        lemmy_community_name="hackers",
        lemmy_community_id=777,
        community_handle=f"!hackers@{LEMMY_EXAMPLE_DOMAIN}",
        community_inbox_url=None,
        follow_activity_id=None,
        status="failed",
    )


@pytest.mark.asyncio
async def test_subscribe_operation_retries_failed_subscription() -> None:
    """Failed rows are deleted before a new pending follow attempt is written."""
    community_actor_url = f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers"
    database = Mock()
    database.users.get_user_by_discord_user_id.return_value = SimpleNamespace(id=1)
    # The channel has an existing failed subscription.
    database.remote_subscriptions.get_subscription_by_channel.return_value = SimpleNamespace(
        status="failed",
        community_handle=f"!hackers@{LEMMY_EXAMPLE_DOMAIN}",
        lemmy_community_name="hackers",
        lemmy_community_actor_id=community_actor_url,
    )
    # The bridge follow is also failed.
    database.bridge_actor_follows.get_bridge_actor_follow.return_value = SimpleNamespace(
        community_actor_id=community_actor_url,
        status="failed",
        follow_activity_id=None,
        community_inbox_url=None,
    )
    fedify_gateway = AsyncMock()
    fedify_gateway.follow_community.return_value = SimpleNamespace(
        community_actor_url=community_actor_url,
        community_inbox_url=f"{community_actor_url}/inbox",
        follow_activity_id=f"https://{BRIDGE_EXAMPLE_DOMAIN}/activities/follow/2",
    )

    result = await run_operation_definition_async(
        subscribe_operation,
        SubscribeInput(
            database=database,
            fedify_gateway=fedify_gateway,
            discord_user_id="1234567890",
            channel_id=123,
            channel_mention="<#123>",
            actor_id=community_actor_url,
            community_name="hackers",
            numeric_id=777,
            community_handle=f"!hackers@{LEMMY_EXAMPLE_DOMAIN}",
        ),
    )

    assert result.applied is True
    # Both stale rows must be cleared.
    database.remote_subscriptions.delete_subscription.assert_called_once_with(123)
    database.bridge_actor_follows.delete_bridge_actor_follow.assert_called_once_with(community_actor_url)
    # Fresh bridge follow and channel subscription rows are created.
    database.bridge_actor_follows.create_bridge_actor_follow.assert_called_once_with(
        community_actor_id=community_actor_url,
        follow_activity_id=f"https://{BRIDGE_EXAMPLE_DOMAIN}/activities/follow/2",
        community_inbox_url=f"{community_actor_url}/inbox",
        status="pending",
    )
    database.remote_subscriptions.create_subscription.assert_called_once_with(
        discord_channel_id=123,
        discord_guild_id=None,
        lemmy_community_actor_id=community_actor_url,
        lemmy_community_name="hackers",
        lemmy_community_id=777,
        community_handle=f"!hackers@{LEMMY_EXAMPLE_DOMAIN}",
        community_inbox_url=f"{community_actor_url}/inbox",
        follow_activity_id=f"https://{BRIDGE_EXAMPLE_DOMAIN}/activities/follow/2",
        initiated_by_discord_user_id="1234567890",
        status="pending",
    )
