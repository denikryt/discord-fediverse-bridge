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

from src.fedify_gateway_client import PublishContentResult
from support.activitypub import build_comment_created_event, build_post_created_event
from support.db import add_accepted_subscription, add_registered_user, build_database
from support.discord import build_starter_message, build_thread, build_thread_message
from support.runtime import build_community_runtime
from tests_constants import BRIDGE_HOST_DOMAIN, LEMMY_EXAMPLE_DOMAIN


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
    database = build_database(tmp_path, "community-runtime.db")
    add_accepted_subscription(database)
    add_registered_user(database)
    post_object_url = f"https://{BRIDGE_HOST_DOMAIN}/users/alice/objects/post/1"
    post_activity_url = f"https://{BRIDGE_HOST_DOMAIN}/users/alice/activities/create/post/1"
    fedify_gateway = AsyncMock()
    fedify_gateway.publish_content.return_value = PublishContentResult(
        activity_id=post_activity_url,
        object_id=post_object_url,
        community_actor_url=f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers",
    )
    runtime = build_community_runtime(database, fedify_gateway=fedify_gateway)
    thread = build_thread()
    starter_message = build_starter_message()

    result = await runtime.handle_discord_thread_create(thread=thread, starter_message=starter_message)

    mapping = database.message_mappings.get_message_mapping_by_discord_message_id(starter_message.id)
    stored_object = database.activitypub_objects.get_published_activity_object_by_object_id(post_object_url)
    thread_group = database.discord_fanout_groups.get_thread_group_by_source_thread(thread.id)

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
    database = build_database(tmp_path, "community-runtime.db")
    add_accepted_subscription(database)
    add_registered_user(database)
    thread = build_thread()
    post_object_url = f"https://{BRIDGE_HOST_DOMAIN}/users/alice/objects/post/1"
    comment_object_url = f"https://{BRIDGE_HOST_DOMAIN}/users/alice/objects/comment/1"
    comment_activity_url = f"https://{BRIDGE_HOST_DOMAIN}/users/alice/activities/create/comment/1"
    # CommunityThreadGroup lets publish_thread_message resolve the post context.
    thread_group = database.discord_fanout_groups.create_thread_group(
        community_actor_id=f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers",
        source_channel_id=thread.parent_id,
        source_thread_id=thread.id,
        source_starter_message_id=300,
        ap_activity_id=f"https://{BRIDGE_HOST_DOMAIN}/users/alice/activities/create/post/1",
        ap_object_id=post_object_url,
    )
    database.discord_fanout_groups.add_thread_delivery(
        thread_group_id=thread_group.id,
        discord_channel_id=thread.parent_id,
        discord_thread_id=thread.id,
        discord_starter_message_id=300,
        role="source",
    )
    fedify_gateway = AsyncMock()
    fedify_gateway.publish_content.return_value = PublishContentResult(
        activity_id=comment_activity_url,
        object_id=comment_object_url,
        community_actor_url=f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers",
    )
    runtime = build_community_runtime(database, fedify_gateway=fedify_gateway)
    message = build_thread_message(
        message_id=301, thread_id=thread.id, channel_id=thread.parent_id
    )

    result = await runtime.handle_discord_message(message=message)

    mapping = database.message_mappings.get_message_mapping_by_discord_message_id(message.id)
    stored_object = database.activitypub_objects.get_published_activity_object_by_object_id(comment_object_url)
    message_group = database.discord_fanout_groups.get_message_group_by_source_message(message.id)

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
    database = build_database(tmp_path, "community-runtime.db")
    add_accepted_subscription(database)
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
    community_rt = build_community_runtime(
        database, fedify_gateway=fedify_gateway, bot=fake_bot
    )
    runtime_obj = SimpleNamespace(
        database=database,
        bot=fake_bot,
        community_runtime=community_rt,
    )
    event = build_post_created_event(object_id=post_ap_id)

    result = await community_rt.handle_inbound_post(event, runtime_obj)

    thread_group = database.discord_fanout_groups.get_thread_group_by_ap_object(post_ap_id)

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
    database = build_database(tmp_path, "community-runtime.db")
    add_accepted_subscription(database)
    post_ap_id = f"https://{LEMMY_EXAMPLE_DOMAIN}/post/99"
    comment_ap_id = f"https://{LEMMY_EXAMPLE_DOMAIN}/comment/55"
    # Pre-existing CommunityThreadGroup lets the inbound handler find the target thread.
    thread_group = database.discord_fanout_groups.create_thread_group(
        community_actor_id=f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers",
        source_channel_id=None,
        source_thread_id=None,
        source_starter_message_id=None,
        ap_activity_id=f"https://{LEMMY_EXAMPLE_DOMAIN}/activities/create/post/99",
        ap_object_id=post_ap_id,
    )
    database.discord_fanout_groups.add_thread_delivery(
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
    community_rt = build_community_runtime(
        database, fedify_gateway=fedify_gateway, bot=fake_bot
    )
    runtime_obj = SimpleNamespace(
        database=database,
        bot=fake_bot,
        community_runtime=community_rt,
    )
    event = build_comment_created_event(object_id=comment_ap_id, post_ap_id=post_ap_id)

    result = await community_rt.handle_inbound_comment(event, runtime_obj)

    message_group = database.discord_fanout_groups.get_message_group_by_ap_object(comment_ap_id)

    assert result.status == "processed"
    # CommunityMessageGroup must exist so future reply lookups resolve the correct parent.
    assert message_group is not None
    assert message_group.ap_object_id == comment_ap_id
