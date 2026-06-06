"""Runtime scenarios for Stage 7 deduplication and inbound hardening."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from src.activitypub_handlers import dispatch_activitypub_event
from src.activitypub_models import ActivityPubEvent
from src.community_sync.runtime import CommunityRuntime
from src.db import Database
from src.content_publish_service import ContentPublishService
from src.http_api import create_http_app
from tests_constants import BRIDGE_HOST_DOMAIN, LEMMY_EXAMPLE_DOMAIN


def _database(tmp_path: Path) -> Database:
    """Create a real SQLite-backed repository for Stage 7 runtime scenarios."""
    database = Database(f"sqlite:///{tmp_path / 'bridge-stage7.db'}")
    database.create_all()
    return database


def _community_runtime(database: Database) -> CommunityRuntime:
    """Build a real CommunityRuntime for inbound routing scenarios.

    ContentPublishService is not called for inbound events, so it is
    constructed with a stub gateway.
    """
    publish_service = ContentPublishService(
        database=database,
        fedify_gateway=AsyncMock(),
        bridge_prefix="[bridge]",
    )
    return CommunityRuntime(database=database, content_publish_service=publish_service)


def _accepted_subscription(database: Database) -> None:
    """Insert one active accepted subscription for inbound community routing."""
    database.remote_subscriptions.create_subscription(
        discord_channel_id=100,
        lemmy_community_actor_id=f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers",
        lemmy_community_name="hackers",
        lemmy_community_id=42,
        community_handle=f"!hackers@{LEMMY_EXAMPLE_DOMAIN}",
        community_inbox_url=f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers/inbox",
        follow_activity_id=f"https://{BRIDGE_HOST_DOMAIN}/activities/follow/1",
        status="accepted",
    )


def _post_event(*, object_id: str, delivery_id: str | None = None) -> ActivityPubEvent:
    """Build one normalized inbound post event for dedup and receipt tests."""
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
    delivery_id: str | None = None,
) -> ActivityPubEvent:
    """Build one normalized inbound comment event for dedup and retry tests."""
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
                "parent_ap_id": None,
                "title": None,
                "body_markdown": "hello comment",
                "url": object_id,
                "published_at": "2026-05-08T10:00:00Z",
                "author_name": "alice",
            },
        }
    )


async def test_inbound_post_with_discord_originated_mapping_is_skipped_as_echo(
    tmp_path: Path,
) -> None:
    """A looped-back Discord-originated post should not create a Discord thread."""
    database = _database(tmp_path)
    _accepted_subscription(database)
    object_id = f"https://{BRIDGE_HOST_DOMAIN}/users/alice/objects/post/1"
    database.message_mappings.create_message_mapping(
        source_platform="discord",
        source_id="300",
        activity_id=f"https://{BRIDGE_HOST_DOMAIN}/users/alice/activities/create/post/1",
        object_id=object_id,
        actor_url=f"https://{BRIDGE_HOST_DOMAIN}/users/alice",
        community_actor_url=f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers",
        discord_channel_id=100,
        discord_message_id=300,
    )
    runtime = SimpleNamespace(
        database=database,
        bot=SimpleNamespace(
            wait_until_bridge_ready=AsyncMock(),
            fetch_forum_channel=AsyncMock(),
        ),
        community_runtime=_community_runtime(database),
    )

    result = await dispatch_activitypub_event(
        _post_event(object_id=object_id),
        runtime,
    )

    assert result.status == "skipped"
    assert result.detail == "discord-originated echo"
    assert result.outcome.value == "ignored_discord_originated_echo"
    runtime.bot.fetch_forum_channel.assert_not_awaited()


async def test_inbound_comment_with_discord_originated_mapping_is_skipped_as_echo(
    tmp_path: Path,
) -> None:
    """A looped-back Discord-originated comment should not create a Discord reply."""
    database = _database(tmp_path)
    _accepted_subscription(database)
    object_id = f"https://{BRIDGE_HOST_DOMAIN}/users/alice/objects/comment/1"
    database.message_mappings.create_message_mapping(
        source_platform="discord",
        source_id="301",
        activity_id=f"https://{BRIDGE_HOST_DOMAIN}/users/alice/activities/create/comment/1",
        object_id=object_id,
        actor_url=f"https://{BRIDGE_HOST_DOMAIN}/users/alice",
        community_actor_url=f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers",
        discord_channel_id=100,
        discord_message_id=301,
    )
    runtime = SimpleNamespace(
        database=database,
        bot=SimpleNamespace(
            wait_until_bridge_ready=AsyncMock(),
            get_thread_by_id=AsyncMock(),
        ),
        community_runtime=_community_runtime(database),
    )

    result = await dispatch_activitypub_event(
        _comment_event(
            object_id=object_id,
            post_ap_id=f"https://{BRIDGE_HOST_DOMAIN}/users/alice/post/111",
        ),
        runtime,
    )

    assert result.status == "skipped"
    assert result.detail == "discord-originated echo"
    assert result.outcome.value == "ignored_discord_originated_echo"
    runtime.bot.get_thread_by_id.assert_not_awaited()


def test_out_of_order_comment_receipt_becomes_deferred_and_retries_successfully(
    tmp_path: Path,
) -> None:
    """A comment that arrives before its parent mapping should be retryable."""
    database = _database(tmp_path)
    _accepted_subscription(database)
    runtime = SimpleNamespace(
        settings=SimpleNamespace(fedify_shared_secret="secret"),
        database=database,
        bot=SimpleNamespace(
            wait_until_bridge_ready=AsyncMock(),
            get_thread_by_id=AsyncMock(
                return_value=SimpleNamespace(
                    id=200,
                    send=AsyncMock(return_value=SimpleNamespace(id=900)),
                )
            ),
        ),
        community_runtime=_community_runtime(database),
    )
    client = TestClient(create_http_app(runtime), raise_server_exceptions=False)
    event = _comment_event(
        object_id=f"https://{LEMMY_EXAMPLE_DOMAIN}/comment/222",
        post_ap_id=f"https://{LEMMY_EXAMPLE_DOMAIN}/post/111",
        delivery_id=f"https://{LEMMY_EXAMPLE_DOMAIN}/activities/create/comment/222",
    )
    headers = {
        "Authorization": "Bearer secret",
        "X-Bridge-Delivery-Id": event.delivery_id,
    }

    first_response = client.post(
        "/internal/activitypub/events",
        headers=headers,
        json=event.model_dump(mode="json"),
    )
    receipt_after_first = database.event_receipts.get_event_receipt(event.delivery_id)

    assert first_response.status_code == 200
    assert first_response.json()["status"] == "deferred"
    assert first_response.json()["outcome"] == "deferred_missing_dependency"
    assert receipt_after_first is not None
    assert receipt_after_first.status == "deferred"
    assert receipt_after_first.outcome == "deferred_missing_dependency"

    post_ap_id = f"https://{LEMMY_EXAMPLE_DOMAIN}/post/111"
    thread_group = database.discord_fanout_groups.create_thread_group(
        community_actor_id=f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers",
        source_channel_id=None,
        source_thread_id=None,
        source_starter_message_id=None,
        ap_activity_id=f"https://{LEMMY_EXAMPLE_DOMAIN}/activities/create/post/111",
        ap_object_id=post_ap_id,
    )
    database.discord_fanout_groups.add_thread_delivery(
        thread_group_id=thread_group.id,
        discord_channel_id=100,
        discord_thread_id=200,
        discord_starter_message_id=300,
        role="inbound",
    )
    second_response = client.post(
        "/internal/activitypub/events",
        headers=headers,
        json=event.model_dump(mode="json"),
    )
    receipt_after_second = database.event_receipts.get_event_receipt(event.delivery_id)
    comment_ap_id = f"https://{LEMMY_EXAMPLE_DOMAIN}/comment/222"
    created_message_group = database.discord_fanout_groups.get_message_group_by_ap_object(comment_ap_id)

    assert second_response.status_code == 200
    assert second_response.json()["status"] == "processed"
    assert second_response.json()["outcome"] == "applied"
    assert receipt_after_second is not None
    assert receipt_after_second.status == "processed"
    assert receipt_after_second.outcome == "applied"
    assert created_message_group is not None


def test_failed_inbound_discord_fanout_marks_receipt_failed(tmp_path: Path) -> None:
    """A Discord-side failure must leave the delivery receipt in `failed`."""
    database = _database(tmp_path)
    _accepted_subscription(database)
    runtime = SimpleNamespace(
        settings=SimpleNamespace(fedify_shared_secret="secret"),
        database=database,
        bot=SimpleNamespace(
            wait_until_bridge_ready=AsyncMock(),
            fetch_forum_channel=AsyncMock(
                return_value=SimpleNamespace(
                    id=100,
                    create_thread=AsyncMock(side_effect=RuntimeError("discord create failed"))
                )
            ),
        ),
        community_runtime=_community_runtime(database),
    )
    client = TestClient(create_http_app(runtime), raise_server_exceptions=False)
    event = _post_event(
        object_id=f"https://{LEMMY_EXAMPLE_DOMAIN}/post/111",
        delivery_id=f"https://{LEMMY_EXAMPLE_DOMAIN}/activities/create/post/111",
    )
    response = client.post(
        "/internal/activitypub/events",
        headers={
            "Authorization": "Bearer secret",
            "X-Bridge-Delivery-Id": event.delivery_id,
        },
        json=event.model_dump(mode="json"),
    )
    receipt = database.event_receipts.get_event_receipt(event.delivery_id)

    assert response.status_code == 500
    assert receipt is not None
    assert receipt.status == "failed"
    assert receipt.outcome == "processing_failed"


def test_duplicate_inbound_delivery_returns_duplicate_without_side_effects(
    tmp_path: Path,
) -> None:
    """A second HTTP delivery with the same receipt ID should short-circuit."""
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
    runtime = SimpleNamespace(
        settings=SimpleNamespace(fedify_shared_secret="secret"),
        database=database,
        bot=SimpleNamespace(
            wait_until_bridge_ready=AsyncMock(),
            fetch_forum_channel=AsyncMock(return_value=forum_channel),
        ),
        community_runtime=_community_runtime(database),
    )
    client = TestClient(create_http_app(runtime), raise_server_exceptions=False)
    event = _post_event(
        object_id=f"https://{LEMMY_EXAMPLE_DOMAIN}/post/333",
        delivery_id=f"https://{LEMMY_EXAMPLE_DOMAIN}/activities/create/post/333",
    )
    headers = {
        "Authorization": "Bearer secret",
        "X-Bridge-Delivery-Id": event.delivery_id,
    }

    first_response = client.post(
        "/internal/activitypub/events",
        headers=headers,
        json=event.model_dump(mode="json"),
    )
    second_response = client.post(
        "/internal/activitypub/events",
        headers=headers,
        json=event.model_dump(mode="json"),
    )

    assert first_response.status_code == 200
    assert first_response.json()["status"] == "processed"
    assert second_response.status_code == 200
    assert second_response.json()["status"] == "duplicate"
    assert second_response.json()["outcome"] == "applied"
    forum_channel.create_thread.assert_awaited_once()
