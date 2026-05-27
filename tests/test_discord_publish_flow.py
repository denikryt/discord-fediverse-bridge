"""Runtime scenarios for Stage 6 Discord-originated ActivityPub publishing."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from src.content_publish_service import ContentPublishService, UNREGISTERED_REPLY
from support.db import add_accepted_subscription, add_registered_user, build_database
from support.discord import build_starter_message, build_thread, build_thread_message
from support.gateway import build_publish_result
from tests_constants import BRIDGE_HOST_DOMAIN, LEMMY_EXAMPLE_DOMAIN


def _service(database, fedify_gateway: AsyncMock) -> ContentPublishService:
    """Build the Stage 6 publish service with one fake gateway boundary."""
    return ContentPublishService(
        database=database,
        fedify_gateway=fedify_gateway,
        bridge_prefix="[bridge]",
    )


@pytest.mark.asyncio
async def test_thread_starter_from_registered_user_publishes_and_persists_mappings(
    tmp_path: Path,
) -> None:
    """A registered user should publish a thread starter through the gateway."""
    database = build_database(tmp_path, "bridge-stage6.db")
    add_accepted_subscription(database)
    add_registered_user(database)
    post_object_url = f"https://{BRIDGE_HOST_DOMAIN}/users/alice/objects/post/1"
    post_activity_url = (
        f"https://{BRIDGE_HOST_DOMAIN}/users/alice/activities/create/post/1"
    )
    fedify_gateway = AsyncMock()
    fedify_gateway.publish_content.return_value = build_publish_result(
        kind="post", activity_id=post_activity_url, object_id=post_object_url
    )
    service = _service(database, fedify_gateway)
    thread = build_thread()
    starter_message = build_starter_message()

    result = await service.publish_thread_starter(
        thread=thread,
        starter_message=starter_message,
    )

    mapping = database.message_mappings.get_message_mapping_by_discord_message_id(starter_message.id)
    stored_object = database.activitypub_objects.get_published_activity_object_by_object_id(
        post_object_url
    )

    assert result.status == "published"
    assert result.reason == "published"
    fedify_gateway.publish_content.assert_awaited_once()
    assert mapping is not None
    assert mapping.activity_id == post_activity_url
    assert stored_object is not None
    assert stored_object.kind == "post"
    assert stored_object.in_reply_to_object_id is None


@pytest.mark.asyncio
async def test_thread_starter_from_unregistered_user_is_ignored_and_replied_to(
    tmp_path: Path,
) -> None:
    """An unregistered author should be told to use `/register` and not published."""
    database = build_database(tmp_path, "bridge-stage6.db")
    add_accepted_subscription(database)
    fedify_gateway = AsyncMock()
    service = _service(database, fedify_gateway)
    thread = build_thread()
    starter_message = build_starter_message()
    starter_message.author.id = 999

    result = await service.publish_thread_starter(
        thread=thread,
        starter_message=starter_message,
    )

    assert result.status == "ignored"
    assert result.reason == "unregistered_user"
    fedify_gateway.publish_content.assert_not_awaited()
    starter_message.reply.assert_awaited_once_with(UNREGISTERED_REPLY)


@pytest.mark.asyncio
async def test_thread_message_from_registered_user_publishes_as_comment(
    tmp_path: Path,
) -> None:
    """A registered user message inside a mapped thread should publish as a comment."""
    database = build_database(tmp_path, "bridge-stage6.db")
    add_accepted_subscription(database)
    add_registered_user(database)
    thread = build_thread()
    post_object_url = f"https://{BRIDGE_HOST_DOMAIN}/users/alice/objects/post/1"
    comment_object_url = f"https://{BRIDGE_HOST_DOMAIN}/users/alice/objects/comment/1"
    comment_activity_url = (
        f"https://{BRIDGE_HOST_DOMAIN}/users/alice/activities/create/comment/1"
    )
    thread_group = database.discord_fanout_groups.create_thread_group(
        community_actor_id=f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers",
        source_channel_id=100,
        source_thread_id=thread.id,
        source_starter_message_id=300,
        ap_activity_id=f"https://{BRIDGE_HOST_DOMAIN}/users/alice/activities/create/post/1",
        ap_object_id=post_object_url,
    )
    database.discord_fanout_groups.add_thread_delivery(
        thread_group_id=thread_group.id,
        discord_channel_id=100,
        discord_thread_id=thread.id,
        discord_starter_message_id=300,
        role="source",
    )
    fedify_gateway = AsyncMock()
    fedify_gateway.publish_content.return_value = build_publish_result(
        kind="comment", activity_id=comment_activity_url, object_id=comment_object_url
    )
    service = _service(database, fedify_gateway)
    message = build_thread_message(thread_id=thread.id, channel_id=thread.parent_id)

    result = await service.publish_thread_message(message=message)

    mapping = database.message_mappings.get_message_mapping_by_discord_message_id(message.id)
    stored_object = database.activitypub_objects.get_published_activity_object_by_object_id(
        comment_object_url
    )

    assert result.status == "published"
    assert result.reason == "published"
    fedify_gateway.publish_content.assert_awaited_once()
    assert mapping is not None
    assert mapping.object_id == comment_object_url
    assert stored_object is not None
    assert stored_object.kind == "comment"
    assert stored_object.in_reply_to_object_id == post_object_url


@pytest.mark.asyncio
async def test_thread_reply_uses_parent_comment_object_id_when_available(
    tmp_path: Path,
) -> None:
    """A Discord reply should target the mapped parent AP comment when known."""
    database = build_database(tmp_path, "bridge-stage6.db")
    add_accepted_subscription(database)
    add_registered_user(database)
    thread = build_thread()
    post_object_url = f"https://{BRIDGE_HOST_DOMAIN}/users/alice/objects/post/1"
    parent_comment_object_url = (
        f"https://{BRIDGE_HOST_DOMAIN}/users/alice/objects/comment/parent"
    )
    thread_group = database.discord_fanout_groups.create_thread_group(
        community_actor_id=f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers",
        source_channel_id=100,
        source_thread_id=thread.id,
        source_starter_message_id=300,
        ap_activity_id=f"https://{BRIDGE_HOST_DOMAIN}/users/alice/activities/create/post/1",
        ap_object_id=post_object_url,
    )
    database.discord_fanout_groups.add_thread_delivery(
        thread_group_id=thread_group.id,
        discord_channel_id=100,
        discord_thread_id=thread.id,
        discord_starter_message_id=300,
        role="source",
    )
    parent_message_group = database.discord_fanout_groups.create_message_group(
        community_actor_id=f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers",
        thread_group_id=thread_group.id,
        source_channel_id=100,
        source_thread_id=thread.id,
        source_message_id=401,
        ap_activity_id=f"https://{BRIDGE_HOST_DOMAIN}/users/alice/activities/create/comment/parent",
        ap_object_id=parent_comment_object_url,
    )
    database.discord_fanout_groups.add_message_delivery(
        message_group_id=parent_message_group.id,
        discord_channel_id=100,
        discord_thread_id=thread.id,
        discord_message_id=401,
        role="source",
    )
    fedify_gateway = AsyncMock()
    fedify_gateway.publish_content.return_value = build_publish_result(
        kind="comment",
        activity_id=f"https://{BRIDGE_HOST_DOMAIN}/users/alice/activities/create/comment/2",
        object_id=f"https://{BRIDGE_HOST_DOMAIN}/users/alice/objects/comment/2",
    )
    service = _service(database, fedify_gateway)
    message = build_thread_message(
        message_id=402,
        thread_id=thread.id,
        channel_id=thread.parent_id,
        reference_message_id=401,
    )

    await service.publish_thread_message(message=message)

    request = fedify_gateway.publish_content.await_args.args[0]
    stored_object = database.activitypub_objects.get_published_activity_object_by_object_id(
        f"https://{BRIDGE_HOST_DOMAIN}/users/alice/objects/comment/2"
    )
    assert request.in_reply_to_object_id == parent_comment_object_url
    assert stored_object is not None
    assert stored_object.in_reply_to_object_id == parent_comment_object_url


@pytest.mark.asyncio
async def test_thread_message_in_pending_subscription_is_ignored(
    tmp_path: Path,
) -> None:
    """Pending subscriptions must not publish Discord messages yet."""
    database = build_database(tmp_path, "bridge-stage6.db")
    database.remote_subscriptions.create_subscription(
        discord_channel_id=100,
        lemmy_community_actor_id=f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers",
        lemmy_community_name="hackers",
        lemmy_community_id=42,
        status="pending",
    )
    add_registered_user(database)
    thread = build_thread()
    fedify_gateway = AsyncMock()
    service = _service(database, fedify_gateway)
    message = build_thread_message(thread_id=thread.id, channel_id=thread.parent_id)

    result = await service.publish_thread_message(message=message)

    assert result.status == "ignored"
    assert result.reason == "subscription_not_active"
    fedify_gateway.publish_content.assert_not_awaited()


@pytest.mark.asyncio
async def test_message_without_thread_group_returns_no_post_context(
    tmp_path: Path,
) -> None:
    """A message in a thread with no CommunityThreadGroup returns no_post_context."""
    database = build_database(tmp_path, "bridge-stage6.db")
    add_accepted_subscription(database)
    add_registered_user(database)
    thread = build_thread()
    fedify_gateway = AsyncMock()
    service = _service(database, fedify_gateway)
    message = build_thread_message(
        thread_id=thread.id, channel_id=thread.parent_id, message_id=301
    )

    result = await service.publish_thread_message(message=message)

    assert result.status == "ignored"
    assert result.reason == "no_post_context"
    fedify_gateway.publish_content.assert_not_awaited()


@pytest.mark.asyncio
async def test_gateway_publish_failure_does_not_store_false_success_mapping(
    tmp_path: Path,
) -> None:
    """Publish persistence must only happen after the gateway returns success."""
    database = build_database(tmp_path, "bridge-stage6.db")
    add_accepted_subscription(database)
    add_registered_user(database)
    fedify_gateway = AsyncMock()
    fedify_gateway.publish_content.side_effect = RuntimeError("boom")
    service = _service(database, fedify_gateway)
    thread = build_thread()
    starter_message = build_starter_message()

    with pytest.raises(RuntimeError):
        await service.publish_thread_starter(
            thread=thread,
            starter_message=starter_message,
        )

    assert (
        database.message_mappings.get_message_mapping_by_discord_message_id(starter_message.id)
        is None
    )
