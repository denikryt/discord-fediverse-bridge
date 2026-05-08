"""Behavior scenarios for community subscription lifecycle decisions."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.activitypub_handlers import dispatch_activitypub_event
from src.activitypub_models import FollowLifecycleEvent
from src.commands import subscribe
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
    """Create the minimum registered user required by subscribe-channel."""
    database.create_user(
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
    # subscribe-channel requires a registered user; interaction.user.id is "1234567890"
    _register_user(database)
    community_actor_url = _community_actor_url()
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
        f"{community_actor_url}|hackers|777",
    )
    subscription = database.get_subscription_by_channel(forum_channel.id)

    assert subscription is not None
    assert subscription.status == "pending"
    fedify_gateway.follow_community.assert_awaited_once_with(community_actor_url)
    interaction.response.send_message.assert_awaited_once_with(
        "Sent a bridge follow for <#12345> -> **hackers**. Waiting for federation acceptance.",
        ephemeral=False,
    )


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
    database.create_subscription(
        discord_channel_id=forum_channel.id,
        lemmy_community_actor_id=_community_actor_url(),
        lemmy_community_name="hackers",
        lemmy_community_id=777,
        community_handle=f"!hackers@{LEMMY_EXAMPLE_DOMAIN}",
        status="pending",
    )

    subscribe.register(command_tree, database, lemmy, fedify_gateway)

    command = command_tree.commands["subscribe-channel"]
    await command.callback(
        interaction,
        forum_channel,
        f"{_community_actor_url()}|hackers|777",
    )

    fedify_gateway.follow_community.assert_not_awaited()
    interaction.response.send_message.assert_awaited_once_with(
        f"Channel <#12345> is still waiting for **!hackers@{LEMMY_EXAMPLE_DOMAIN}** to accept the bridge follow.",
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
    database.create_subscription(
        discord_channel_id=forum_channel.id,
        lemmy_community_actor_id=_community_actor_url(),
        lemmy_community_name="hackers",
        lemmy_community_id=777,
        community_handle=f"!hackers@{LEMMY_EXAMPLE_DOMAIN}",
        status="accepted",
    )

    subscribe.register(command_tree, database, lemmy, fedify_gateway)

    command = command_tree.commands["subscribe-channel"]
    await command.callback(
        interaction,
        forum_channel,
        f"{_community_actor_url()}|hackers|777",
    )

    fedify_gateway.follow_community.assert_not_awaited()
    interaction.response.send_message.assert_awaited_once_with(
        f"Channel <#12345> is already subscribed to **!hackers@{LEMMY_EXAMPLE_DOMAIN}**.",
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
    # subscribe-channel requires a registered user; interaction.user.id is "1234567890"
    _register_user(database)
    fedify_gateway.follow_community.side_effect = RuntimeError("boom")

    subscribe.register(command_tree, database, lemmy, fedify_gateway)

    command = command_tree.commands["subscribe-channel"]
    await command.callback(
        interaction,
        forum_channel,
        f"{_community_actor_url()}|hackers|777",
    )
    subscription = database.get_subscription_by_channel(forum_channel.id)

    assert subscription is not None
    assert subscription.status == "failed"
    interaction.response.send_message.assert_awaited_once_with(
        "Could not subscribe <#12345> to **hackers** because the bridge Follow request failed.",
        ephemeral=True,
    )


@pytest.mark.asyncio
async def test_follow_accepted_event_promotes_pending_subscription_to_accepted(
    tmp_path: Path,
) -> None:
    """A matching Accept(Follow) should activate the subscription and DM the initiator."""
    database = _database(tmp_path)
    follow_activity_id = f"https://{BRIDGE_EXAMPLE_DOMAIN}/activities/follow/1"
    dm_user = SimpleNamespace(send=AsyncMock())
    database.create_subscription(
        discord_channel_id=12345,
        lemmy_community_actor_id=_community_actor_url(),
        lemmy_community_name="hackers",
        lemmy_community_id=777,
        community_handle=f"!hackers@{LEMMY_EXAMPLE_DOMAIN}",
        community_inbox_url=f"{_community_actor_url()}/inbox",
        follow_activity_id=follow_activity_id,
        initiated_by_discord_user_id="1234567890",
        status="pending",
    )
    runtime = SimpleNamespace(
        database=database,
        lemmy=SimpleNamespace(),
        bot=SimpleNamespace(
            fetch_user=AsyncMock(return_value=dm_user),
        ),
    )
    event = FollowLifecycleEvent(
        event_type="follow.accepted",
        delivery_id=f"https://{LEMMY_EXAMPLE_DOMAIN}/activities/accept/1",
        occurred_at=datetime.now(UTC),
        community_actor_id=_community_actor_url(),
        actor_id=_community_actor_url(),
        object={"follow_activity_id": follow_activity_id},
    )

    result = await dispatch_activitypub_event(event, runtime)
    subscription = database.get_subscription_by_channel(12345)

    assert result.status == "processed"
    assert subscription is not None
    assert subscription.status == "accepted"
    runtime.bot.fetch_user.assert_awaited_once_with(1234567890)
    dm_user.send.assert_awaited_once_with(
        "Your bridge follow for **!hackers@lemmy.example** was accepted. "
        "<#12345> is now federated."
    )
