"""Runtime-oriented follow lifecycle scenarios for Stage 4 subscriptions."""

from __future__ import annotations
from support.runtime import build_test_policy_service

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.activitypub_handlers import dispatch_activitypub_event
from src.activitypub_models import FollowLifecycleEvent
from src.db import Database
from tests_constants import BRIDGE_EXAMPLE_DOMAIN, LEMMY_EXAMPLE_DOMAIN


def _database(tmp_path: Path) -> Database:
    """Create a real SQLite-backed repository for follow lifecycle tests."""
    database = Database(f"sqlite:///{tmp_path / 'bridge-stage4.db'}")
    database.create_all()
    return database


def test_follow_accept_event_has_separate_typed_model() -> None:
    """Follow lifecycle payloads should validate without the post/comment shape."""
    lemmy_actor_url = f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers"
    follow_activity_id = f"https://{BRIDGE_EXAMPLE_DOMAIN}/activities/follow/1"
    event = FollowLifecycleEvent.model_validate(
        {
            "event_type": "follow.accepted",
            "delivery_id": f"https://{LEMMY_EXAMPLE_DOMAIN}/activities/accept/1",
            "occurred_at": "2026-05-08T10:00:00Z",
            "community_actor_id": lemmy_actor_url,
            "actor_id": lemmy_actor_url,
            "object": {"follow_activity_id": follow_activity_id},
        }
    )

    assert event.event_type == "follow.accepted"
    assert event.object.follow_activity_id == follow_activity_id


@pytest.mark.asyncio
async def test_follow_accept_event_marks_bridge_follow_and_channel_accepted(
    tmp_path: Path,
) -> None:
    """A matching Accept(Follow) should activate the bridge follow and channel rows."""
    database = _database(tmp_path)
    lemmy_actor_url = f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers"
    follow_activity_id = f"https://{BRIDGE_EXAMPLE_DOMAIN}/activities/follow/1"
    dm_user = SimpleNamespace(send=AsyncMock())
    database.bridge_actor_follows.create_bridge_actor_follow(
        community_actor_id=lemmy_actor_url,
        follow_activity_id=follow_activity_id,
        community_inbox_url=f"{lemmy_actor_url}/inbox",
        status="pending",
    )
    database.remote_subscriptions.create_subscription(
        discord_channel_id=123,
        lemmy_community_actor_id=lemmy_actor_url,
        lemmy_community_name="hackers",
        lemmy_community_id=42,
        community_handle=f"!hackers@{LEMMY_EXAMPLE_DOMAIN}",
        community_inbox_url=f"{lemmy_actor_url}/inbox",
        follow_activity_id=follow_activity_id,
        initiated_by_discord_user_id="1234567890",
        status="pending",
    )
    runtime = SimpleNamespace(
        database=database,
        bot=SimpleNamespace(
            fetch_user=AsyncMock(return_value=dm_user),
        ),
            bridge_policy_service=build_test_policy_service(database),
)
    event = FollowLifecycleEvent(
        event_type="follow.accepted",
        delivery_id=f"https://{LEMMY_EXAMPLE_DOMAIN}/activities/accept/1",
        occurred_at=datetime.now(UTC),
        community_actor_id=lemmy_actor_url,
        actor_id=lemmy_actor_url,
        object={"follow_activity_id": follow_activity_id},
    )

    result = await dispatch_activitypub_event(event, runtime)
    subscription = database.remote_subscriptions.get_subscription_by_channel(123)
    bridge_follow = database.bridge_actor_follows.get_bridge_actor_follow(lemmy_actor_url)

    assert result.status == "processed"
    assert subscription is not None
    assert subscription.status == "accepted"
    assert bridge_follow is not None
    assert bridge_follow.status == "accepted"
    runtime.bot.fetch_user.assert_awaited_once_with(1234567890)
    dm_user.send.assert_awaited_once_with(
        "Your bridge follow for **!hackers@lemmy.example** was accepted. "
        "<#123> is now federated."
    )


@pytest.mark.asyncio
async def test_follow_accept_without_bridge_follow_does_not_accept_channel_subscription(
    tmp_path: Path,
) -> None:
    """Unknown Accept(Follow) replies must not mutate subscription rows directly."""
    database = _database(tmp_path)
    lemmy_actor_url = f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers"
    follow_activity_id = f"https://{BRIDGE_EXAMPLE_DOMAIN}/activities/follow/legacy"
    dm_user = SimpleNamespace(send=AsyncMock())
    database.remote_subscriptions.create_subscription(
        discord_channel_id=123,
        lemmy_community_actor_id=lemmy_actor_url,
        lemmy_community_name="hackers",
        lemmy_community_id=42,
        community_handle=f"!hackers@{LEMMY_EXAMPLE_DOMAIN}",
        community_inbox_url=f"{lemmy_actor_url}/inbox",
        follow_activity_id=follow_activity_id,
        initiated_by_discord_user_id="1234567890",
        status="pending",
    )
    runtime = SimpleNamespace(
        database=database,
        bot=SimpleNamespace(
            fetch_user=AsyncMock(return_value=dm_user),
        ),
            bridge_policy_service=build_test_policy_service(database),
)
    event = FollowLifecycleEvent(
        event_type="follow.accepted",
        delivery_id=f"https://{LEMMY_EXAMPLE_DOMAIN}/activities/accept/legacy",
        occurred_at=datetime.now(UTC),
        community_actor_id=lemmy_actor_url,
        actor_id=lemmy_actor_url,
        object={"follow_activity_id": follow_activity_id},
    )

    result = await dispatch_activitypub_event(event, runtime)
    subscription = database.remote_subscriptions.get_subscription_by_channel(123)

    assert result.status == "skipped"
    assert result.detail == "bridge follow activity is not mapped"
    assert result.outcome.value == "ignored_unknown_follow"
    assert subscription is not None
    assert subscription.status == "pending"
    runtime.bot.fetch_user.assert_not_awaited()
    dm_user.send.assert_not_awaited()


def test_only_accepted_subscriptions_are_routed_for_inbound_community_events(
    tmp_path: Path,
) -> None:
    """Inbound routing must ignore pending and failed subscriptions."""
    database = _database(tmp_path)
    lemmy_actor_url = f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers"
    database.remote_subscriptions.create_subscription(
        discord_channel_id=1,
        lemmy_community_actor_id=lemmy_actor_url,
        lemmy_community_name="hackers",
        lemmy_community_id=42,
        status="pending",
    )
    database.remote_subscriptions.create_subscription(
        discord_channel_id=2,
        lemmy_community_actor_id=lemmy_actor_url,
        lemmy_community_name="hackers",
        lemmy_community_id=42,
        status="failed",
    )
    database.remote_subscriptions.create_subscription(
        discord_channel_id=3,
        lemmy_community_actor_id=lemmy_actor_url,
        lemmy_community_name="hackers",
        lemmy_community_id=42,
        status="accepted",
    )

    accepted = database.remote_subscriptions.get_subscriptions_by_community(lemmy_actor_url)

    assert len(accepted) == 1
    assert accepted[0].discord_channel_id == 3
