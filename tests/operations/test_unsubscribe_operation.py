"""Operation-level tests for the unsubscribe channel lifecycle."""

from __future__ import annotations
from src.bridge_policy import BridgePolicyService

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from discordops import run_operation_definition_async

from src.operations import UnsubscribeInput, unsubscribe_operation
from src.fedify_gateway_client import UnfollowCommunityResult
from tests_constants import BRIDGE_EXAMPLE_DOMAIN, LEMMY_EXAMPLE_DOMAIN


@pytest.mark.asyncio
async def test_unsubscribe_operation_rejects_missing_subscription() -> None:
    """Missing subscription stops before the delete call."""
    database = Mock()
    database.bridge_policy_entries.list_all_active.return_value = []
    database.remote_subscriptions.get_subscription_by_channel.return_value = None
    fedify_gateway = AsyncMock()

    result = await run_operation_definition_async(
        unsubscribe_operation,
        UnsubscribeInput(
            database=database,
            fedify_gateway=fedify_gateway,
            channel_id=123,
            channel_mention="<#123>",

            policy_service=BridgePolicyService(settings=SimpleNamespace(), repository=database.bridge_policy_entries),),
    )

    assert result.applied is False
    assert result.reason == "channel_subscription_not_found"
    assert result.message == "Channel <#123> has no active subscription."
    database.remote_subscriptions.delete_subscription.assert_not_called()


@pytest.mark.asyncio
async def test_unsubscribe_skips_unfollow_when_other_channels_remain() -> None:
    """Unsubscribe only deletes the channel row when other channels still subscribe."""
    community_actor_url = f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers"
    database = Mock()
    database.bridge_policy_entries.list_all_active.return_value = []
    database.remote_subscriptions.get_subscription_by_channel.return_value = SimpleNamespace(
        lemmy_community_name="hackers",
        lemmy_community_actor_id=community_actor_url,
    )
    database.remote_subscriptions.delete_subscription.return_value = True
    # The count is read before deletion, so two rows means one other channel
    # will still remain subscribed afterward.
    database.remote_subscriptions.count_subscriptions_for_community.return_value = 2
    fedify_gateway = AsyncMock()

    result = await run_operation_definition_async(
        unsubscribe_operation,
        UnsubscribeInput(
            database=database,
            fedify_gateway=fedify_gateway,
            channel_id=123,
            channel_mention="<#123>",

            policy_service=BridgePolicyService(settings=SimpleNamespace(), repository=database.bridge_policy_entries),),
    )

    assert result.applied is True
    assert result.message == "Unsubscribed <#123> from **hackers**."
    database.remote_subscriptions.delete_subscription.assert_called_once_with(123)
    # No Undo(Follow) should be dispatched while other channels remain.
    fedify_gateway.unfollow_community.assert_not_awaited()
    database.bridge_actor_follows.delete_bridge_actor_follow.assert_not_called()


@pytest.mark.asyncio
async def test_unsubscribe_sends_unfollow_when_last_channel() -> None:
    """Last channel unsubscribing triggers Undo(Follow) and removes bridge follow row."""
    community_actor_url = f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers"
    follow_activity_id = f"https://{BRIDGE_EXAMPLE_DOMAIN}/activities/follow/1"
    database = Mock()
    database.bridge_policy_entries.list_all_active.return_value = []
    database.remote_subscriptions.get_subscription_by_channel.return_value = SimpleNamespace(
        lemmy_community_name="hackers",
        lemmy_community_actor_id=community_actor_url,
    )
    database.remote_subscriptions.delete_subscription.return_value = True
    # One row exists before deletion, so this unsubscribe is the last channel.
    database.remote_subscriptions.count_subscriptions_for_community.return_value = 1
    database.bridge_actor_follows.get_bridge_actor_follow.return_value = SimpleNamespace(
        community_actor_id=community_actor_url,
        follow_activity_id=follow_activity_id,
        status="accepted",
    )
    fedify_gateway = AsyncMock()
    fedify_gateway.unfollow_community.return_value = UnfollowCommunityResult(accepted=True)

    result = await run_operation_definition_async(
        unsubscribe_operation,
        UnsubscribeInput(
            database=database,
            fedify_gateway=fedify_gateway,
            channel_id=123,
            channel_mention="<#123>",

            policy_service=BridgePolicyService(settings=SimpleNamespace(), repository=database.bridge_policy_entries),),
    )

    assert result.applied is True
    assert result.message == "Unsubscribed <#123> from **hackers**."
    database.remote_subscriptions.delete_subscription.assert_called_once_with(123)
    # Undo(Follow) must be dispatched and the bridge follow row deleted.
    fedify_gateway.unfollow_community.assert_awaited_once_with(
        community_actor_url, follow_activity_id
    )
    database.bridge_actor_follows.delete_bridge_actor_follow.assert_called_once_with(community_actor_url)


@pytest.mark.asyncio
async def test_unsubscribe_last_channel_undo_failure_keeps_follow_row() -> None:
    """Remote cleanup failure must preserve the bridge follow row for retry."""
    community_actor_url = f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers"
    follow_activity_id = f"https://{BRIDGE_EXAMPLE_DOMAIN}/activities/follow/1"
    database = Mock()
    database.bridge_policy_entries.list_all_active.return_value = []
    database.remote_subscriptions.get_subscription_by_channel.return_value = SimpleNamespace(
        lemmy_community_name="hackers",
        lemmy_community_actor_id=community_actor_url,
    )
    database.remote_subscriptions.delete_subscription.return_value = True
    database.remote_subscriptions.count_subscriptions_for_community.return_value = 1
    database.bridge_actor_follows.get_bridge_actor_follow.return_value = SimpleNamespace(
        community_actor_id=community_actor_url,
        follow_activity_id=follow_activity_id,
        status="accepted",
    )
    fedify_gateway = AsyncMock()
    fedify_gateway.unfollow_community.return_value = UnfollowCommunityResult(
        accepted=False,
        error="network error",
    )

    result = await run_operation_definition_async(
        unsubscribe_operation,
        UnsubscribeInput(
            database=database,
            fedify_gateway=fedify_gateway,
            channel_id=123,
            channel_mention="<#123>",

            policy_service=BridgePolicyService(settings=SimpleNamespace(), repository=database.bridge_policy_entries),),
    )

    # The local channel row may be removed, but the overall unsubscribe is not
    # considered fully successful until remote cleanup succeeds.
    assert result.applied is False
    assert "remote Undo(Follow) failed" in result.message
    database.remote_subscriptions.delete_subscription.assert_called_once_with(123)
    database.bridge_actor_follows.delete_bridge_actor_follow.assert_not_called()


@pytest.mark.asyncio
async def test_unsubscribe_last_channel_with_missing_follow_id_keeps_local_state() -> None:
    """Missing follow state must block last-channel cleanup before deletion."""
    community_actor_url = f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers"
    database = Mock()
    database.bridge_policy_entries.list_all_active.return_value = []
    database.remote_subscriptions.get_subscription_by_channel.return_value = SimpleNamespace(
        lemmy_community_name="hackers",
        lemmy_community_actor_id=community_actor_url,
    )
    database.remote_subscriptions.count_subscriptions_for_community.return_value = 1
    database.bridge_actor_follows.get_bridge_actor_follow.return_value = SimpleNamespace(
        community_actor_id=community_actor_url,
        follow_activity_id=None,
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

            policy_service=BridgePolicyService(settings=SimpleNamespace(), repository=database.bridge_policy_entries),),
    )

    assert result.applied is False
    assert "follow activity id is missing" in result.message
    database.remote_subscriptions.delete_subscription.assert_not_called()
    database.bridge_actor_follows.delete_bridge_actor_follow.assert_not_called()
    fedify_gateway.unfollow_community.assert_not_awaited()
