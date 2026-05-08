"""Operation-level tests for the subscribe channel lifecycle."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from discordops import run_operation_definition_async

from src.operations import SubscribeInput, subscribe_operation
from tests_constants import BRIDGE_EXAMPLE_DOMAIN, LEMMY_EXAMPLE_DOMAIN


@pytest.mark.asyncio
async def test_subscribe_operation_rejects_unregistered_user() -> None:
    """Subscribe requires a registered Discord user before any follow work runs."""
    community_actor_url = f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers"
    database = Mock()
    database.get_user_by_discord_user_id.return_value = None
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
    assert result.reason == "discord_user_is_registered"
    assert result.message == (
        "You must register with the bridge before subscribing a channel. Use `/register` first."
    )
    fedify_gateway.follow_community.assert_not_awaited()
    database.create_subscription.assert_not_called()


@pytest.mark.asyncio
async def test_subscribe_operation_rejects_accepted_subscription() -> None:
    """Accepted subscriptions short-circuit before another bridge follow."""
    community_actor_url = f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers"
    database = Mock()
    database.get_user_by_discord_user_id.return_value = SimpleNamespace(id=1)
    database.get_subscription_by_channel.return_value = SimpleNamespace(
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
    assert result.reason == "channel_subscription_not_accepted"
    assert result.message == f"Channel <#123> is already subscribed to **!hackers@{LEMMY_EXAMPLE_DOMAIN}**."
    fedify_gateway.follow_community.assert_not_awaited()
    database.create_subscription.assert_not_called()


@pytest.mark.asyncio
async def test_subscribe_operation_rejects_pending_subscription() -> None:
    """Pending subscriptions surface their waiting state without a second follow."""
    community_actor_url = f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers"
    database = Mock()
    database.get_user_by_discord_user_id.return_value = SimpleNamespace(id=1)
    database.get_subscription_by_channel.return_value = SimpleNamespace(
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
    assert result.reason == "channel_subscription_not_pending"
    assert result.message == (
        f"Channel <#123> is still waiting for **!hackers@{LEMMY_EXAMPLE_DOMAIN}** to accept the bridge follow."
    )
    fedify_gateway.follow_community.assert_not_awaited()
    database.create_subscription.assert_not_called()


@pytest.mark.asyncio
async def test_subscribe_operation_creates_pending_subscription_on_success() -> None:
    """Successful subscribe writes one pending row after the gateway follow succeeds."""
    community_actor_url = f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers"
    database = Mock()
    database.get_user_by_discord_user_id.return_value = SimpleNamespace(id=1)
    database.get_subscription_by_channel.return_value = None
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
    assert result.message == "Sent a bridge follow for <#123> -> **hackers**. Waiting for federation acceptance."
    fedify_gateway.follow_community.assert_awaited_once_with(community_actor_url)
    database.create_subscription.assert_called_once_with(
        discord_channel_id=123,
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
async def test_subscribe_operation_marks_failed_when_follow_dispatch_fails() -> None:
    """Gateway follow failures become explicit failed rows for moderator retries."""
    community_actor_url = f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers"
    database = Mock()
    database.get_user_by_discord_user_id.return_value = SimpleNamespace(id=1)
    database.get_subscription_by_channel.return_value = None
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
    database.create_subscription.assert_called_once_with(
        discord_channel_id=123,
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
    database.get_user_by_discord_user_id.return_value = SimpleNamespace(id=1)
    database.get_subscription_by_channel.return_value = SimpleNamespace(
        status="failed",
        community_handle=f"!hackers@{LEMMY_EXAMPLE_DOMAIN}",
        lemmy_community_name="hackers",
        lemmy_community_actor_id=community_actor_url,
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
    database.delete_subscription.assert_called_once_with(123)
    database.create_subscription.assert_called_once_with(
        discord_channel_id=123,
        lemmy_community_actor_id=community_actor_url,
        lemmy_community_name="hackers",
        lemmy_community_id=777,
        community_handle=f"!hackers@{LEMMY_EXAMPLE_DOMAIN}",
        community_inbox_url=f"{community_actor_url}/inbox",
        follow_activity_id=f"https://{BRIDGE_EXAMPLE_DOMAIN}/activities/follow/2",
        initiated_by_discord_user_id="1234567890",
        status="pending",
    )
