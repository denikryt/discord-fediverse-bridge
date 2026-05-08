"""Runtime scenarios for Stage 6 Discord-originated ActivityPub publishing."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.db import Database
from src.discord_publish_service import DiscordPublishService, UNREGISTERED_REPLY
from src.fedify_gateway_client import PublishContentResult
from tests_constants import (
    BRIDGE_EXAMPLE_DOMAIN,
    BRIDGE_HOST_DOMAIN,
    LEMMY_EXAMPLE_DOMAIN,
)


def _database(tmp_path: Path) -> Database:
    """Create a real SQLite-backed repository for publish flow tests."""
    database = Database(f"sqlite:///{tmp_path / 'bridge-stage6.db'}")
    database.create_all()
    return database


def _accepted_subscription(database: Database) -> None:
    """Insert one active accepted subscription used by outbound publish tests."""
    community_actor_url = f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers"
    database.create_subscription(
        discord_channel_id=100,
        lemmy_community_actor_id=community_actor_url,
        lemmy_community_name="hackers",
        lemmy_community_id=42,
        community_handle=f"!hackers@{LEMMY_EXAMPLE_DOMAIN}",
        community_inbox_url=f"{community_actor_url}/inbox",
        follow_activity_id=f"https://{BRIDGE_EXAMPLE_DOMAIN}/activities/follow/1",
        status="accepted",
    )


def _registered_user(database: Database) -> None:
    """Insert one registered local user actor used by outbound publish tests."""
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


def _service(database: Database, fedify_gateway: AsyncMock) -> DiscordPublishService:
    """Build the Stage 6 publish service with one fake gateway boundary."""
    return DiscordPublishService(
        database=database,
        fedify_gateway=fedify_gateway,
        bridge_prefix="[bridge]",
    )


def _thread() -> SimpleNamespace:
    """Return one fake forum thread object with the fields Stage 6 uses."""
    return SimpleNamespace(id=200, parent_id=100, name="Thread title")


def _starter_message() -> SimpleNamespace:
    """Return one fake Discord starter message for thread publish scenarios."""
    return SimpleNamespace(
        id=300,
        content="hello from discord",
        author=SimpleNamespace(id=123, display_name="Alice", name="alice"),
        reply=AsyncMock(),
    )


def _thread_message(
    *,
    thread: SimpleNamespace,
    message_id: int = 301,
    reference_message_id: int | None = None,
    author_id: int = 123,
) -> SimpleNamespace:
    """Return one fake Discord thread message for comment publish scenarios."""
    reference = (
        SimpleNamespace(message_id=reference_message_id)
        if reference_message_id is not None
        else None
    )
    return SimpleNamespace(
        id=message_id,
        content="hello comment",
        author=SimpleNamespace(id=author_id, display_name="Alice", name="alice"),
        channel=thread,
        reference=reference,
        reply=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_thread_starter_from_registered_user_publishes_and_persists_mappings(
    tmp_path: Path,
) -> None:
    """A registered user should publish a thread starter through the gateway."""
    database = _database(tmp_path)
    _accepted_subscription(database)
    _registered_user(database)
    post_object_url = f"https://{BRIDGE_HOST_DOMAIN}/users/alice/objects/post/1"
    post_activity_url = (
        f"https://{BRIDGE_HOST_DOMAIN}/users/alice/activities/create/post/1"
    )
    fedify_gateway = AsyncMock()
    fedify_gateway.publish_content.return_value = PublishContentResult(
        activity_id=post_activity_url,
        object_id=post_object_url,
        community_actor_url=f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers",
    )
    service = _service(database, fedify_gateway)
    thread = _thread()
    starter_message = _starter_message()

    result = await service.publish_thread_starter(
        thread=thread,
        starter_message=starter_message,
    )

    post_link = database.get_post_link_by_thread_id(thread.id)
    mapping = database.get_message_mapping_by_discord_message_id(starter_message.id)

    assert result.status == "published"
    assert result.reason == "published"
    fedify_gateway.publish_content.assert_awaited_once()
    assert post_link is not None
    assert post_link.lemmy_post_ap_id == post_object_url
    assert mapping is not None
    assert mapping.activity_id == post_activity_url


@pytest.mark.asyncio
async def test_thread_starter_from_unregistered_user_is_ignored_and_replied_to(
    tmp_path: Path,
) -> None:
    """An unregistered author should be told to use `/register` and not published."""
    database = _database(tmp_path)
    _accepted_subscription(database)
    fedify_gateway = AsyncMock()
    service = _service(database, fedify_gateway)
    thread = _thread()
    starter_message = _starter_message()
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
    database = _database(tmp_path)
    _accepted_subscription(database)
    _registered_user(database)
    thread = _thread()
    post_object_url = f"https://{BRIDGE_HOST_DOMAIN}/users/alice/objects/post/1"
    comment_object_url = f"https://{BRIDGE_HOST_DOMAIN}/users/alice/objects/comment/1"
    comment_activity_url = (
        f"https://{BRIDGE_HOST_DOMAIN}/users/alice/activities/create/comment/1"
    )
    database.create_post_link(
        lemmy_post_id=0,
        lemmy_post_ap_id=post_object_url,
        discord_forum_thread_id=thread.id,
        discord_starter_message_id=300,
        direction="discord_to_activitypub",
    )
    fedify_gateway = AsyncMock()
    fedify_gateway.publish_content.return_value = PublishContentResult(
        activity_id=comment_activity_url,
        object_id=comment_object_url,
        community_actor_url=f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers",
    )
    service = _service(database, fedify_gateway)
    message = _thread_message(thread=thread)

    result = await service.publish_thread_message(message=message)

    comment_link = database.get_comment_link_by_discord_message_id(message.id)
    mapping = database.get_message_mapping_by_discord_message_id(message.id)

    assert result.status == "published"
    assert result.reason == "published"
    fedify_gateway.publish_content.assert_awaited_once()
    assert comment_link is not None
    assert comment_link.lemmy_comment_ap_id == comment_object_url
    assert mapping is not None
    assert mapping.object_id == comment_object_url


@pytest.mark.asyncio
async def test_thread_reply_uses_parent_comment_object_id_when_available(
    tmp_path: Path,
) -> None:
    """A Discord reply should target the mapped parent AP comment when known."""
    database = _database(tmp_path)
    _accepted_subscription(database)
    _registered_user(database)
    thread = _thread()
    post_object_url = f"https://{BRIDGE_HOST_DOMAIN}/users/alice/objects/post/1"
    parent_comment_object_url = (
        f"https://{BRIDGE_HOST_DOMAIN}/users/alice/objects/comment/parent"
    )
    database.create_post_link(
        lemmy_post_id=0,
        lemmy_post_ap_id=post_object_url,
        discord_forum_thread_id=thread.id,
        discord_starter_message_id=300,
        direction="discord_to_activitypub",
    )
    database.create_comment_link(
        lemmy_comment_id=0,
        lemmy_comment_ap_id=parent_comment_object_url,
        lemmy_parent_comment_ap_id=None,
        lemmy_post_id=0,
        discord_forum_thread_id=thread.id,
        discord_message_id=401,
        direction="discord_to_activitypub",
    )
    fedify_gateway = AsyncMock()
    fedify_gateway.publish_content.return_value = PublishContentResult(
        activity_id=f"https://{BRIDGE_HOST_DOMAIN}/users/alice/activities/create/comment/2",
        object_id=f"https://{BRIDGE_HOST_DOMAIN}/users/alice/objects/comment/2",
        community_actor_url=f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers",
    )
    service = _service(database, fedify_gateway)
    message = _thread_message(
        thread=thread,
        message_id=402,
        reference_message_id=401,
    )

    await service.publish_thread_message(message=message)

    request = fedify_gateway.publish_content.await_args.args[0]
    assert request.in_reply_to_object_id == parent_comment_object_url


@pytest.mark.asyncio
async def test_thread_message_in_pending_subscription_is_ignored(
    tmp_path: Path,
) -> None:
    """Pending subscriptions must not publish Discord messages yet."""
    database = _database(tmp_path)
    database.create_subscription(
        discord_channel_id=100,
        lemmy_community_actor_id=f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers",
        lemmy_community_name="hackers",
        lemmy_community_id=42,
        status="pending",
    )
    _registered_user(database)
    thread = _thread()
    database.create_post_link(
        lemmy_post_id=0,
        lemmy_post_ap_id=f"https://{BRIDGE_HOST_DOMAIN}/users/alice/objects/post/1",
        discord_forum_thread_id=thread.id,
        discord_starter_message_id=300,
        direction="discord_to_activitypub",
    )
    fedify_gateway = AsyncMock()
    service = _service(database, fedify_gateway)
    message = _thread_message(thread=thread)

    result = await service.publish_thread_message(message=message)

    assert result.status == "ignored"
    assert result.reason == "subscription_not_active"
    fedify_gateway.publish_content.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_discord_message_is_ignored_before_gateway_call(
    tmp_path: Path,
) -> None:
    """Duplicate Discord message IDs should stop before another publish attempt."""
    database = _database(tmp_path)
    _accepted_subscription(database)
    _registered_user(database)
    thread = _thread()
    database.create_post_link(
        lemmy_post_id=0,
        lemmy_post_ap_id=f"https://{BRIDGE_HOST_DOMAIN}/users/alice/objects/post/1",
        discord_forum_thread_id=thread.id,
        discord_starter_message_id=300,
        direction="discord_to_activitypub",
    )
    database.create_message_mapping(
        source_platform="discord",
        source_id="301",
        activity_id=f"https://{BRIDGE_HOST_DOMAIN}/users/alice/activities/create/comment/existing",
        object_id=f"https://{BRIDGE_HOST_DOMAIN}/users/alice/objects/comment/existing",
        actor_url=f"https://{BRIDGE_HOST_DOMAIN}/users/alice",
        community_actor_url=f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers",
        discord_channel_id=100,
        discord_message_id=301,
    )
    fedify_gateway = AsyncMock()
    service = _service(database, fedify_gateway)
    message = _thread_message(thread=thread, message_id=301)

    result = await service.publish_thread_message(message=message)

    assert result.status == "ignored"
    assert result.reason == "duplicate_discord_message"
    fedify_gateway.publish_content.assert_not_awaited()


@pytest.mark.asyncio
async def test_gateway_publish_failure_does_not_store_false_success_mapping(
    tmp_path: Path,
) -> None:
    """Publish persistence must only happen after the gateway returns success."""
    database = _database(tmp_path)
    _accepted_subscription(database)
    _registered_user(database)
    fedify_gateway = AsyncMock()
    fedify_gateway.publish_content.side_effect = RuntimeError("boom")
    service = _service(database, fedify_gateway)
    thread = _thread()
    starter_message = _starter_message()

    with pytest.raises(RuntimeError):
        await service.publish_thread_starter(
            thread=thread,
            starter_message=starter_message,
        )

    assert database.get_post_link_by_thread_id(thread.id) is None
    assert (
        database.get_message_mapping_by_discord_message_id(starter_message.id)
        is None
    )
