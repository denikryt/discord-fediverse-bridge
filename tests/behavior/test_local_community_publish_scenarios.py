"""Behavior scenarios for Discord-originated local-community publishes."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from src.content_publish_service import ContentPublishService
from src.fedify_gateway_client import PublishLocalCommunityContentResult
from src.local_communities.runtime import LocalCommunityRuntime
from src.local_communities.service import LocalCommunityService
from src.models import MessageMapping, PublishedActivityObject
from support.db import add_registered_user, build_database
from support.discord import build_starter_message, build_thread, build_thread_message


def _runtime(tmp_path: Path) -> tuple[object, LocalCommunityRuntime]:
    """Build a real local-community runtime with only the gateway mocked."""
    database = build_database(tmp_path, "local-community-publish.db")
    gateway = AsyncMock()
    publish_service = ContentPublishService(
        database=database,
        fedify_gateway=gateway,
        bridge_prefix="[bridge]",
    )
    runtime = LocalCommunityRuntime(
        database=database,
        fedify_gateway=gateway,
        content_publish_service=publish_service,
        bridge_prefix="[bridge]",
    )
    return database, runtime


def _local_community(database: object, *, channel_id: int = 100) -> None:
    """Seed one local community bound to the shared Discord forum channel."""
    LocalCommunityService(
        database=database,
        base_url="https://bridge.example",
        keypair_generator=lambda: ("public-key", "private-key"),
    ).create_local_community(
        discord_guild_id=10,
        discord_forum_channel_id=channel_id,
        slug="hackers",
        name="Hackers",
        description="A local hackerspace forum.",
        created_by_discord_user_id="123",
    )


@pytest.mark.asyncio
async def test_registered_user_thread_starter_in_local_community_publishes_post(
    tmp_path: Path,
) -> None:
    """A local-community thread starter should publish one AP post and map the thread."""
    database, runtime = _runtime(tmp_path)
    _local_community(database)
    add_registered_user(database)
    runtime.fedify_gateway.publish_content.side_effect = AssertionError(
        "local-community thread starters must use the dedicated local-community publish path"
    )
    runtime.fedify_gateway.publish_local_community_content.return_value = (
        PublishLocalCommunityContentResult(
        activity_id="https://bridge.example/users/alice/activities/create/post/1",
        object_id="https://bridge.example/users/alice/post/1",
        community_actor_url="https://bridge.example/communities/hackers",
        delivered_follower_count=1,
        failed_follower_count=0,
        )
    )

    result = await runtime.handle_discord_thread_create(
        thread=build_thread(),
        starter_message=build_starter_message(),
    )
    thread_surface = database.local_community_surfaces.get_local_community_thread_surface_by_discord_thread_id(200)
    assert thread_surface is not None
    created = database.local_community_surfaces.get_local_community_thread_for_surface(thread_surface.id)

    assert result.status == "published"
    assert created is not None
    assert created.ap_object_id == "https://bridge.example/users/alice/post/1"
    runtime.fedify_gateway.publish_local_community_content.assert_awaited_once()
    with database.session() as session:
        assert session.scalar(select(func.count()).select_from(MessageMapping)) == 1
        assert session.scalar(select(func.count()).select_from(PublishedActivityObject)) == 1


@pytest.mark.asyncio
async def test_unregistered_user_thread_starter_in_local_community_is_rejected(
    tmp_path: Path,
) -> None:
    """An unregistered author should not publish into a local community."""
    database, runtime = _runtime(tmp_path)
    _local_community(database)
    starter_message = build_starter_message(author_id=999)

    result = await runtime.handle_discord_thread_create(
        thread=build_thread(),
        starter_message=starter_message,
    )

    assert result.reason == "unregistered_user"
    assert database.local_community_surfaces.get_local_community_thread_surface_by_discord_thread_id(200) is None
    starter_message.reply.assert_awaited_once()


@pytest.mark.asyncio
async def test_discord_reply_in_local_community_thread_publishes_comment_with_parent_mapping(
    tmp_path: Path,
) -> None:
    """A local-community reply should publish one AP comment linked to the post root."""
    database, runtime = _runtime(tmp_path)
    _local_community(database)
    add_registered_user(database)
    thread = build_thread()
    starter = build_starter_message()
    runtime.fedify_gateway.publish_content.side_effect = AssertionError(
        "local-community replies must use the dedicated local-community publish path"
    )
    runtime.fedify_gateway.publish_local_community_content.side_effect = [
        PublishLocalCommunityContentResult(
            activity_id="https://bridge.example/users/alice/activities/create/post/1",
            object_id="https://bridge.example/users/alice/post/1",
            community_actor_url="https://bridge.example/communities/hackers",
            delivered_follower_count=1,
            failed_follower_count=0,
        ),
        PublishLocalCommunityContentResult(
            activity_id="https://bridge.example/users/alice/activities/create/comment/1",
            object_id="https://bridge.example/users/alice/comment/1",
            community_actor_url="https://bridge.example/communities/hackers",
            delivered_follower_count=1,
            failed_follower_count=0,
        ),
    ]
    await runtime.handle_discord_thread_create(thread=thread, starter_message=starter)

    result = await runtime.handle_discord_message(
        message=build_thread_message(),
    )
    message_surface = database.local_community_surfaces.get_local_community_message_surface_by_discord_message_id(301)
    assert message_surface is not None
    created = database.local_community_surfaces.get_local_community_message_for_surface(message_surface.id)

    assert result.status == "published"
    assert created is not None
    assert created.parent_ap_object_id == "https://bridge.example/users/alice/post/1"
    assert runtime.fedify_gateway.publish_local_community_content.await_count == 2


@pytest.mark.asyncio
async def test_local_community_reply_semantics_preserve_nested_and_unknown_fallback(
    tmp_path: Path,
) -> None:
    """Local-community replies should keep nested and unknown-parent semantics."""
    database, runtime = _runtime(tmp_path)
    _local_community(database)
    add_registered_user(database)
    thread = build_thread()
    starter = build_starter_message()
    runtime.fedify_gateway.publish_local_community_content.side_effect = [
        PublishLocalCommunityContentResult(
            activity_id="https://bridge.example/users/alice/activities/create/post/1",
            object_id="https://bridge.example/users/alice/post/1",
            community_actor_url="https://bridge.example/communities/hackers",
            delivered_follower_count=1,
            failed_follower_count=0,
        ),
        PublishLocalCommunityContentResult(
            activity_id="https://bridge.example/users/alice/activities/create/comment/0",
            object_id="https://bridge.example/users/alice/comment/0",
            community_actor_url="https://bridge.example/communities/hackers",
            delivered_follower_count=1,
            failed_follower_count=0,
        ),
        PublishLocalCommunityContentResult(
            activity_id="https://bridge.example/users/alice/activities/create/comment/1",
            object_id="https://bridge.example/users/alice/comment/1",
            community_actor_url="https://bridge.example/communities/hackers",
            delivered_follower_count=1,
            failed_follower_count=0,
        ),
        PublishLocalCommunityContentResult(
            activity_id="https://bridge.example/users/alice/activities/create/comment/2",
            object_id="https://bridge.example/users/alice/comment/2",
            community_actor_url="https://bridge.example/communities/hackers",
            delivered_follower_count=1,
            failed_follower_count=0,
        ),
    ]
    await runtime.handle_discord_thread_create(thread=thread, starter_message=starter)
    await runtime.handle_discord_message(message=build_thread_message(message_id=301))

    nested_result = await runtime.handle_discord_message(
        message=build_thread_message(message_id=302, reference_message_id=301),
    )
    unknown_result = await runtime.handle_discord_message(
        message=build_thread_message(message_id=303, reference_message_id=999),
    )

    assert nested_result.status == "published"
    assert unknown_result.status == "published"
    nested_request = runtime.fedify_gateway.publish_local_community_content.await_args_list[2].args[0]
    unknown_request = runtime.fedify_gateway.publish_local_community_content.await_args_list[3].args[0]
    assert nested_request.in_reply_to_object_id == "https://bridge.example/users/alice/comment/0"
    assert unknown_request.in_reply_to_object_id == "https://bridge.example/users/alice/post/1"
