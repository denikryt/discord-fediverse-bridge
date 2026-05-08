"""Runtime-oriented follow lifecycle scenarios for Stage 4 subscriptions."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

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
async def test_follow_accept_event_marks_pending_subscription_accepted(
    tmp_path: Path,
) -> None:
    """A matching Accept(Follow) should activate the pending subscription row."""
    database = _database(tmp_path)
    lemmy_actor_url = f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers"
    follow_activity_id = f"https://{BRIDGE_EXAMPLE_DOMAIN}/activities/follow/1"
    database.create_subscription(
        discord_channel_id=123,
        lemmy_community_actor_id=lemmy_actor_url,
        lemmy_community_name="hackers",
        lemmy_community_id=42,
        community_handle=f"!hackers@{LEMMY_EXAMPLE_DOMAIN}",
        community_inbox_url=f"{lemmy_actor_url}/inbox",
        follow_activity_id=follow_activity_id,
        status="pending",
    )
    runtime = SimpleNamespace(
        database=database,
        lemmy=SimpleNamespace(person_name=None),
        bot=SimpleNamespace(),
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
    subscription = database.get_subscription_by_channel(123)

    assert result.status == "processed"
    assert result.detail == "subscription accepted"
    assert subscription is not None
    assert subscription.status == "accepted"


def test_only_accepted_subscriptions_are_routed_for_inbound_community_events(
    tmp_path: Path,
) -> None:
    """Inbound routing must ignore pending and failed subscriptions."""
    database = _database(tmp_path)
    lemmy_actor_url = f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers"
    database.create_subscription(
        discord_channel_id=1,
        lemmy_community_actor_id=lemmy_actor_url,
        lemmy_community_name="hackers",
        lemmy_community_id=42,
        status="pending",
    )
    database.create_subscription(
        discord_channel_id=2,
        lemmy_community_actor_id=lemmy_actor_url,
        lemmy_community_name="hackers",
        lemmy_community_id=42,
        status="failed",
    )
    database.create_subscription(
        discord_channel_id=3,
        lemmy_community_actor_id=lemmy_actor_url,
        lemmy_community_name="hackers",
        lemmy_community_id=42,
        status="accepted",
    )

    accepted = database.get_subscriptions_by_community(lemmy_actor_url)

    assert len(accepted) == 1
    assert accepted[0].discord_channel_id == 3
