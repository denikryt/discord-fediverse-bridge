"""Behavior scenarios for community subscription lifecycle decisions."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.activitypub_handlers import dispatch_activitypub_event
from src.activitypub_models import FollowLifecycleEvent
from src.commands import subscribe, unsubscribe
from src.db import Database
from tests_constants import BRIDGE_EXAMPLE_DOMAIN, LEMMY_EXAMPLE_DOMAIN


def _database(tmp_path: Path) -> Database:
    """Create one real SQLite repository for subscription behavior scenarios."""
    database = Database(f"sqlite:///{tmp_path / 'behavior-subscriptions.db'}")
    database.create_all()
    return database


def _community_actor_url() -> str:
    """Return the shared fake community actor used across subscription tests."""
    return f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers"


def _register_user(database: Database, discord_user_id: str = "1234567890") -> None:
    """Create the minimum registered user required by subscribe-community."""
    database.users.create_user(
        discord_user_id=discord_user_id,
        activitypub_username="alice",
        actor_url=f"https://{BRIDGE_EXAMPLE_DOMAIN}/users/alice",
        inbox_url=f"https://{BRIDGE_EXAMPLE_DOMAIN}/users/alice/inbox",
        outbox_url=f"https://{BRIDGE_EXAMPLE_DOMAIN}/users/alice/outbox",
        followers_url=f"https://{BRIDGE_EXAMPLE_DOMAIN}/users/alice/followers",
        public_key_pem="public-key",
        private_key_pem="private-key",
    )


@pytest.mark.asyncio
async def test_no_subscription_subscribe_command_sends_follow_and_marks_pending(
    tmp_path: Path,
    command_tree,
    interaction,
    forum_channel,
    lemmy,
    fedify_gateway,
) -> None:
    """A fresh moderator subscribe should create one pending follow lifecycle row."""
    database = _database(tmp_path)
    # subscribe-community requires a registered user; interaction.user.id is "1234567890"
    _register_user(database)
    community_actor_url = _community_actor_url()
    fedify_gateway.follow_community.return_value = SimpleNamespace(
        community_actor_url=community_actor_url,
        community_inbox_url=f"{community_actor_url}/inbox",
        follow_activity_id=f"https://{BRIDGE_EXAMPLE_DOMAIN}/activities/follow/1",
    )

    subscribe.register(command_tree, database, fedify_gateway)

    command = command_tree.commands["subscribe-community"]
    await command.callback(
        interaction,
        f"https://{LEMMY_EXAMPLE_DOMAIN}",
        f"{community_actor_url}|hackers|777",
        forum_channel,
    )
    subscription = database.remote_subscriptions.get_subscription_by_channel(forum_channel.id)
    bridge_follow = database.bridge_actor_follows.get_bridge_actor_follow(community_actor_url)

    assert subscription is not None
    assert subscription.status == "pending"
    # A bridge_actor_follows row must be created alongside the channel subscription.
    assert bridge_follow is not None
    assert bridge_follow.status == "pending"
    assert bridge_follow.community_actor_id == community_actor_url
    fedify_gateway.follow_community.assert_awaited_once_with(community_actor_url)
    interaction.response.send_message.assert_awaited_once_with(
        "Sent a bridge follow for <#12345> -> **hackers**. Waiting for federation acceptance.",
        ephemeral=False,
    )


@pytest.mark.asyncio
async def test_second_channel_reuses_existing_accepted_bridge_follow(
    tmp_path: Path,
    command_tree,
    interaction,
    forum_channel,
    lemmy,
    fedify_gateway,
) -> None:
    """Second channel subscribing to an already-accepted community activates immediately."""
    database = _database(tmp_path)
    _register_user(database)
    community_actor_url = _community_actor_url()
    follow_activity_id = f"https://{BRIDGE_EXAMPLE_DOMAIN}/activities/follow/1"
    # Pre-seed: bridge actor already accepted for this community, first channel active.
    database.bridge_actor_follows.create_bridge_actor_follow(
        community_actor_id=community_actor_url,
        follow_activity_id=follow_activity_id,
        community_inbox_url=f"{community_actor_url}/inbox",
        status="accepted",
    )
    # Different channel from forum_channel — first one is already subscribed.
    database.remote_subscriptions.create_subscription(
        discord_channel_id=99999,
        lemmy_community_actor_id=community_actor_url,
        lemmy_community_name="hackers",
        lemmy_community_id=777,
        community_handle=f"!hackers@{LEMMY_EXAMPLE_DOMAIN}",
        community_inbox_url=f"{community_actor_url}/inbox",
        follow_activity_id=follow_activity_id,
        initiated_by_discord_user_id="9999",
        status="accepted",
    )

    subscribe.register(command_tree, database, fedify_gateway)

    command = command_tree.commands["subscribe-community"]
    await command.callback(
        interaction,
        f"https://{LEMMY_EXAMPLE_DOMAIN}",
        f"{community_actor_url}|hackers|777",
        forum_channel,
    )
    new_subscription = database.remote_subscriptions.get_subscription_by_channel(forum_channel.id)

    # No new Follow sent — bridge actor already federated.
    fedify_gateway.follow_community.assert_not_awaited()
    assert new_subscription is not None
    # Second channel is immediately accepted — no need to wait.
    assert new_subscription.status == "accepted"
    # Still only one bridge_actor_follows row.
    bridge_follow = database.bridge_actor_follows.get_bridge_actor_follow(community_actor_url)
    assert bridge_follow is not None
    assert bridge_follow.status == "accepted"


@pytest.mark.asyncio
async def test_pending_subscription_second_subscribe_does_not_send_follow(
    tmp_path: Path,
    command_tree,
    interaction,
    forum_channel,
    lemmy,
    fedify_gateway,
) -> None:
    """A pending subscription should return a waiting message instead of a second follow."""
    database = _database(tmp_path)
    _register_user(database)
    database.remote_subscriptions.create_subscription(
        discord_channel_id=forum_channel.id,
        lemmy_community_actor_id=_community_actor_url(),
        lemmy_community_name="hackers",
        lemmy_community_id=777,
        community_handle=f"!hackers@{LEMMY_EXAMPLE_DOMAIN}",
        status="pending",
    )

    subscribe.register(command_tree, database, fedify_gateway)

    command = command_tree.commands["subscribe-community"]
    await command.callback(
        interaction,
        f"https://{LEMMY_EXAMPLE_DOMAIN}",
        f"{_community_actor_url()}|hackers|777",
        forum_channel,
    )

    fedify_gateway.follow_community.assert_not_awaited()
    interaction.response.send_message.assert_awaited_once_with(
        "Forum channel <#12345> is already used by another bridge community or subscription.",
        ephemeral=True,
    )


@pytest.mark.asyncio
async def test_accepted_subscription_second_subscribe_does_not_send_follow(
    tmp_path: Path,
    command_tree,
    interaction,
    forum_channel,
    lemmy,
    fedify_gateway,
) -> None:
    """An already active subscription should report success without refollowing."""
    database = _database(tmp_path)
    _register_user(database)
    database.remote_subscriptions.create_subscription(
        discord_channel_id=forum_channel.id,
        lemmy_community_actor_id=_community_actor_url(),
        lemmy_community_name="hackers",
        lemmy_community_id=777,
        community_handle=f"!hackers@{LEMMY_EXAMPLE_DOMAIN}",
        status="accepted",
    )

    subscribe.register(command_tree, database, fedify_gateway)

    command = command_tree.commands["subscribe-community"]
    await command.callback(
        interaction,
        f"https://{LEMMY_EXAMPLE_DOMAIN}",
        f"{_community_actor_url()}|hackers|777",
        forum_channel,
    )

    fedify_gateway.follow_community.assert_not_awaited()
    interaction.response.send_message.assert_awaited_once_with(
        "Forum channel <#12345> is already used by another bridge community or subscription.",
        ephemeral=True,
    )


@pytest.mark.asyncio
async def test_follow_dispatch_failure_marks_subscription_failed(
    tmp_path: Path,
    command_tree,
    interaction,
    forum_channel,
    lemmy,
    fedify_gateway,
) -> None:
    """A gateway follow failure should become explicit failed local state."""
    database = _database(tmp_path)
    # subscribe-community requires a registered user; interaction.user.id is "1234567890"
    _register_user(database)
    fedify_gateway.follow_community.side_effect = RuntimeError("boom")

    subscribe.register(command_tree, database, fedify_gateway)

    command = command_tree.commands["subscribe-community"]
    await command.callback(
        interaction,
        f"https://{LEMMY_EXAMPLE_DOMAIN}",
        f"{_community_actor_url()}|hackers|777",
        forum_channel,
    )
    subscription = database.remote_subscriptions.get_subscription_by_channel(forum_channel.id)
    bridge_follow = database.bridge_actor_follows.get_bridge_actor_follow(_community_actor_url())

    assert subscription is not None
    assert subscription.status == "failed"
    assert bridge_follow is not None
    assert bridge_follow.status == "failed"
    interaction.response.send_message.assert_awaited_once_with(
        "Could not subscribe <#12345> to **hackers** because the bridge Follow request failed.",
        ephemeral=True,
    )


@pytest.mark.asyncio
async def test_follow_accepted_event_promotes_all_pending_subscriptions(
    tmp_path: Path,
) -> None:
    """A matching Accept(Follow) should activate ALL pending channel subscriptions."""
    database = _database(tmp_path)
    follow_activity_id = f"https://{BRIDGE_EXAMPLE_DOMAIN}/activities/follow/1"
    community_actor_url = _community_actor_url()
    # Two channels both waiting for acceptance on the same community.
    database.bridge_actor_follows.create_bridge_actor_follow(
        community_actor_id=community_actor_url,
        follow_activity_id=follow_activity_id,
        community_inbox_url=f"{community_actor_url}/inbox",
        status="pending",
    )
    database.remote_subscriptions.create_subscription(
        discord_channel_id=12345,
        lemmy_community_actor_id=community_actor_url,
        lemmy_community_name="hackers",
        lemmy_community_id=777,
        community_handle=f"!hackers@{LEMMY_EXAMPLE_DOMAIN}",
        community_inbox_url=f"{community_actor_url}/inbox",
        follow_activity_id=follow_activity_id,
        initiated_by_discord_user_id="1111",
        status="pending",
    )
    database.remote_subscriptions.create_subscription(
        discord_channel_id=99999,
        lemmy_community_actor_id=community_actor_url,
        lemmy_community_name="hackers",
        lemmy_community_id=777,
        community_handle=f"!hackers@{LEMMY_EXAMPLE_DOMAIN}",
        community_inbox_url=f"{community_actor_url}/inbox",
        follow_activity_id=follow_activity_id,
        initiated_by_discord_user_id="2222",
        status="pending",
    )
    dm_user = SimpleNamespace(send=AsyncMock())
    runtime = SimpleNamespace(
        database=database,
        bot=SimpleNamespace(
            fetch_user=AsyncMock(return_value=dm_user),
        ),
    )
    event = FollowLifecycleEvent(
        event_type="follow.accepted",
        delivery_id=f"https://{LEMMY_EXAMPLE_DOMAIN}/activities/accept/1",
        occurred_at=datetime.now(UTC),
        community_actor_id=community_actor_url,
        actor_id=community_actor_url,
        object={"follow_activity_id": follow_activity_id},
    )

    result = await dispatch_activitypub_event(event, runtime)

    assert result.status == "processed"
    # Both channel subscriptions must be accepted.
    sub1 = database.remote_subscriptions.get_subscription_by_channel(12345)
    sub2 = database.remote_subscriptions.get_subscription_by_channel(99999)
    assert sub1.status == "accepted"
    assert sub2.status == "accepted"
    # The bridge-actor follow row must be accepted.
    bridge_follow = database.bridge_actor_follows.get_bridge_actor_follow(community_actor_url)
    assert bridge_follow.status == "accepted"
    # Both initiating users should receive a DM.
    assert runtime.bot.fetch_user.await_count == 2
    assert dm_user.send.await_count == 2


@pytest.mark.asyncio
async def test_unsubscribe_last_channel_sends_undo_follow(
    tmp_path: Path,
    command_tree,
    interaction,
    forum_channel,
    fedify_gateway,
) -> None:
    """Unsubscribing the last channel for a community sends Undo(Follow)."""
    database = _database(tmp_path)
    community_actor_url = _community_actor_url()
    follow_activity_id = f"https://{BRIDGE_EXAMPLE_DOMAIN}/activities/follow/1"
    database.bridge_actor_follows.create_bridge_actor_follow(
        community_actor_id=community_actor_url,
        follow_activity_id=follow_activity_id,
        community_inbox_url=f"{community_actor_url}/inbox",
        status="accepted",
    )
    database.remote_subscriptions.create_subscription(
        discord_channel_id=forum_channel.id,
        lemmy_community_actor_id=community_actor_url,
        lemmy_community_name="hackers",
        lemmy_community_id=777,
        community_handle=f"!hackers@{LEMMY_EXAMPLE_DOMAIN}",
        community_inbox_url=f"{community_actor_url}/inbox",
        follow_activity_id=follow_activity_id,
        initiated_by_discord_user_id="1234567890",
        status="accepted",
    )

    unsubscribe.register(command_tree, database, fedify_gateway)

    command = command_tree.commands["unsubscribe-channel"]
    await command.callback(interaction, forum_channel)

    # Channel subscription must be gone.
    assert database.remote_subscriptions.get_subscription_by_channel(forum_channel.id) is None
    # Undo(Follow) must be dispatched.
    fedify_gateway.unfollow_community.assert_awaited_once_with(
        community_actor_url, follow_activity_id
    )
    # Bridge follow row must be cleaned up.
    assert database.bridge_actor_follows.get_bridge_actor_follow(community_actor_url) is None


@pytest.mark.asyncio
async def test_unsubscribe_one_of_two_channels_keeps_bridge_follow(
    tmp_path: Path,
    command_tree,
    interaction,
    forum_channel,
    fedify_gateway,
) -> None:
    """Unsubscribing one of two channels keeps the bridge follow row intact."""
    database = _database(tmp_path)
    community_actor_url = _community_actor_url()
    follow_activity_id = f"https://{BRIDGE_EXAMPLE_DOMAIN}/activities/follow/1"
    database.bridge_actor_follows.create_bridge_actor_follow(
        community_actor_id=community_actor_url,
        follow_activity_id=follow_activity_id,
        community_inbox_url=f"{community_actor_url}/inbox",
        status="accepted",
    )
    # Two channels subscribed to the same community.
    database.remote_subscriptions.create_subscription(
        discord_channel_id=forum_channel.id,
        lemmy_community_actor_id=community_actor_url,
        lemmy_community_name="hackers",
        lemmy_community_id=777,
        community_handle=f"!hackers@{LEMMY_EXAMPLE_DOMAIN}",
        community_inbox_url=f"{community_actor_url}/inbox",
        follow_activity_id=follow_activity_id,
        initiated_by_discord_user_id="1234567890",
        status="accepted",
    )
    database.remote_subscriptions.create_subscription(
        discord_channel_id=99999,
        lemmy_community_actor_id=community_actor_url,
        lemmy_community_name="hackers",
        lemmy_community_id=777,
        community_handle=f"!hackers@{LEMMY_EXAMPLE_DOMAIN}",
        community_inbox_url=f"{community_actor_url}/inbox",
        follow_activity_id=follow_activity_id,
        initiated_by_discord_user_id="9999",
        status="accepted",
    )

    unsubscribe.register(command_tree, database, fedify_gateway)

    command = command_tree.commands["unsubscribe-channel"]
    await command.callback(interaction, forum_channel)

    # Only the target channel subscription is removed.
    assert database.remote_subscriptions.get_subscription_by_channel(forum_channel.id) is None
    assert database.remote_subscriptions.get_subscription_by_channel(99999) is not None
    # No Undo dispatched — the other channel still subscribes.
    fedify_gateway.unfollow_community.assert_not_awaited()
    # Bridge follow row is preserved.
    assert database.bridge_actor_follows.get_bridge_actor_follow(community_actor_url) is not None
