"""Behavior scenarios for on-demand post backfill when a comment arrives before its parent post.

These tests cover the case where a comment.created event arrives but the parent
post has not yet been delivered to Discord. The bridge should fetch the post from
the remote AP endpoint and create the thread before delivering the comment.

System state -> action -> observable result pattern per AGENTS.md.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from src.activitypub_models import ActivityPubEvent
from src.community_sync.runtime import CommunityRuntime
from src.db import Database
from src.content_publish_service import ContentPublishService
from src.http_api import create_http_app
from tests_constants import BRIDGE_HOST_DOMAIN, LEMMY_EXAMPLE_DOMAIN

_POST_AP_ID = f"https://{LEMMY_EXAMPLE_DOMAIN}/post/999"
_COMMENT_AP_ID = f"https://{LEMMY_EXAMPLE_DOMAIN}/comment/888"
_COMMUNITY_ACTOR_ID = f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers"

# Minimal valid Lemmy AP Page document as returned by a real instance.
_AP_POST_DOC = {
    "@context": [
        "https://join-lemmy.org/context.json",
        "https://www.w3.org/ns/activitystreams",
    ],
    "type": "Page",
    "id": _POST_AP_ID,
    "attributedTo": f"https://{LEMMY_EXAMPLE_DOMAIN}/u/bob",
    "name": "Backfill Test Post",
    "attachment": [
        {
            "href": "https://example.com/article",
            "mediaType": "text/html; charset=utf-8",
            "type": "Link",
        }
    ],
    "source": {
        "content": "Some body text",
        "mediaType": "text/markdown",
    },
    "published": "2026-05-10T12:00:00Z",
    "audience": _COMMUNITY_ACTOR_ID,
}

# Text-only post: no attachment, has source body.
_AP_TEXT_POST_DOC = {
    "@context": ["https://www.w3.org/ns/activitystreams"],
    "type": "Page",
    "id": _POST_AP_ID,
    "attributedTo": f"https://{LEMMY_EXAMPLE_DOMAIN}/u/bob",
    "name": "Text Only Post",
    "attachment": [],
    "source": {
        "content": "Pure text body",
        "mediaType": "text/markdown",
    },
    "published": "2026-05-10T12:00:00Z",
    "audience": _COMMUNITY_ACTOR_ID,
}


def _database(tmp_path: Path) -> Database:
    """Create one real SQLite DB for backfill behavior scenarios."""
    db = Database(f"sqlite:///{tmp_path / 'backfill.db'}")
    db.create_all()
    return db


def _community_runtime(database: Database, *, bot: object) -> CommunityRuntime:
    """Build a real CommunityRuntime for backfill routing scenarios."""
    publish_service = ContentPublishService(
        database=database,
        fedify_gateway=AsyncMock(),
        bridge_prefix="[bridge]",
    )
    return CommunityRuntime(database=database, content_publish_service=publish_service, bot=bot)


def _accepted_subscription(database: Database, *, channel_id: int = 100) -> None:
    """Insert one accepted subscription for the test community and channel."""
    database.remote_subscriptions.create_subscription(
        discord_channel_id=channel_id,
        lemmy_community_actor_id=_COMMUNITY_ACTOR_ID,
        lemmy_community_name="hackers",
        lemmy_community_id=42,
        community_handle=f"!hackers@{LEMMY_EXAMPLE_DOMAIN}",
        community_inbox_url=f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers/inbox",
        follow_activity_id=f"https://{BRIDGE_HOST_DOMAIN}/activities/follow/{channel_id}",
        status="accepted",
    )


def _comment_event(
    *,
    object_id: str = _COMMENT_AP_ID,
    post_ap_id: str = _POST_AP_ID,
    delivery_id: str | None = None,
) -> ActivityPubEvent:
    """Build a minimal comment.created event for the test post."""
    return ActivityPubEvent.model_validate(
        {
            "event_type": "comment.created",
            "delivery_id": delivery_id
            or f"https://{LEMMY_EXAMPLE_DOMAIN}/activities/create/comment/888",
            "occurred_at": "2026-05-10T13:00:00Z",
            "community_actor_id": _COMMUNITY_ACTOR_ID,
            "actor_id": f"https://{LEMMY_EXAMPLE_DOMAIN}/u/bob",
            "object": {
                "ap_id": object_id,
                "kind": "comment",
                "lemmy_id": 888,
                "post_ap_id": post_ap_id,
                "post_lemmy_id": 999,
                "parent_ap_id": None,
                "title": None,
                "body_markdown": "hello from comment",
                "url": object_id,
                "published_at": "2026-05-10T13:00:00Z",
                "author_name": "bob",
            },
        }
    )


def _event_headers(delivery_id: str) -> dict[str, str]:
    """Return the trusted internal-auth headers for one event delivery."""
    return {
        "Authorization": "Bearer secret",
        "X-Bridge-Delivery-Id": delivery_id,
    }


def test_inbound_comment_creates_post_thread_when_post_not_yet_mapped(
    tmp_path: Path,
) -> None:
    """comment.created should backfill the missing post thread then deliver the comment.

    System state: one accepted subscription, no CommunityThreadGroup for the post.
    Action: comment.created arrives; HTTP fetch of post_ap_id succeeds.
    Expected: CommunityThreadGroup created, one delivery row, comment delivered into
    the thread, CommunityMessageGroup and its delivery row written.
    """
    database = _database(tmp_path)
    _accepted_subscription(database, channel_id=100)

    forum_channel = SimpleNamespace(
        id=100,
        create_thread=AsyncMock(
            return_value=SimpleNamespace(
                thread=SimpleNamespace(id=200),
                message=SimpleNamespace(id=300),
            )
        ),
    )
    thread = SimpleNamespace(
        id=200,
        send=AsyncMock(return_value=SimpleNamespace(id=900)),
    )

    async def _fetch_forum_channel(channel_id: int) -> object:
        assert channel_id == 100
        return forum_channel

    async def _get_thread_by_id(thread_id: int) -> object:
        assert thread_id == 200
        return thread

    bot = SimpleNamespace(
        wait_until_bridge_ready=AsyncMock(),
        fetch_forum_channel=AsyncMock(side_effect=_fetch_forum_channel),
        get_thread_by_id=AsyncMock(side_effect=_get_thread_by_id),
    )
    runtime = SimpleNamespace(
        settings=SimpleNamespace(fedify_shared_secret="secret"),
        database=database,
        bot=bot,
        community_runtime=_community_runtime(database, bot=bot),
    )
    client = TestClient(create_http_app(runtime), raise_server_exceptions=False)
    event = _comment_event()

    with patch(
        "src.bridge_lemmy_to_discord._fetch_ap_object",
        new=AsyncMock(return_value=_AP_POST_DOC),
    ):
        response = client.post(
            "/internal/activitypub/events",
            headers=_event_headers(event.delivery_id),
            json=event.model_dump(mode="json"),
        )

    assert response.status_code == 200
    assert response.json()["status"] == "processed"

    # Thread group must be created for the post that was fetched.
    thread_group = database.discord_fanout_groups.get_thread_group_by_ap_object(_POST_AP_ID)
    assert thread_group is not None, "CommunityThreadGroup should have been created by backfill"
    assert thread_group.ap_object_id == _POST_AP_ID

    thread_deliveries = database.discord_fanout_groups.get_thread_deliveries(thread_group.id)
    assert len(thread_deliveries) == 1
    assert thread_deliveries[0].discord_thread_id == 200
    assert thread_deliveries[0].discord_channel_id == 100

    # Comment must be mapped in CommunityMessageGroup.
    message_group = database.discord_fanout_groups.get_message_group_by_ap_object(_COMMENT_AP_ID)
    assert message_group is not None, "CommunityMessageGroup should have been written"
    msg_deliveries = database.discord_fanout_groups.get_message_deliveries(message_group.id)
    assert len(msg_deliveries) == 1
    assert msg_deliveries[0].discord_thread_id == 200
    assert msg_deliveries[0].discord_message_id == 900


def test_inbound_comment_only_backfills_channels_without_existing_delivery(
    tmp_path: Path,
) -> None:
    """Backfill must only create threads in channels that have no delivery row yet.

    System state: two accepted subscriptions (ch 100, ch 101); thread group exists
    for the post with a delivery only for channel 100.
    Action: comment.created arrives; HTTP fetch succeeds.
    Expected: new thread delivery created for channel 101 only; channel 100 delivery
    unchanged; comment delivered into both threads.
    """
    database = _database(tmp_path)
    _accepted_subscription(database, channel_id=100)
    _accepted_subscription(database, channel_id=101)

    # Channel 100 already has a thread group delivery (partial prior delivery).
    thread_group = database.discord_fanout_groups.create_thread_group(
        community_actor_id=_COMMUNITY_ACTOR_ID,
        source_channel_id=None,
        source_thread_id=None,
        source_starter_message_id=None,
        ap_object_id=_POST_AP_ID,
    )
    database.discord_fanout_groups.add_thread_delivery(
        thread_group_id=thread_group.id,
        discord_channel_id=100,
        discord_thread_id=200,
        discord_starter_message_id=300,
        role="inbound",
    )

    forum_channel_b = SimpleNamespace(
        id=101,
        create_thread=AsyncMock(
            return_value=SimpleNamespace(
                thread=SimpleNamespace(id=201),
                message=SimpleNamespace(id=301),
            )
        ),
    )
    thread_a = SimpleNamespace(
        id=200,
        send=AsyncMock(return_value=SimpleNamespace(id=900)),
    )
    thread_b = SimpleNamespace(
        id=201,
        send=AsyncMock(return_value=SimpleNamespace(id=901)),
    )

    async def _fetch_forum_channel(channel_id: int) -> object:
        # Only ch 101 needs a new thread — ch 100 already has one.
        assert channel_id == 101, f"Unexpected fetch_forum_channel({channel_id})"
        return forum_channel_b

    async def _get_thread_by_id(thread_id: int) -> object:
        if thread_id == 200:
            return thread_a
        if thread_id == 201:
            return thread_b
        raise AssertionError(f"Unexpected get_thread_by_id({thread_id})")

    bot = SimpleNamespace(
        wait_until_bridge_ready=AsyncMock(),
        fetch_forum_channel=AsyncMock(side_effect=_fetch_forum_channel),
        get_thread_by_id=AsyncMock(side_effect=_get_thread_by_id),
    )
    runtime = SimpleNamespace(
        settings=SimpleNamespace(fedify_shared_secret="secret"),
        database=database,
        bot=bot,
        community_runtime=_community_runtime(database, bot=bot),
    )
    client = TestClient(create_http_app(runtime), raise_server_exceptions=False)
    event = _comment_event()

    with patch(
        "src.bridge_lemmy_to_discord._fetch_ap_object",
        new=AsyncMock(return_value=_AP_POST_DOC),
    ):
        response = client.post(
            "/internal/activitypub/events",
            headers=_event_headers(event.delivery_id),
            json=event.model_dump(mode="json"),
        )

    assert response.status_code == 200
    assert response.json()["status"] == "processed"

    # Thread group is the existing one — not duplicated.
    refreshed_tg = database.discord_fanout_groups.get_thread_group_by_ap_object(_POST_AP_ID)
    assert refreshed_tg is not None
    assert refreshed_tg.id == thread_group.id

    thread_deliveries = database.discord_fanout_groups.get_thread_deliveries(thread_group.id)
    thread_ids = {d.discord_thread_id for d in thread_deliveries}
    # Channel 100 already had thread 200; channel 101 should now have thread 201.
    assert thread_ids == {200, 201}

    # Channel 100's original delivery row must be unchanged.
    ch100_delivery = next(d for d in thread_deliveries if d.discord_channel_id == 100)
    assert ch100_delivery.discord_thread_id == 200
    assert ch100_delivery.discord_starter_message_id == 300

    # Comment delivered into both threads.
    message_group = database.discord_fanout_groups.get_message_group_by_ap_object(_COMMENT_AP_ID)
    assert message_group is not None
    msg_deliveries = database.discord_fanout_groups.get_message_deliveries(message_group.id)
    assert {d.discord_thread_id for d in msg_deliveries} == {200, 201}


def test_inbound_comment_deferred_when_post_fetch_fails(
    tmp_path: Path,
) -> None:
    """comment.created must return deferred when the remote post fetch fails.

    System state: one accepted subscription, no thread group for the post.
    Action: comment.created arrives; HTTP fetch of post_ap_id raises an exception (404).
    Expected: result is deferred, no CommunityThreadGroup created, no comment delivery.
    """
    database = _database(tmp_path)
    _accepted_subscription(database, channel_id=100)

    bot = SimpleNamespace(
        wait_until_bridge_ready=AsyncMock(),
        fetch_forum_channel=AsyncMock(),
        get_thread_by_id=AsyncMock(),
    )
    runtime = SimpleNamespace(
        settings=SimpleNamespace(fedify_shared_secret="secret"),
        database=database,
        bot=bot,
        community_runtime=_community_runtime(database, bot=bot),
    )
    client = TestClient(create_http_app(runtime), raise_server_exceptions=False)
    event = _comment_event()

    with patch(
        "src.bridge_lemmy_to_discord._fetch_ap_object",
        new=AsyncMock(side_effect=RuntimeError("HTTP 404")),
    ):
        response = client.post(
            "/internal/activitypub/events",
            headers=_event_headers(event.delivery_id),
            json=event.model_dump(mode="json"),
        )

    assert response.status_code == 200
    assert response.json()["status"] == "deferred"

    # No thread group must have been created.
    thread_group = database.discord_fanout_groups.get_thread_group_by_ap_object(_POST_AP_ID)
    assert thread_group is None, "No CommunityThreadGroup should be created on fetch failure"

    # No comment delivery attempted.
    bot.fetch_forum_channel.assert_not_awaited()
    bot.get_thread_by_id.assert_not_awaited()
