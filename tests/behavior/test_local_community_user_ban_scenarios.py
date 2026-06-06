"""Behavior scenarios for community-scoped local user ban enforcement."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from src.content_publish_service import ContentPublishService
from src.http_api import create_http_app
from src.local_communities.runtime import LocalCommunityRuntime
from src.local_communities.service import LocalCommunityService
from support.db import build_database
from support.discord import build_bot, build_forum_channel_tuple_result


def _settings() -> SimpleNamespace:
    """Build the settings fields used by internal event intake and dispatch."""
    return SimpleNamespace(
        fedify_shared_secret="secret",
        federation_allowlist=[],
        registration_session_cookie_name="bridge_registration_session",
        registration_session_ttl_seconds=3600,
    )


def _runtime(tmp_path: Path, name: str = "ban-http.db") -> SimpleNamespace:
    """Build a real local-community runtime behind the HTTP ingestion route."""
    database = build_database(tmp_path, name)
    gateway = AsyncMock()
    local_runtime = LocalCommunityRuntime(
        database=database,
        fedify_gateway=gateway,
        content_publish_service=ContentPublishService(
            database=database,
            fedify_gateway=gateway,
            bridge_prefix="[bridge]",
        ),
        bridge_prefix="[bridge]",
    )
    return SimpleNamespace(
        database=database,
        local_community_runtime=local_runtime,
        community_runtime=SimpleNamespace(),
        settings=_settings(),
        bot=SimpleNamespace(),
    )


def _local_community(database: object, *, slug: str = "cats", forum_channel_id: int = 100) -> object:
    """Seed one local community and return its persisted row."""
    LocalCommunityService(
        database=database,
        base_url="https://bridge.example",
        keypair_generator=lambda: ("public-key", "private-key"),
    ).create_local_community(
        discord_guild_id=10,
        discord_forum_channel_id=forum_channel_id,
        slug=slug,
        name=slug.title(),
        description=f"{slug} community.",
        created_by_discord_user_id="123",
    )
    return database.local_communities.get_local_community_by_slug(slug)


def _post_payload(*, delivery_id: str, actor_id: str, community_actor_id: str) -> dict[str, object]:
    """Build one inbound post payload in the gateway-to-Python contract."""
    return {
        "event_type": "post.created",
        "delivery_id": delivery_id,
        "occurred_at": "2026-05-19T10:00:00Z",
        "community_actor_id": community_actor_id,
        "actor_id": actor_id,
        "object": {
            "ap_id": "https://example.com/post/1",
            "kind": "post",
            "lemmy_id": 1,
            "post_ap_id": None,
            "post_lemmy_id": None,
            "parent_ap_id": None,
            "title": "Remote topic",
            "body_markdown": "spam content",
            "url": "https://example.com/post/1",
            "published_at": "2026-05-19T10:00:00Z",
            "author_name": "alice",
        },
    }


def _comment_payload(*, delivery_id: str, actor_id: str, community_actor_id: str) -> dict[str, object]:
    """Build one inbound comment payload in the gateway-to-Python contract."""
    return {
        "event_type": "comment.created",
        "delivery_id": delivery_id,
        "occurred_at": "2026-05-19T10:05:00Z",
        "community_actor_id": community_actor_id,
        "actor_id": actor_id,
        "object": {
            "ap_id": "https://example.com/comment/1",
            "kind": "comment",
            "lemmy_id": 2,
            "post_ap_id": "https://example.com/post/1",
            "post_lemmy_id": 1,
            "parent_ap_id": "https://example.com/post/1",
            "title": None,
            "body_markdown": "banned reply",
            "url": "https://example.com/comment/1",
            "published_at": "2026-05-19T10:05:00Z",
            "author_name": "alice",
        },
    }


def _follow_payload(*, delivery_id: str, actor_id: str, community_actor_id: str) -> dict[str, object]:
    """Build one inbound Follow request payload for a local community."""
    return {
        "event_type": "local.follow_requested",
        "delivery_id": delivery_id,
        "occurred_at": "2026-05-19T10:00:00Z",
        "community_actor_id": community_actor_id,
        "actor_id": actor_id,
        "object": {
            "follow_activity_id": delivery_id,
            "remote_inbox_url": f"{actor_id}/inbox",
        },
    }


def _unfollow_payload(*, delivery_id: str, actor_id: str, community_actor_id: str) -> dict[str, object]:
    """Build one inbound Undo(Follow) payload for a local community."""
    return {
        "event_type": "local.unfollow_requested",
        "delivery_id": delivery_id,
        "occurred_at": "2026-05-19T10:00:00Z",
        "community_actor_id": community_actor_id,
        "actor_id": actor_id,
        "object": {
            "follow_activity_id": "https://example.com/activities/follow/original",
        },
    }


def _post_event(client: TestClient, payload: dict[str, object]):
    """Post one internal event with the required gateway auth headers."""
    return client.post(
        "/internal/activitypub/events",
        headers={
            "Authorization": "Bearer secret",
            "X-Bridge-Delivery-Id": str(payload["delivery_id"]),
        },
        json=payload,
    )


def test_banned_actor_post_is_acked_and_skipped_before_discord_side_effects(tmp_path: Path) -> None:
    """A banned remote post should write a receipt but create no local surfaces."""
    runtime = _runtime(tmp_path)
    community = _local_community(runtime.database)
    runtime.database.community_actor_bans.create_active_ban(
        local_community_id=community.id,
        actor_handle="alice@example.com",
        actor_url=None,
        created_by_discord_user_id="123",
        reason="spam",
    )
    runtime.local_community_runtime.bot = SimpleNamespace(
        wait_until_bridge_ready=AsyncMock(),
        fetch_forum_channel=AsyncMock(return_value=build_forum_channel_tuple_result(channel_id=100, thread_id=200, starter_message_id=300)),
    )
    client = TestClient(create_http_app(runtime), raise_server_exceptions=False)
    payload = _post_payload(
        delivery_id="https://example.com/activities/create/post/1",
        actor_id="https://example.com/u/alice",
        community_actor_id=community.actor_url,
    )

    response = _post_event(client, payload)
    receipt = runtime.database.event_receipts.get_event_receipt(str(payload["delivery_id"]))

    assert response.status_code == 200
    assert response.json() == {"status": "skipped", "outcome": "ignored_by_ban", "detail": "actor is banned for this community"}
    assert receipt is not None
    assert receipt.status == "skipped"
    assert receipt.detail == "actor is banned for this community"
    assert receipt.outcome == "ignored_by_ban"
    assert runtime.database.local_community_content.get_local_community_thread_by_ap_object_id("https://example.com/post/1") is None
    runtime.local_community_runtime.bot.fetch_forum_channel.assert_not_awaited()


def test_banned_actor_comment_is_acked_and_skipped_before_message_side_effects(tmp_path: Path) -> None:
    """A banned remote comment must not create Discord messages or mappings."""
    runtime = _runtime(tmp_path, "ban-comment.db")
    community = _local_community(runtime.database)
    runtime.database.community_actor_bans.create_active_ban(
        local_community_id=community.id,
        actor_handle="alice@example.com",
        actor_url=None,
        created_by_discord_user_id="123",
        reason=None,
    )
    client = TestClient(create_http_app(runtime), raise_server_exceptions=False)
    payload = _comment_payload(
        delivery_id="https://example.com/activities/create/comment/1",
        actor_id="https://example.com/users/alice",
        community_actor_id=community.actor_url,
    )

    response = _post_event(client, payload)

    assert response.status_code == 200
    assert response.json()["status"] == "skipped"
    assert runtime.database.local_community_content.get_local_community_message_by_ap_object_id("https://example.com/comment/1") is None
    assert runtime.database.message_mappings.get_message_mapping_by_object_id("https://example.com/comment/1") is None


def test_banned_actor_follow_is_acked_and_does_not_create_subscriber_or_accept(tmp_path: Path) -> None:
    """A banned Follow is acknowledged locally but no subscriber row or Accept is emitted."""
    runtime = _runtime(tmp_path, "ban-follow.db")
    community = _local_community(runtime.database)
    runtime.database.community_actor_bans.create_active_ban(
        local_community_id=community.id,
        actor_handle="alice@example.com",
        actor_url=None,
        created_by_discord_user_id="123",
        reason="spam",
    )
    client = TestClient(create_http_app(runtime), raise_server_exceptions=False)
    payload = _follow_payload(
        delivery_id="https://example.com/activities/follow/1",
        actor_id="https://example.com/u/alice",
        community_actor_id=community.actor_url,
    )

    response = _post_event(client, payload)

    assert response.status_code == 200
    assert response.json() == {"status": "skipped", "outcome": "ignored_by_ban", "detail": "actor is banned for this community"}
    assert runtime.database.remote_subscribers.get_remote_subscriber(local_community_id=community.id, remote_actor_id="https://example.com/u/alice") is None
    runtime.local_community_runtime.fedify_gateway.accept_local_community_follow.assert_not_awaited()


def test_banned_actor_unfollow_is_acked_and_does_not_remove_existing_subscriber(tmp_path: Path) -> None:
    """A banned Undo(Follow) is skipped without mutating existing subscriber state."""
    runtime = _runtime(tmp_path, "ban-unfollow.db")
    community = _local_community(runtime.database)
    runtime.database.community_actor_bans.create_active_ban(
        local_community_id=community.id,
        actor_handle="alice@example.com",
        actor_url=None,
        created_by_discord_user_id="123",
        reason="spam",
    )
    runtime.database.remote_subscribers.create_remote_subscriber(
        local_community_id=community.id,
        remote_actor_id="https://example.com/u/alice",
        remote_inbox_url="https://example.com/u/alice/inbox",
        follow_activity_id="https://example.com/activities/follow/original",
    )
    client = TestClient(create_http_app(runtime), raise_server_exceptions=False)
    payload = _unfollow_payload(
        delivery_id="https://example.com/activities/undo/follow/1",
        actor_id="https://example.com/u/alice",
        community_actor_id=community.actor_url,
    )

    response = _post_event(client, payload)

    assert response.status_code == 200
    assert response.json() == {"status": "skipped", "outcome": "ignored_by_ban", "detail": "actor is banned for this community"}
    assert runtime.database.remote_subscribers.get_remote_subscriber(local_community_id=community.id, remote_actor_id="https://example.com/u/alice") is not None


def test_duplicate_delivery_for_banned_activity_stays_idempotent(tmp_path: Path) -> None:
    """The first banned delivery records skipped state and the second is duplicate."""
    runtime = _runtime(tmp_path, "ban-duplicate.db")
    community = _local_community(runtime.database)
    runtime.database.community_actor_bans.create_active_ban(
        local_community_id=community.id,
        actor_handle="alice@example.com",
        actor_url=None,
        created_by_discord_user_id="123",
        reason="spam",
    )
    client = TestClient(create_http_app(runtime), raise_server_exceptions=False)
    payload = _post_payload(
        delivery_id="https://example.com/activities/create/post/dupe",
        actor_id="https://example.com/u/alice",
        community_actor_id=community.actor_url,
    )

    first = _post_event(client, payload)
    second = _post_event(client, payload)

    assert first.json() == {"status": "skipped", "outcome": "ignored_by_ban", "detail": "actor is banned for this community"}
    assert second.json() == {"status": "duplicate", "outcome": "ignored_by_ban", "detail": "actor is banned for this community"}
    assert runtime.database.local_community_content.get_local_community_thread_by_ap_object_id("https://example.com/post/1") is None


def test_same_actor_banned_in_one_local_community_is_not_blocked_in_another(tmp_path: Path) -> None:
    """Ban matching must stay scoped by local_community_id, not actor handle alone."""
    runtime = _runtime(tmp_path, "ban-scope.db")
    cats = _local_community(runtime.database, slug="cats", forum_channel_id=100)
    dogs = _local_community(runtime.database, slug="dogs", forum_channel_id=101)
    runtime.database.community_actor_bans.create_active_ban(
        local_community_id=cats.id,
        actor_handle="alice@example.com",
        actor_url=None,
        created_by_discord_user_id="123",
        reason="spam",
    )
    runtime.database.remote_subscribers.create_remote_subscriber(
        local_community_id=dogs.id,
        remote_actor_id="https://example.com/u/alice",
        remote_inbox_url="https://example.com/u/alice/inbox",
        follow_activity_id="https://example.com/activities/follow/dogs",
    )
    runtime.local_community_runtime.bot = build_bot(
        forum_channels={101: build_forum_channel_tuple_result(channel_id=101, thread_id=201, starter_message_id=301)}
    )
    client = TestClient(create_http_app(runtime), raise_server_exceptions=False)
    payload = _post_payload(
        delivery_id="https://example.com/activities/create/post/dogs",
        actor_id="https://example.com/u/alice",
        community_actor_id=dogs.actor_url,
    )

    response = _post_event(client, payload)

    assert response.status_code == 200
    assert response.json()["status"] == "processed"
    assert runtime.database.local_community_content.get_local_community_thread_by_ap_object_id("https://example.com/post/1") is not None
