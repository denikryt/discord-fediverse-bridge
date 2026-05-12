"""Operation-level tests for the unsubscribe channel lifecycle."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from discordops import run_operation_definition_async

from src.operations import UnsubscribeInput, unsubscribe_operation
from tests_constants import BRIDGE_EXAMPLE_DOMAIN, LEMMY_EXAMPLE_DOMAIN


@pytest.mark.asyncio
async def test_unsubscribe_operation_rejects_missing_subscription() -> None:
    """Missing subscription stops before the delete call."""
    database = Mock()
    database.get_subscription_by_channel.return_value = None
    fedify_gateway = AsyncMock()

    result = await run_operation_definition_async(
        unsubscribe_operation,
        UnsubscribeInput(
            database=database,
            fedify_gateway=fedify_gateway,
            channel_id=123,
            channel_mention="<#123>",
        ),
    )

    assert result.applied is False
    assert result.reason == "channel_has_subscription"
    assert result.message == "Channel <#123> has no active subscription."
    database.delete_subscription.assert_not_called()


@pytest.mark.asyncio
async def test_unsubscribe_skips_unfollow_when_other_channels_remain() -> None:
    """Unsubscribe only deletes the channel row when other channels still subscribe."""
    community_actor_url = f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers"
    database = Mock()
    database.get_subscription_by_channel.return_value = SimpleNamespace(
        lemmy_community_name="hackers",
        lemmy_community_actor_id=community_actor_url,
    )
    database.delete_subscription.return_value = True
    # Another channel still subscribes to the same community after deletion.
    database.count_subscriptions_for_community.return_value = 1
    fedify_gateway = AsyncMock()

    result = await run_operation_definition_async(
        unsubscribe_operation,
        UnsubscribeInput(
            database=database,
            fedify_gateway=fedify_gateway,
            channel_id=123,
            channel_mention="<#123>",
        ),
    )

    assert result.applied is True
    assert result.message == "Unsubscribed <#123> from **hackers**."
    database.delete_subscription.assert_called_once_with(123)
    # No Undo(Follow) should be dispatched while other channels remain.
    fedify_gateway.unfollow_community.assert_not_awaited()
    database.delete_bridge_actor_follow.assert_not_called()


@pytest.mark.asyncio
async def test_unsubscribe_sends_unfollow_when_last_channel() -> None:
    """Last channel unsubscribing triggers Undo(Follow) and removes bridge follow row."""
    community_actor_url = f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers"
    follow_activity_id = f"https://{BRIDGE_EXAMPLE_DOMAIN}/activities/follow/1"
    database = Mock()
    database.get_subscription_by_channel.return_value = SimpleNamespace(
        lemmy_community_name="hackers",
        lemmy_community_actor_id=community_actor_url,
    )
    database.delete_subscription.return_value = True
    # No channels remain after deletion.
    database.count_subscriptions_for_community.return_value = 0
    database.get_bridge_actor_follow.return_value = SimpleNamespace(
        community_actor_id=community_actor_url,
        follow_activity_id=follow_activity_id,
        status="accepted",
    )
    fedify_gateway = AsyncMock()

    result = await run_operation_definition_async(
        unsubscribe_operation,
        UnsubscribeInput(
            database=database,
            fedify_gateway=fedify_gateway,
            channel_id=123,
            channel_mention="<#123>",
        ),
    )

    assert result.applied is True
    assert result.message == "Unsubscribed <#123> from **hackers**."
    database.delete_subscription.assert_called_once_with(123)
    # Undo(Follow) must be dispatched and the bridge follow row deleted.
    fedify_gateway.unfollow_community.assert_awaited_once_with(
        community_actor_url, follow_activity_id
    )
    database.delete_bridge_actor_follow.assert_called_once_with(community_actor_url)


@pytest.mark.asyncio
async def test_unsubscribe_last_channel_undo_failure_still_deletes_follow_row() -> None:
    """Undo delivery failure logs but does not prevent bridge follow row cleanup."""
    community_actor_url = f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers"
    follow_activity_id = f"https://{BRIDGE_EXAMPLE_DOMAIN}/activities/follow/1"
    database = Mock()
    database.get_subscription_by_channel.return_value = SimpleNamespace(
        lemmy_community_name="hackers",
        lemmy_community_actor_id=community_actor_url,
    )
    database.delete_subscription.return_value = True
    database.count_subscriptions_for_community.return_value = 0
    database.get_bridge_actor_follow.return_value = SimpleNamespace(
        community_actor_id=community_actor_url,
        follow_activity_id=follow_activity_id,
        status="accepted",
    )
    fedify_gateway = AsyncMock()
    fedify_gateway.unfollow_community.side_effect = RuntimeError("network error")

    result = await run_operation_definition_async(
        unsubscribe_operation,
        UnsubscribeInput(
            database=database,
            fedify_gateway=fedify_gateway,
            channel_id=123,
            channel_mention="<#123>",
        ),
    )

    # The unsubscribe result is still applied even if Undo delivery failed.
    assert result.applied is True
    # Bridge follow row must still be removed from the local DB.
    database.delete_bridge_actor_follow.assert_called_once_with(community_actor_url)
