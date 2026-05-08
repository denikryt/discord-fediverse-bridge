"""Runtime-oriented follow lifecycle scenarios for Stage 4 subscriptions."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.activitypub_handlers import dispatch_activitypub_event
from src.activitypub_models import FollowLifecycleEvent
from src.db import Database


def _database(tmp_path: Path) -> Database:
    """Create a real SQLite-backed repository for follow lifecycle tests."""
    database = Database(f"sqlite:///{tmp_path / 'bridge-stage4.db'}")
    database.create_all()
    return database


def test_follow_accept_event_has_separate_typed_model() -> None:
    """Follow lifecycle payloads should validate without the post/comment shape."""
    event = FollowLifecycleEvent.model_validate(
        {
            "event_type": "follow.accepted",
            "delivery_id": "https://lemmy.example/activities/accept/1",
            "occurred_at": "2026-05-08T10:00:00Z",
            "community_actor_id": "https://lemmy.example/c/hackers",
            "actor_id": "https://lemmy.example/c/hackers",
            "object": {
                "follow_activity_id": "https://bridge.example/activities/follow/1"
            },
        }
    )

    assert event.event_type == "follow.accepted"
    assert (
        event.object.follow_activity_id
        == "https://bridge.example/activities/follow/1"
    )


@pytest.mark.asyncio
async def test_follow_accept_event_marks_pending_subscription_accepted(
    tmp_path: Path,
) -> None:
    """A matching Accept(Follow) should activate the pending subscription row."""
    database = _database(tmp_path)
    database.create_subscription(
        discord_channel_id=123,
        lemmy_community_actor_id="https://lemmy.example/c/hackers",
        lemmy_community_name="hackers",
        lemmy_community_id=42,
        community_handle="!hackers@lemmy.example",
        community_inbox_url="https://lemmy.example/c/hackers/inbox",
        follow_activity_id="https://bridge.example/activities/follow/1",
        status="pending",
    )
    runtime = SimpleNamespace(
        database=database,
        lemmy=SimpleNamespace(person_name=None),
        bot=SimpleNamespace(),
    )
    event = FollowLifecycleEvent(
        event_type="follow.accepted",
        delivery_id="https://lemmy.example/activities/accept/1",
        occurred_at=datetime.now(UTC),
        community_actor_id="https://lemmy.example/c/hackers",
        actor_id="https://lemmy.example/c/hackers",
        object={"follow_activity_id": "https://bridge.example/activities/follow/1"},
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
    database.create_subscription(
        discord_channel_id=1,
        lemmy_community_actor_id="https://lemmy.example/c/hackers",
        lemmy_community_name="hackers",
        lemmy_community_id=42,
        status="pending",
    )
    database.create_subscription(
        discord_channel_id=2,
        lemmy_community_actor_id="https://lemmy.example/c/hackers",
        lemmy_community_name="hackers",
        lemmy_community_id=42,
        status="failed",
    )
    database.create_subscription(
        discord_channel_id=3,
        lemmy_community_actor_id="https://lemmy.example/c/hackers",
        lemmy_community_name="hackers",
        lemmy_community_id=42,
        status="accepted",
    )

    accepted = database.get_subscriptions_by_community(
        "https://lemmy.example/c/hackers"
    )

    assert len(accepted) == 1
    assert accepted[0].discord_channel_id == 3
