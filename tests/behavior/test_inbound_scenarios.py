"""Behavior scenarios for inbound ActivityPub delivery and dedup outcomes."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from src.activitypub_handlers import dispatch_activitypub_event
from src.activitypub_models import ActivityPubEvent
from src.community_sync.runtime import CommunityRuntime
from src.db import Database
from src.discord_publish_service import DiscordPublishService
from src.http_api import create_http_app
from tests_constants import BRIDGE_HOST_DOMAIN, LEMMY_EXAMPLE_DOMAIN


def _database(tmp_path: Path) -> Database:
    """Create one real SQLite repository for inbound behavior scenarios."""
    database = Database(f"sqlite:///{tmp_path / 'behavior-inbound.db'}")
    database.create_all()
    return database


def _community_runtime(database: Database, *, bot: object | None = None) -> CommunityRuntime:
    """Build a real CommunityRuntime for inbound routing scenarios."""
    publish_service = DiscordPublishService(
        database=database,
        fedify_gateway=AsyncMock(),
        bridge_prefix="[bridge]",
    )
    return CommunityRuntime(database=database, discord_publish_service=publish_service, bot=bot)


def _accepted_subscription(database: Database) -> None:
    """Insert one accepted community subscription used for inbound routing."""
    database.create_subscription(
        discord_channel_id=100,
        lemmy_community_actor_id=f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers",
        lemmy_community_name="hackers",
        lemmy_community_id=42,
        community_handle=f"!hackers@{LEMMY_EXAMPLE_DOMAIN}",
        community_inbox_url=f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers/inbox",
        follow_activity_id=f"https://{BRIDGE_HOST_DOMAIN}/activities/follow/1",
        status="accepted",
    )


def _accepted_subscription_for_channel(database: Database, *, channel_id: int) -> None:
    """Insert one accepted subscription row for a specific Discord forum channel."""
    database.create_subscription(
        discord_channel_id=channel_id,
        lemmy_community_actor_id=f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers",
        lemmy_community_name="hackers",
        lemmy_community_id=42,
        community_handle=f"!hackers@{LEMMY_EXAMPLE_DOMAIN}",
        community_inbox_url=f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers/inbox",
        follow_activity_id=f"https://{BRIDGE_HOST_DOMAIN}/activities/follow/{channel_id}",
        status="accepted",
    )


def _post_event(*, object_id: str, delivery_id: str | None = None) -> ActivityPubEvent:
    """Build one normalized inbound post event used across inbound scenarios."""
    return ActivityPubEvent.model_validate(
        {
            "event_type": "post.created",
            "delivery_id": delivery_id
            or f"https://{LEMMY_EXAMPLE_DOMAIN}/activities/create/post/1",
            "occurred_at": "2026-05-08T10:00:00Z",
            "community_actor_id": f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers",
            "actor_id": f"https://{LEMMY_EXAMPLE_DOMAIN}/u/alice",
            "object": {
                "ap_id": object_id,
                "kind": "post",
                "lemmy_id": 111,
                "post_ap_id": None,
                "post_lemmy_id": None,
                "parent_ap_id": None,
                "title": "Post title",
                "body_markdown": "hello",
                "url": object_id,
                "published_at": "2026-05-08T10:00:00Z",
                "author_name": "alice",
            },
        }
    )


def _comment_event(
    *,
    object_id: str,
    post_ap_id: str,
    parent_ap_id: str | None = None,
    delivery_id: str | None = None,
) -> ActivityPubEvent:
    """Build one normalized inbound comment event for retry and routing tests."""
    return ActivityPubEvent.model_validate(
        {
            "event_type": "comment.created",
            "delivery_id": delivery_id
            or f"https://{LEMMY_EXAMPLE_DOMAIN}/activities/create/comment/1",
            "occurred_at": "2026-05-08T10:00:00Z",
            "community_actor_id": f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers",
            "actor_id": f"https://{LEMMY_EXAMPLE_DOMAIN}/u/alice",
            "object": {
                "ap_id": object_id,
                "kind": "comment",
                "lemmy_id": 222,
                "post_ap_id": post_ap_id,
                "post_lemmy_id": 111,
                "parent_ap_id": parent_ap_id,
                "title": None,
                "body_markdown": "hello comment",
                "url": object_id,
                "published_at": "2026-05-08T10:00:00Z",
                "author_name": "alice",
            },
        }
    )


def _event_headers(delivery_id: str) -> dict[str, str]:
    """Return the trusted internal-auth headers for one event delivery."""
    return {
        "Authorization": "Bearer secret",
        "X-Bridge-Delivery-Id": delivery_id,
    }


def test_accepted_subscription_inbound_post_creates_discord_thread_and_receipt(
    tmp_path: Path,
) -> None:
    """An accepted subscription should fan a remote post into one Discord thread.

    After Phase 5 the mapping is recorded in CommunityThreadGroup, not PostLink.
    """
    database = _database(tmp_path)
    _accepted_subscription(database)
    forum_channel = SimpleNamespace(
        id=100,
        create_thread=AsyncMock(
            return_value=SimpleNamespace(
                thread=SimpleNamespace(id=200),
                message=SimpleNamespace(id=300),
            )
        )
    )
    bot = SimpleNamespace(
        wait_until_bridge_ready=AsyncMock(),
        fetch_forum_channel=AsyncMock(return_value=forum_channel),
    )
    runtime = SimpleNamespace(
        settings=SimpleNamespace(fedify_shared_secret="secret"),
        database=database,
        bot=bot,
        community_runtime=_community_runtime(database, bot=bot),
    )
    client = TestClient(create_http_app(runtime), raise_server_exceptions=False)
    event = _post_event(object_id=f"https://{LEMMY_EXAMPLE_DOMAIN}/post/111")

    response = client.post(
        "/internal/activitypub/events",
        headers=_event_headers(event.delivery_id),
        json=event.model_dump(mode="json"),
    )
    thread_group = database.get_thread_group_by_ap_object(event.object.ap_id)
    receipt = database.get_event_receipt(event.delivery_id)

    assert response.status_code == 200
    assert response.json()["status"] == "processed"
    # Mapping is now in CommunityThreadGroup, not PostLink.
    assert thread_group is not None
    assert thread_group.ap_object_id == event.object.ap_id
    deliveries = database.get_thread_deliveries(thread_group.id)
    assert len(deliveries) == 1
    assert deliveries[0].discord_thread_id == 200
    assert receipt is not None
    assert receipt.status == "processed"


def test_inbound_post_and_comment_fan_out_to_all_accepted_subscriptions(
    tmp_path: Path,
) -> None:
    """One inbound post/comment pair should create copies in every subscribed forum.

    After Phase 5 the mapping is in CommunityThreadGroup/CommunityMessageGroup.
    """
    database = _database(tmp_path)
    _accepted_subscription_for_channel(database, channel_id=100)
    _accepted_subscription_for_channel(database, channel_id=101)
    forum_channel_a = SimpleNamespace(
        id=100,
        create_thread=AsyncMock(
            return_value=SimpleNamespace(
                thread=SimpleNamespace(id=200),
                message=SimpleNamespace(id=300),
            )
        )
    )
    forum_channel_b = SimpleNamespace(
        id=101,
        create_thread=AsyncMock(
            return_value=SimpleNamespace(
                thread=SimpleNamespace(id=201),
                message=SimpleNamespace(id=301),
            )
        )
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
        if channel_id == 100:
            return forum_channel_a
        if channel_id == 101:
            return forum_channel_b
        raise AssertionError(f"Unexpected forum channel lookup {channel_id}")

    async def _get_thread_by_id(thread_id: int) -> object:
        if thread_id == 200:
            return thread_a
        if thread_id == 201:
            return thread_b
        raise AssertionError(f"Unexpected thread lookup {thread_id}")

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
    post_event = _post_event(
        object_id=f"https://{LEMMY_EXAMPLE_DOMAIN}/post/777",
        delivery_id=f"https://{LEMMY_EXAMPLE_DOMAIN}/activities/create/post/777",
    )
    comment_event = _comment_event(
        object_id=f"https://{LEMMY_EXAMPLE_DOMAIN}/comment/777",
        post_ap_id=post_event.object.ap_id,
        delivery_id=f"https://{LEMMY_EXAMPLE_DOMAIN}/activities/create/comment/777",
    )

    post_response = client.post(
        "/internal/activitypub/events",
        headers=_event_headers(post_event.delivery_id),
        json=post_event.model_dump(mode="json"),
    )
    comment_response = client.post(
        "/internal/activitypub/events",
        headers=_event_headers(comment_event.delivery_id),
        json=comment_event.model_dump(mode="json"),
    )

    thread_group = database.get_thread_group_by_ap_object(post_event.object.ap_id)
    message_group = database.get_message_group_by_ap_object(comment_event.object.ap_id)

    assert post_response.status_code == 200
    assert post_response.json()["status"] == "processed"
    assert comment_response.status_code == 200
    assert comment_response.json()["status"] == "processed"

    assert thread_group is not None
    thread_deliveries = database.get_thread_deliveries(thread_group.id)
    assert {d.discord_thread_id for d in thread_deliveries} == {200, 201}

    assert message_group is not None
    msg_deliveries = database.get_message_deliveries(message_group.id)
    assert {d.discord_thread_id for d in msg_deliveries} == {200, 201}

    assert thread_a.send.await_count == 1
    assert thread_b.send.await_count == 1


def test_accepted_subscription_inbound_comment_creates_discord_message_and_receipt(
    tmp_path: Path,
) -> None:
    """A mapped parent post should let an inbound remote comment reach Discord.

    After Phase 5 the parent is mapped via CommunityThreadGroup, not PostLink.
    """
    database = _database(tmp_path)
    _accepted_subscription(database)

    # Pre-insert the thread group for the parent post (as Phase 5 inbound would do).
    post_ap_id = f"https://{LEMMY_EXAMPLE_DOMAIN}/post/111"
    thread_group = database.create_thread_group(
        community_actor_id=f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers",
        source_channel_id=None,
        source_thread_id=None,
        source_starter_message_id=None,
        ap_object_id=post_ap_id,
    )
    database.add_thread_delivery(
        thread_group_id=thread_group.id,
        discord_channel_id=100,
        discord_thread_id=200,
        discord_starter_message_id=300,
        role="inbound",
    )

    thread = SimpleNamespace(
        id=200,
        send=AsyncMock(return_value=SimpleNamespace(id=900)),
    )
    bot = SimpleNamespace(
        wait_until_bridge_ready=AsyncMock(),
        get_thread_by_id=AsyncMock(return_value=thread),
    )
    runtime = SimpleNamespace(
        settings=SimpleNamespace(fedify_shared_secret="secret"),
        database=database,
        bot=bot,
        community_runtime=_community_runtime(database, bot=bot),
    )
    client = TestClient(create_http_app(runtime), raise_server_exceptions=False)
    event = _comment_event(
        object_id=f"https://{LEMMY_EXAMPLE_DOMAIN}/comment/111",
        post_ap_id=post_ap_id,
        delivery_id=f"https://{LEMMY_EXAMPLE_DOMAIN}/activities/create/comment/111",
    )

    response = client.post(
        "/internal/activitypub/events",
        headers=_event_headers(event.delivery_id),
        json=event.model_dump(mode="json"),
    )
    message_group = database.get_message_group_by_ap_object(event.object.ap_id)
    receipt = database.get_event_receipt(event.delivery_id)

    assert response.status_code == 200
    assert response.json()["status"] == "processed"
    # Mapping is now in CommunityMessageGroup, not CommentLink.
    assert message_group is not None
    assert message_group.ap_object_id == event.object.ap_id
    assert receipt is not None
    assert receipt.status == "processed"


@pytest.mark.asyncio
async def test_no_accepted_subscription_inbound_post_is_skipped(
    tmp_path: Path,
) -> None:
    """Without an accepted subscription the system should leave the event untouched."""
    database = _database(tmp_path)
    bot = SimpleNamespace(
        wait_until_bridge_ready=AsyncMock(),
        fetch_forum_channel=AsyncMock(),
    )
    runtime = SimpleNamespace(
        database=database,
        bot=bot,
        community_runtime=_community_runtime(database, bot=bot),
    )

    result = await dispatch_activitypub_event(
        _post_event(object_id=f"https://{LEMMY_EXAMPLE_DOMAIN}/post/222"),
        runtime,
    )

    assert result.status == "skipped"
    bot.fetch_forum_channel.assert_not_awaited()


def test_duplicate_delivery_id_returns_idempotent_duplicate_without_side_effects(
    tmp_path: Path,
) -> None:
    """A repeated delivery ID should short-circuit before a second Discord write."""
    database = _database(tmp_path)
    _accepted_subscription(database)
    forum_channel = SimpleNamespace(
        id=100,
        create_thread=AsyncMock(
            return_value=SimpleNamespace(
                thread=SimpleNamespace(id=200),
                message=SimpleNamespace(id=300),
            )
        )
    )
    bot = SimpleNamespace(
        wait_until_bridge_ready=AsyncMock(),
        fetch_forum_channel=AsyncMock(return_value=forum_channel),
    )
    runtime = SimpleNamespace(
        settings=SimpleNamespace(fedify_shared_secret="secret"),
        database=database,
        bot=bot,
        community_runtime=_community_runtime(database, bot=bot),
    )
    client = TestClient(create_http_app(runtime), raise_server_exceptions=False)
    event = _post_event(
        object_id=f"https://{LEMMY_EXAMPLE_DOMAIN}/post/333",
        delivery_id=f"https://{LEMMY_EXAMPLE_DOMAIN}/activities/create/post/333",
    )

    first_response = client.post(
        "/internal/activitypub/events",
        headers=_event_headers(event.delivery_id),
        json=event.model_dump(mode="json"),
    )
    second_response = client.post(
        "/internal/activitypub/events",
        headers=_event_headers(event.delivery_id),
        json=event.model_dump(mode="json"),
    )

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert second_response.json()["status"] == "duplicate"
    forum_channel.create_thread.assert_awaited_once()


@pytest.mark.asyncio
async def test_discord_originated_echo_is_skipped_without_creating_duplicate(
    tmp_path: Path,
) -> None:
    """A looped-back AP object from Discord publish should be suppressed as echo."""
    database = _database(tmp_path)
    _accepted_subscription(database)
    object_id = f"https://{BRIDGE_HOST_DOMAIN}/users/alice/objects/post/1"
    database.create_message_mapping(
        source_platform="discord",
        source_id="300",
        activity_id=f"https://{BRIDGE_HOST_DOMAIN}/users/alice/activities/create/post/1",
        object_id=object_id,
        actor_url=f"https://{BRIDGE_HOST_DOMAIN}/users/alice",
        community_actor_url=f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers",
        discord_channel_id=100,
        discord_message_id=300,
    )
    bot = SimpleNamespace(
        wait_until_bridge_ready=AsyncMock(),
        fetch_forum_channel=AsyncMock(),
    )
    runtime = SimpleNamespace(
        database=database,
        bot=bot,
        community_runtime=_community_runtime(database, bot=bot),
    )

    result = await dispatch_activitypub_event(_post_event(object_id=object_id), runtime)

    assert result.status == "skipped"
    assert result.detail == "discord-originated echo"
    bot.fetch_forum_channel.assert_not_awaited()


def test_comment_before_parent_mapping_becomes_deferred_then_retries_processed(
    tmp_path: Path,
) -> None:
    """A retryable out-of-order comment should succeed once the parent post is mapped.

    After Phase 5 the parent is looked up via CommunityThreadGroup, not PostLink.
    """
    database = _database(tmp_path)
    _accepted_subscription(database)
    thread = SimpleNamespace(
        id=200,
        send=AsyncMock(return_value=SimpleNamespace(id=900)),
    )
    bot = SimpleNamespace(
        wait_until_bridge_ready=AsyncMock(),
        get_thread_by_id=AsyncMock(return_value=thread),
    )
    runtime = SimpleNamespace(
        settings=SimpleNamespace(fedify_shared_secret="secret"),
        database=database,
        bot=bot,
        community_runtime=_community_runtime(database, bot=bot),
    )
    client = TestClient(create_http_app(runtime), raise_server_exceptions=False)
    post_ap_id = f"https://{LEMMY_EXAMPLE_DOMAIN}/post/111"
    event = _comment_event(
        object_id=f"https://{LEMMY_EXAMPLE_DOMAIN}/comment/222",
        post_ap_id=post_ap_id,
        delivery_id=f"https://{LEMMY_EXAMPLE_DOMAIN}/activities/create/comment/222",
    )

    # First attempt: no thread group yet — should be deferred.
    first_response = client.post(
        "/internal/activitypub/events",
        headers=_event_headers(event.delivery_id),
        json=event.model_dump(mode="json"),
    )
    first_receipt = database.get_event_receipt(event.delivery_id)

    # Now create the thread group for the parent post.
    thread_group = database.create_thread_group(
        community_actor_id=f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers",
        source_channel_id=None,
        source_thread_id=None,
        source_starter_message_id=None,
        ap_object_id=post_ap_id,
    )
    database.add_thread_delivery(
        thread_group_id=thread_group.id,
        discord_channel_id=100,
        discord_thread_id=200,
        discord_starter_message_id=300,
        role="inbound",
    )

    # Second attempt: thread group exists — should be processed.
    second_response = client.post(
        "/internal/activitypub/events",
        headers=_event_headers(event.delivery_id),
        json=event.model_dump(mode="json"),
    )
    second_receipt = database.get_event_receipt(event.delivery_id)
    message_group = database.get_message_group_by_ap_object(
        f"https://{LEMMY_EXAMPLE_DOMAIN}/comment/222"
    )

    assert first_response.status_code == 200
    assert first_response.json()["status"] == "deferred"
    assert first_receipt is not None
    assert first_receipt.status == "deferred"
    assert second_response.status_code == 200
    assert second_response.json()["status"] == "processed"
    assert second_receipt is not None
    assert second_receipt.status == "processed"
    # Mapping is now in CommunityMessageGroup, not CommentLink.
    assert message_group is not None


def test_discord_target_failure_marks_inbound_receipt_failed(
    tmp_path: Path,
) -> None:
    """A Discord-side failure must not leave a false processed inbound receipt."""
    database = _database(tmp_path)
    _accepted_subscription(database)
    bot = SimpleNamespace(
        wait_until_bridge_ready=AsyncMock(),
        fetch_forum_channel=AsyncMock(
            return_value=SimpleNamespace(
                id=100,
                create_thread=AsyncMock(side_effect=RuntimeError("discord create failed"))
            )
        ),
    )
    runtime = SimpleNamespace(
        settings=SimpleNamespace(fedify_shared_secret="secret"),
        database=database,
        bot=bot,
        community_runtime=_community_runtime(database, bot=bot),
    )
    client = TestClient(create_http_app(runtime), raise_server_exceptions=False)
    event = _post_event(
        object_id=f"https://{LEMMY_EXAMPLE_DOMAIN}/post/444",
        delivery_id=f"https://{LEMMY_EXAMPLE_DOMAIN}/activities/create/post/444",
    )

    response = client.post(
        "/internal/activitypub/events",
        headers=_event_headers(event.delivery_id),
        json=event.model_dump(mode="json"),
    )
    receipt = database.get_event_receipt(event.delivery_id)

    assert response.status_code == 500
    assert receipt is not None
    assert receipt.status == "failed"
