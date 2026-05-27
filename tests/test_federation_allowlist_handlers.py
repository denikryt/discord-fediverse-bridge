"""Tests for federation allowlist enforcement in activitypub_handlers."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.activitypub_handlers import dispatch_activitypub_event
from src.activitypub_models import ActivityPubEvent, FollowLifecycleEvent
from src.db import Database
from tests_constants import BRIDGE_EXAMPLE_DOMAIN, LEMMY_EXAMPLE_DOMAIN


def _database(tmp_path: Path) -> Database:
    """Create a real SQLite-backed repository for allowlist handler tests."""
    db = Database(f"sqlite:///{tmp_path / 'bridge-allowlist.db'}")
    db.create_all()
    return db


def _settings(allowlist: list[str]) -> SimpleNamespace:
    """Build a minimal settings stub with the given federation_allowlist."""
    return SimpleNamespace(federation_allowlist=allowlist)


def _post_created_event(community_actor_id: str) -> ActivityPubEvent:
    """Build a minimal post.created event for the given community actor URL."""
    return ActivityPubEvent(
        event_type="post.created",
        delivery_id=f"https://{LEMMY_EXAMPLE_DOMAIN}/activities/create/1",
        occurred_at=datetime.now(UTC),
        community_actor_id=community_actor_id,
        actor_id=f"https://{LEMMY_EXAMPLE_DOMAIN}/u/alice",
        object={
            "ap_id": f"https://{LEMMY_EXAMPLE_DOMAIN}/post/1",
            "kind": "post",
            "lemmy_id": 1,
            "title": "Test post",
            "body_markdown": "body",
            "url": f"https://{LEMMY_EXAMPLE_DOMAIN}/post/1",
            "published_at": datetime.now(UTC).isoformat(),
            "author_name": "alice",
        },
    )


def _create_accepted_remote_subscription(db: Database, community_actor_id: str) -> None:
    """Create active remote subscription state required for content dispatch."""
    db.bridge_actor_follows.create_bridge_actor_follow(
        community_actor_id=community_actor_id,
        follow_activity_id=f"https://{BRIDGE_EXAMPLE_DOMAIN}/activities/follow/allowlist",
        community_inbox_url=f"{community_actor_id}/inbox",
        status="accepted",
    )
    db.remote_subscriptions.create_subscription(
        discord_channel_id=123,
        lemmy_community_actor_id=community_actor_id,
        lemmy_community_name="hackers",
        lemmy_community_id=42,
        community_handle=f"!hackers@{LEMMY_EXAMPLE_DOMAIN}",
        community_inbox_url=f"{community_actor_id}/inbox",
        follow_activity_id=f"https://{BRIDGE_EXAMPLE_DOMAIN}/activities/follow/allowlist",
        initiated_by_discord_user_id="1234567890",
        status="accepted",
    )


def _follow_accepted_event(community_actor_id: str) -> FollowLifecycleEvent:
    """Build a minimal follow.accepted event for the given community actor URL."""
    return FollowLifecycleEvent(
        event_type="follow.accepted",
        delivery_id=f"https://{LEMMY_EXAMPLE_DOMAIN}/activities/accept/1",
        occurred_at=datetime.now(UTC),
        community_actor_id=community_actor_id,
        actor_id=community_actor_id,
        object={"follow_activity_id": f"https://{BRIDGE_EXAMPLE_DOMAIN}/activities/follow/1"},
    )


@pytest.mark.asyncio
async def test_dispatch_skips_event_from_unlisted_instance(tmp_path: Path) -> None:
    # When a non-empty allowlist is configured and the event's community is on
    # an instance not in the list, the event must be skipped without processing.
    db = _database(tmp_path)
    runtime = SimpleNamespace(
        database=db,
        settings=_settings(["allowed.example"]),
        bot=AsyncMock(),
        community_runtime=AsyncMock(),
    )
    community_actor_id = f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers"
    _create_accepted_remote_subscription(db, community_actor_id)
    event = _post_created_event(community_actor_id)

    result = await dispatch_activitypub_event(event, runtime)

    assert result.status == "skipped"
    assert result.detail == "instance not in allowlist"


@pytest.mark.asyncio
async def test_dispatch_allows_event_from_listed_instance(tmp_path: Path) -> None:
    # An event from an instance that is in the allowlist must pass through to
    # normal routing (echo suppression and community lookup).
    db = _database(tmp_path)
    community_runtime = AsyncMock()
    community_runtime.handle_inbound_post.return_value = SimpleNamespace(
        status="processed", detail="ok"
    )
    runtime = SimpleNamespace(
        database=db,
        settings=_settings([LEMMY_EXAMPLE_DOMAIN]),
        bot=AsyncMock(),
        community_runtime=community_runtime,
    )
    community_actor_id = f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers"
    _create_accepted_remote_subscription(db, community_actor_id)
    event = _post_created_event(community_actor_id)

    result = await dispatch_activitypub_event(event, runtime)

    # Allowlist passed — routed to the community runtime handler.
    community_runtime.handle_inbound_post.assert_awaited_once()
    assert result.status == "processed"


@pytest.mark.asyncio
async def test_dispatch_allows_all_when_allowlist_empty(tmp_path: Path) -> None:
    # An empty allowlist means open federation — events from any instance pass through.
    db = _database(tmp_path)
    community_runtime = AsyncMock()
    community_runtime.handle_inbound_post.return_value = SimpleNamespace(
        status="processed", detail="ok"
    )
    runtime = SimpleNamespace(
        database=db,
        settings=_settings([]),
        bot=AsyncMock(),
        community_runtime=community_runtime,
    )
    community_actor_id = "https://some.random.instance/c/news"
    _create_accepted_remote_subscription(db, community_actor_id)
    event = _post_created_event(community_actor_id)

    result = await dispatch_activitypub_event(event, runtime)

    community_runtime.handle_inbound_post.assert_awaited_once()
    assert result.status == "processed"


@pytest.mark.asyncio
async def test_dispatch_follow_accepted_bypasses_allowlist(tmp_path: Path) -> None:
    # follow.accepted is a lifecycle response to our own outbound Follow.
    # It must always be processed regardless of the allowlist, because we
    # initiated the Follow and need to record the acceptance.
    db = _database(tmp_path)
    follow_activity_id = f"https://{BRIDGE_EXAMPLE_DOMAIN}/activities/follow/1"
    community_actor_id = f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers"
    db.bridge_actor_follows.create_bridge_actor_follow(
        community_actor_id=community_actor_id,
        follow_activity_id=follow_activity_id,
        community_inbox_url=f"{community_actor_id}/inbox",
        status="pending",
    )
    db.remote_subscriptions.create_subscription(
        discord_channel_id=123,
        lemmy_community_actor_id=community_actor_id,
        lemmy_community_name="hackers",
        lemmy_community_id=42,
        community_handle=f"!hackers@{LEMMY_EXAMPLE_DOMAIN}",
        community_inbox_url=f"{community_actor_id}/inbox",
        follow_activity_id=follow_activity_id,
        initiated_by_discord_user_id="1234567890",
        status="pending",
    )
    dm_user = SimpleNamespace(send=AsyncMock())
    runtime = SimpleNamespace(
        database=db,
        # Allowlist explicitly excludes LEMMY_EXAMPLE_DOMAIN — must not block follow.accepted.
        settings=_settings(["allowed.example"]),
        bot=SimpleNamespace(fetch_user=AsyncMock(return_value=dm_user)),
        community_runtime=AsyncMock(),
    )
    event = _follow_accepted_event(community_actor_id)

    result = await dispatch_activitypub_event(event, runtime)

    # follow.accepted must still be processed even though the instance is not listed.
    assert result.status == "processed"
    subscription = db.remote_subscriptions.get_subscription_by_channel(123)
    assert subscription.status == "accepted"
