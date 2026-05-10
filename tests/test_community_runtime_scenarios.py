"""Scenario tests for CommunityRuntime Phase 0 routing boundaries.

Each test verifies that CommunityRuntime preserves the observable effects of
the existing bridge logic when called through the new entry points. All four
tests run through real DB, real CommunityRuntime, and real handler logic.
Only outer boundaries are mocked: FedifyGatewayClient and Discord SDK calls.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.community_sync.runtime import CommunityRuntime
from src.db import Database
from src.discord_publish_service import DiscordPublishService
from src.fedify_gateway_client import PublishContentResult
from tests_constants import BRIDGE_HOST_DOMAIN, LEMMY_EXAMPLE_DOMAIN


def _database(tmp_path: Path) -> Database:
    """Create one real SQLite repository for CommunityRuntime scenario tests."""
    database = Database(f"sqlite:///{tmp_path / 'community-runtime.db'}")
    database.create_all()
    return database


def _accepted_subscription(database: Database, *, channel_id: int = 100) -> None:
    """Insert one accepted community subscription for routing scenarios."""
    community_actor_url = f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers"
    database.create_subscription(
        discord_channel_id=channel_id,
        lemmy_community_actor_id=community_actor_url,
        lemmy_community_name="hackers",
        lemmy_community_id=42,
        community_handle=f"!hackers@{LEMMY_EXAMPLE_DOMAIN}",
        community_inbox_url=f"{community_actor_url}/inbox",
        follow_activity_id=f"https://{BRIDGE_HOST_DOMAIN}/activities/follow/1",
        status="accepted",
    )


def _registered_user(database: Database) -> None:
    """Insert one registered local user actor for outbound publish scenarios."""
    actor_url = f"https://{BRIDGE_HOST_DOMAIN}/users/alice"
    database.create_user(
        discord_user_id="123",
        activitypub_username="alice",
        actor_url=actor_url,
        inbox_url=f"{actor_url}/inbox",
        outbox_url=f"{actor_url}/outbox",
        followers_url=f"{actor_url}/followers",
        public_key_pem="public-key",
        private_key_pem="private-key",
    )


def _publish_service(database: Database, fedify_gateway: AsyncMock) -> DiscordPublishService:
    """Build a DiscordPublishService wired to one fake gateway boundary."""
    return DiscordPublishService(
        database=database,
        fedify_gateway=fedify_gateway,
        bridge_prefix="[bridge]",
    )


def _community_runtime(
    database: Database,
    fedify_gateway: AsyncMock,
    *,
    bot: object | None = None,
) -> CommunityRuntime:
    """Build a real CommunityRuntime with one fake gateway boundary."""
    runtime = CommunityRuntime(
        database=database,
        discord_publish_service=_publish_service(database, fedify_gateway),
    )
    runtime.bot = bot
    return runtime


def _fake_thread(*, thread_id: int = 200, channel_id: int = 100) -> SimpleNamespace:
    """Return one fake Discord forum thread for outbound publish scenarios."""
    return SimpleNamespace(id=thread_id, parent_id=channel_id, name="Thread title")


def _fake_starter_message(*, message_id: int = 300, author_id: int = 123) -> SimpleNamespace:
    """Return one fake Discord starter message for thread publish scenarios."""
    return SimpleNamespace(
        id=message_id,
        content="hello from discord",
        author=SimpleNamespace(id=author_id, display_name="Alice", name="alice"),
        reply=AsyncMock(),
    )


def _fake_message(
    *,
    thread: SimpleNamespace,
    message_id: int = 301,
    author_id: int = 123,
    reference: object | None = None,
) -> SimpleNamespace:
    """Return one fake Discord thread message for comment publish scenarios."""
    return SimpleNamespace(
        id=message_id,
        content="hello comment",
        author=SimpleNamespace(id=author_id, display_name="Alice", name="alice"),
        channel=thread,
        reference=reference,
        reply=AsyncMock(),
    )


def _post_event(
    *,
    object_id: str,
    community_actor_id: str | None = None,
    actor_id: str | None = None,
    delivery_id: str | None = None,
) -> object:
    """Build one inbound post event for CommunityRuntime routing scenarios."""
    from src.activitypub_models import ActivityPubEvent
    return ActivityPubEvent.model_validate({
        "event_type": "post.created",
        "delivery_id": delivery_id or f"https://{LEMMY_EXAMPLE_DOMAIN}/activities/create/post/99",
        "occurred_at": "2026-05-10T10:00:00Z",
        "community_actor_id": community_actor_id or f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers",
        "actor_id": actor_id or f"https://{LEMMY_EXAMPLE_DOMAIN}/u/alice",
        "object": {
            "ap_id": object_id,
            "kind": "post",
            "lemmy_id": 99,
            "post_ap_id": None,
            "post_lemmy_id": None,
            "parent_ap_id": None,
            "title": "Test post",
            "body_markdown": "test body",
            "url": object_id,
            "published_at": "2026-05-10T10:00:00Z",
            "author_name": "alice",
        },
    })


def _comment_event(
    *,
    object_id: str,
    post_ap_id: str,
    actor_id: str | None = None,
    delivery_id: str | None = None,
) -> object:
    """Build one inbound comment event for CommunityRuntime routing scenarios."""
    from src.activitypub_models import ActivityPubEvent
    return ActivityPubEvent.model_validate({
        "event_type": "comment.created",
        "delivery_id": delivery_id or f"https://{LEMMY_EXAMPLE_DOMAIN}/activities/create/comment/55",
        "occurred_at": "2026-05-10T10:00:00Z",
        "community_actor_id": f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers",
        "actor_id": actor_id or f"https://{LEMMY_EXAMPLE_DOMAIN}/u/alice",
        "object": {
            "ap_id": object_id,
            "kind": "comment",
            "lemmy_id": 55,
            "post_ap_id": post_ap_id,
            "post_lemmy_id": 99,
            "parent_ap_id": None,
            "title": None,
            "body_markdown": "test comment body",
            "url": object_id,
            "published_at": "2026-05-10T10:00:00Z",
            "author_name": "alice",
        },
    })


@pytest.mark.asyncio
async def test_community_runtime_thread_create_publishes_and_persists(
    tmp_path: Path,
) -> None:
    """handle_discord_thread_create should publish via gateway and persist all mapping rows.

    System state: one accepted subscription for channel 100, one registered user alice.
    Action: call handle_discord_thread_create with a thread in channel 100.
    Assert: result.status is 'published', PostLink exists, MessageMapping exists,
    PublishedActivityObject exists, gateway was called exactly once.
    """
    database = _database(tmp_path)
    _accepted_subscription(database)
    _registered_user(database)
    post_object_url = f"https://{BRIDGE_HOST_DOMAIN}/users/alice/objects/post/1"
    post_activity_url = f"https://{BRIDGE_HOST_DOMAIN}/users/alice/activities/create/post/1"
    fedify_gateway = AsyncMock()
    fedify_gateway.publish_content.return_value = PublishContentResult(
        activity_id=post_activity_url,
        object_id=post_object_url,
        community_actor_url=f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers",
    )
    runtime = _community_runtime(database, fedify_gateway)
    thread = _fake_thread()
    starter_message = _fake_starter_message()

    result = await runtime.handle_discord_thread_create(thread=thread, starter_message=starter_message)

    mapping = database.get_message_mapping_by_discord_message_id(starter_message.id)
    stored_object = database.get_published_activity_object_by_object_id(post_object_url)
    thread_group = database.get_thread_group_by_source_thread(thread.id)

    assert result.status == "published"
    # CommunityThreadGroup must exist with the AP object id for reply resolution.
    assert thread_group is not None
    assert thread_group.ap_object_id == post_object_url
    # MessageMapping must exist for echo suppression when Lemmy loops the activity back.
    assert mapping is not None
    assert mapping.activity_id == post_activity_url
    # PublishedActivityObject must exist so the gateway can serve the AP object later.
    assert stored_object is not None
    assert stored_object.kind == "post"
    assert stored_object.in_reply_to_object_id is None
    # Boundary: gateway was called exactly once (one AP publish per thread create).
    fedify_gateway.publish_content.assert_awaited_once()


@pytest.mark.asyncio
async def test_community_runtime_thread_message_publishes_as_comment(
    tmp_path: Path,
) -> None:
    """handle_discord_message should publish a comment and persist all mapping rows.

    System state: one accepted subscription for channel 100, one registered user alice,
    one PostLink for thread 200. Action: call handle_discord_message with message 301
    in thread 200. Assert: result.status is 'published', CommentLink exists,
    MessageMapping exists, PublishedActivityObject with kind='comment' exists.
    """
    database = _database(tmp_path)
    _accepted_subscription(database)
    _registered_user(database)
    thread = _fake_thread()
    post_object_url = f"https://{BRIDGE_HOST_DOMAIN}/users/alice/objects/post/1"
    comment_object_url = f"https://{BRIDGE_HOST_DOMAIN}/users/alice/objects/comment/1"
    comment_activity_url = f"https://{BRIDGE_HOST_DOMAIN}/users/alice/activities/create/comment/1"
    # CommunityThreadGroup lets publish_thread_message resolve the post context.
    database.create_thread_group(
        community_actor_id=f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers",
        source_channel_id=thread.parent_id,
        source_thread_id=thread.id,
        source_starter_message_id=300,
        ap_activity_id=f"https://{BRIDGE_HOST_DOMAIN}/users/alice/activities/create/post/1",
        ap_object_id=post_object_url,
    )
    fedify_gateway = AsyncMock()
    fedify_gateway.publish_content.return_value = PublishContentResult(
        activity_id=comment_activity_url,
        object_id=comment_object_url,
        community_actor_url=f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers",
    )
    runtime = _community_runtime(database, fedify_gateway)
    message = _fake_message(thread=thread, message_id=301)

    result = await runtime.handle_discord_message(message=message)

    mapping = database.get_message_mapping_by_discord_message_id(message.id)
    stored_object = database.get_published_activity_object_by_object_id(comment_object_url)
    message_group = database.get_message_group_by_source_message(message.id)

    assert result.status == "published"
    # CommunityMessageGroup must exist for reply chain resolution.
    assert message_group is not None
    assert message_group.ap_object_id == comment_object_url
    # MessageMapping must exist for echo suppression on the Announce loop-back.
    assert mapping is not None
    assert mapping.object_id == comment_object_url
    # PublishedActivityObject must exist with the correct kind and reply target.
    assert stored_object is not None
    assert stored_object.kind == "comment"
    assert stored_object.in_reply_to_object_id == post_object_url
    fedify_gateway.publish_content.assert_awaited_once()


@pytest.mark.asyncio
async def test_community_runtime_inbound_post_creates_discord_thread(
    tmp_path: Path,
) -> None:
    """handle_inbound_post should fan an AP post into each subscribed Discord channel.

    System state: one accepted subscription for channel 100 bound to hackers community.
    No existing PostLink for the incoming AP post.
    Action: call handle_inbound_post with a post event from the hackers community.
    Assert: HandlerResult.status is 'processed', PostLink exists for the AP post ID.
    """
    database = _database(tmp_path)
    _accepted_subscription(database)
    post_ap_id = f"https://{LEMMY_EXAMPLE_DOMAIN}/post/99"
    # Forum channel and bot are mocked — Discord SDK is the outer boundary here.
    fake_forum_channel = SimpleNamespace(
        id=100,
        create_thread=AsyncMock(
            return_value=SimpleNamespace(
                thread=SimpleNamespace(id=200),
                message=SimpleNamespace(id=300),
            )
        ),
    )
    fake_bot = SimpleNamespace(
        wait_until_bridge_ready=AsyncMock(),
        fetch_forum_channel=AsyncMock(return_value=fake_forum_channel),
    )
    fedify_gateway = AsyncMock()
    community_rt = _community_runtime(database, fedify_gateway, bot=fake_bot)
    runtime_obj = SimpleNamespace(
        database=database,
        bot=fake_bot,
        community_runtime=community_rt,
    )
    event = _post_event(object_id=post_ap_id)

    result = await community_rt.handle_inbound_post(event, runtime_obj)

    thread_group = database.get_thread_group_by_ap_object(post_ap_id)

    assert result.status == "processed"
    # CommunityThreadGroup must exist so later inbound comments can resolve the thread.
    assert thread_group is not None


@pytest.mark.asyncio
async def test_community_runtime_inbound_comment_creates_discord_message(
    tmp_path: Path,
) -> None:
    """handle_inbound_comment should deliver an AP comment to the mapped Discord thread.

    System state: one accepted subscription for channel 100, one PostLink mapping
    AP post 99 to Discord thread 200. No existing CommentLink for comment 55.
    Action: call handle_inbound_comment with a comment event for post 99.
    Assert: HandlerResult.status is 'processed', CommentLink exists for comment 55.
    """
    database = _database(tmp_path)
    _accepted_subscription(database)
    post_ap_id = f"https://{LEMMY_EXAMPLE_DOMAIN}/post/99"
    comment_ap_id = f"https://{LEMMY_EXAMPLE_DOMAIN}/comment/55"
    # Pre-existing CommunityThreadGroup lets the inbound handler find the target thread.
    thread_group = database.create_thread_group(
        community_actor_id=f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers",
        source_channel_id=None,
        source_thread_id=None,
        source_starter_message_id=None,
        ap_activity_id=f"https://{LEMMY_EXAMPLE_DOMAIN}/activities/create/post/99",
        ap_object_id=post_ap_id,
    )
    database.add_thread_delivery(
        thread_group_id=thread_group.id,
        discord_channel_id=100,
        discord_thread_id=200,
        discord_starter_message_id=300,
        role="inbound",
    )
    # Thread and send are mocked — Discord SDK is the outer boundary here.
    fake_thread = SimpleNamespace(
        id=200,
        send=AsyncMock(return_value=SimpleNamespace(id=900)),
        fetch_message=AsyncMock(),
    )
    fake_bot = SimpleNamespace(
        wait_until_bridge_ready=AsyncMock(),
        get_thread_by_id=AsyncMock(return_value=fake_thread),
    )
    fedify_gateway = AsyncMock()
    community_rt = _community_runtime(database, fedify_gateway, bot=fake_bot)
    runtime_obj = SimpleNamespace(
        database=database,
        bot=fake_bot,
        community_runtime=community_rt,
    )
    event = _comment_event(object_id=comment_ap_id, post_ap_id=post_ap_id)

    result = await community_rt.handle_inbound_comment(event, runtime_obj)

    message_group = database.get_message_group_by_ap_object(comment_ap_id)

    assert result.status == "processed"
    # CommunityMessageGroup must exist so future reply lookups resolve the correct parent.
    assert message_group is not None
    assert message_group.ap_object_id == comment_ap_id
