"""Operation-level tests for the subscribe channel lifecycle."""

from __future__ import annotations
from src.bridge_policy import BridgePolicyService

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
    database.bridge_policy_entries.list_all_active.return_value = []
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
            policy_service=BridgePolicyService(
                settings=SimpleNamespace(), repository=database.bridge_policy_entries
            ),
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
    database.bridge_policy_entries.list_all_active.return_value = []
    database.users.get_user_by_discord_user_id.return_value = SimpleNamespace(id=1)
    database.remote_subscriptions.get_subscription_by_channel.return_value = (
        SimpleNamespace(
            status="accepted",
            community_handle=f"!hackers@{LEMMY_EXAMPLE_DOMAIN}",
            lemmy_community_name="hackers",
            lemmy_community_actor_id=community_actor_url,
        )
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
            policy_service=BridgePolicyService(
                settings=SimpleNamespace(), repository=database.bridge_policy_entries
            ),
        ),
    )

    assert result.applied is False
    assert result.reason == "channel_subscription_already_accepted"
    assert (
        result.message
        == f"Channel <#123> is already subscribed to **!hackers@{LEMMY_EXAMPLE_DOMAIN}**."
    )
    fedify_gateway.follow_community.assert_not_awaited()
    database.remote_subscriptions.create_subscription.assert_not_called()


@pytest.mark.asyncio
async def test_subscribe_operation_rejects_pending_subscription() -> None:
    """Pending subscriptions surface their waiting state without a second follow."""
    community_actor_url = f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers"
    database = Mock()
    database.bridge_policy_entries.list_all_active.return_value = []
    database.users.get_user_by_discord_user_id.return_value = SimpleNamespace(id=1)
    database.remote_subscriptions.get_subscription_by_channel.return_value = (
        SimpleNamespace(
            status="pending",
            community_handle=f"!hackers@{LEMMY_EXAMPLE_DOMAIN}",
            lemmy_community_name="hackers",
            lemmy_community_actor_id=community_actor_url,
        )
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
            policy_service=BridgePolicyService(
                settings=SimpleNamespace(), repository=database.bridge_policy_entries
            ),
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
@pytest.mark.parametrize("follow_status", [None, "accepted", "pending", "failed"])
async def test_subscribe_persists_domain_and_claim_with_shared_follow(
    tmp_path, follow_status
):
    """A moderator subscribes once; existing AP state is reused or retried in place."""
    from src.db import Database

    database = Database(f"sqlite:///{tmp_path / 'subscribe.db'}")
    database.migrate()
    actor = f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers"
    user_actor = f"https://{BRIDGE_EXAMPLE_DOMAIN}/actors/users/moderator"
    database.users.create_user(
        discord_user_id="1234567890",
        activitypub_username="moderator",
        actor_url=user_actor,
        inbox_url=user_actor + "/inbox",
        outbox_url=user_actor + "/outbox",
        followers_url=user_actor + "/followers",
        public_key_pem="public",
        private_key_pem="private",
    )
    old_follow = None
    old_subscription = None
    if follow_status is not None:
        old_follow = database.bridge_actor_follows.create_bridge_actor_follow(
            community_actor_id=actor,
            follow_activity_id="https://bridge.test/follow/original",
            community_inbox_url=actor + "/inbox",
            status=follow_status,
        )
    if follow_status == "failed":
        old_subscription = database.remote_subscriptions.create_subscription(
            discord_channel_id=123,
            lemmy_community_actor_id=actor,
            lemmy_community_name="hackers",
            lemmy_community_id=777,
            status="failed",
        )
    gateway = AsyncMock()
    gateway.follow_community.return_value = SimpleNamespace(
        community_actor_url=actor,
        community_inbox_url=actor + "/inbox",
        follow_activity_id="https://bridge.test/follow/new",
    )
    result = await run_operation_definition_async(
        subscribe_operation,
        SubscribeInput(
            database=database,
            fedify_gateway=gateway,
            discord_user_id="1234567890",
            channel_id=123,
            channel_mention="<#123>",
            actor_id=actor,
            community_name="hackers",
            numeric_id=777,
            community_handle=f"!hackers@{LEMMY_EXAMPLE_DOMAIN}",
            policy_service=BridgePolicyService(
                settings=SimpleNamespace(), repository=database.bridge_policy_entries
            ),
        ),
    )
    assert result.applied is True
    subscription = database.remote_subscriptions.get_subscription_by_channel(123)
    follow = database.bridge_actor_follows.get_bridge_actor_follow(actor)
    assert subscription.status == (
        "accepted" if follow_status == "accepted" else "pending"
    )
    assert subscription.follow_activity_id == follow.follow_activity_id
    assert (
        database.platform_surfaces.resolve_claim(
            "discord-primary", "123"
        ).remote_subscription_id
        == subscription.id
    )
    assert len(database.remote_subscriptions.list_subscriptions()) == 1
    assert len(database.bridge_actor_follows.list_bridge_actor_follows()) == 1
    if old_follow is not None:
        assert follow.id == old_follow.id
    if old_subscription is not None:
        assert subscription.id == old_subscription.id
    if follow_status in {"accepted", "pending"}:
        gateway.follow_community.assert_not_awaited()
    else:
        gateway.follow_community.assert_awaited_once_with(actor)
    database.close()


@pytest.mark.asyncio
async def test_first_failed_dispatch_records_one_owned_failed_subscription(tmp_path):
    """Network failure on a first subscription leaves one durable occupied forum."""
    from src.db import Database

    database = Database(f"sqlite:///{tmp_path / 'failed.db'}")
    database.migrate()
    actor = f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers"
    user_actor = f"https://{BRIDGE_EXAMPLE_DOMAIN}/actors/users/moderator"
    database.users.create_user(
        discord_user_id="1234567890",
        activitypub_username="moderator",
        actor_url=user_actor,
        inbox_url=user_actor + "/inbox",
        outbox_url=user_actor + "/outbox",
        followers_url=user_actor + "/followers",
        public_key_pem="public",
        private_key_pem="private",
    )
    gateway = AsyncMock()
    gateway.follow_community.side_effect = RuntimeError("network failure")
    result = await run_operation_definition_async(
        subscribe_operation,
        SubscribeInput(
            database=database,
            fedify_gateway=gateway,
            discord_user_id="1234567890",
            channel_id=123,
            channel_mention="<#123>",
            actor_id=actor,
            community_name="hackers",
            numeric_id=777,
            community_handle=f"!hackers@{LEMMY_EXAMPLE_DOMAIN}",
            policy_service=BridgePolicyService(
                settings=SimpleNamespace(), repository=database.bridge_policy_entries
            ),
        ),
    )
    assert result.applied is False and result.reason == "follow_dispatch_failed"
    subscription = database.remote_subscriptions.get_subscription_by_channel(123)
    assert subscription.status == "failed"
    assert (
        database.platform_surfaces.resolve_claim(
            "discord-primary", "123"
        ).remote_subscription_id
        == subscription.id
    )
    assert (
        database.bridge_actor_follows.get_bridge_actor_follow(actor).status == "failed"
    )
    assert len(database.remote_subscriptions.list_subscriptions()) == 1
    database.close()
