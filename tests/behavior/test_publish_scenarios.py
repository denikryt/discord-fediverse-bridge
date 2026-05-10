"""Behavior scenarios for Discord-originated outbound federation decisions."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.db import Database
from src.discord_publish_service import DiscordPublishService, UNREGISTERED_REPLY
from src.fedify_gateway_client import PublishContentResult
from tests_constants import BRIDGE_HOST_DOMAIN, LEMMY_EXAMPLE_DOMAIN


def _database(tmp_path: Path) -> Database:
    """Create one real SQLite repository for outbound publish scenarios."""
    database = Database(f"sqlite:///{tmp_path / 'behavior-publish.db'}")
    database.create_all()
    return database


def _accepted_subscription(database: Database) -> None:
    """Seed one active channel subscription used by publish behavior tests."""
    community_actor_url = f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers"
    database.create_subscription(
        discord_channel_id=100,
        lemmy_community_actor_id=community_actor_url,
        lemmy_community_name="hackers",
        lemmy_community_id=42,
        community_handle=f"!hackers@{LEMMY_EXAMPLE_DOMAIN}",
        community_inbox_url=f"{community_actor_url}/inbox",
        follow_activity_id=f"https://{BRIDGE_HOST_DOMAIN}/activities/follow/1",
        status="accepted",
    )


def _registered_user(database: Database) -> None:
    """Seed one local AP user actor for registered publish scenarios."""
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
    """Build the real publish service with only the gateway boundary mocked."""
    return DiscordPublishService(
        database=database,
        fedify_gateway=fedify_gateway,
        bridge_prefix="[bridge]",
    )


def _thread() -> SimpleNamespace:
    """Return one fake Discord forum thread used by publish scenarios."""
    return SimpleNamespace(id=200, parent_id=100, name="Thread title")


def _starter_message(*, author_id: int = 123) -> SimpleNamespace:
    """Return one fake starter message with the fields the service reads."""
    return SimpleNamespace(
        id=300,
        content="hello from discord",
        author=SimpleNamespace(id=author_id, display_name="Alice", name="alice"),
        reply=AsyncMock(),
    )


def _thread_message(
    *,
    thread: SimpleNamespace,
    message_id: int = 301,
    author_id: int = 123,
    reference_message_id: int | None = None,
) -> SimpleNamespace:
    """Return one fake thread message for comment publish scenarios."""
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
async def test_registered_user_with_accepted_subscription_publishes_thread_starter(
    tmp_path: Path,
) -> None:
    """A registered author plus active subscription should produce AP post state."""
    database = _database(tmp_path)
    _accepted_subscription(database)
    _registered_user(database)
    fedify_gateway = AsyncMock()
    fedify_gateway.publish_content.return_value = PublishContentResult(
        activity_id=f"https://{BRIDGE_HOST_DOMAIN}/users/alice/activities/create/post/1",
        object_id=f"https://{BRIDGE_HOST_DOMAIN}/users/alice/objects/post/1",
        community_actor_url=f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers",
    )
    service = _service(database, fedify_gateway)

    result = await service.publish_thread_starter(
        thread=_thread(),
        starter_message=_starter_message(),
    )
    mapping = database.get_message_mapping_by_discord_message_id(300)
    stored_object = database.get_published_activity_object_by_object_id(
        f"https://{BRIDGE_HOST_DOMAIN}/users/alice/objects/post/1"
    )

    assert result.status == "published"
    assert mapping is not None
    assert mapping.object_id == f"https://{BRIDGE_HOST_DOMAIN}/users/alice/objects/post/1"
    assert stored_object is not None
    assert stored_object.kind == "post"


@pytest.mark.asyncio
async def test_registered_user_with_accepted_subscription_publishes_thread_reply(
    tmp_path: Path,
) -> None:
    """A mapped thread reply should publish as an AP comment and persist links."""
    database = _database(tmp_path)
    _accepted_subscription(database)
    _registered_user(database)
    post_object_url = f"https://{BRIDGE_HOST_DOMAIN}/users/alice/objects/post/1"
    database.create_thread_group(
        community_actor_id=f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers",
        source_channel_id=100,
        source_thread_id=200,
        source_starter_message_id=300,
        ap_activity_id=f"https://{BRIDGE_HOST_DOMAIN}/users/alice/activities/create/post/1",
        ap_object_id=post_object_url,
    )
    fedify_gateway = AsyncMock()
    fedify_gateway.publish_content.return_value = PublishContentResult(
        activity_id=f"https://{BRIDGE_HOST_DOMAIN}/users/alice/activities/create/comment/1",
        object_id=f"https://{BRIDGE_HOST_DOMAIN}/users/alice/objects/comment/1",
        community_actor_url=f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers",
    )
    service = _service(database, fedify_gateway)

    result = await service.publish_thread_message(
        message=_thread_message(thread=_thread()),
    )
    mapping = database.get_message_mapping_by_discord_message_id(301)
    stored_object = database.get_published_activity_object_by_object_id(
        f"https://{BRIDGE_HOST_DOMAIN}/users/alice/objects/comment/1"
    )

    assert result.status == "published"
    assert mapping is not None
    assert mapping.object_id == f"https://{BRIDGE_HOST_DOMAIN}/users/alice/objects/comment/1"
    assert stored_object is not None
    assert stored_object.kind == "comment"


@pytest.mark.asyncio
async def test_unregistered_user_message_is_not_federated_and_gets_register_reply(
    tmp_path: Path,
) -> None:
    """An unregistered author should be guided to registration instead of published."""
    database = _database(tmp_path)
    _accepted_subscription(database)
    fedify_gateway = AsyncMock()
    service = _service(database, fedify_gateway)
    starter_message = _starter_message(author_id=999)

    result = await service.publish_thread_starter(
        thread=_thread(),
        starter_message=starter_message,
    )

    assert result.status == "ignored"
    assert result.reason == "unregistered_user"
    fedify_gateway.publish_content.assert_not_awaited()
    starter_message.reply.assert_awaited_once_with(UNREGISTERED_REPLY)
    assert database.get_message_mapping_by_discord_message_id(300) is None


@pytest.mark.asyncio
async def test_registered_user_without_accepted_subscription_does_not_publish(
    tmp_path: Path,
) -> None:
    """Without an accepted subscription the service should keep the message local."""
    database = _database(tmp_path)
    _registered_user(database)
    fedify_gateway = AsyncMock()
    service = _service(database, fedify_gateway)

    result = await service.publish_thread_starter(
        thread=_thread(),
        starter_message=_starter_message(),
    )

    assert result.status == "ignored"
    assert result.reason == "no_subscription"
    fedify_gateway.publish_content.assert_not_awaited()
    assert database.get_message_mapping_by_discord_message_id(300) is None


@pytest.mark.asyncio
async def test_gateway_publish_failure_does_not_persist_false_success(
    tmp_path: Path,
) -> None:
    """A failing gateway publish must not leave links or mappings behind."""
    database = _database(tmp_path)
    _accepted_subscription(database)
    _registered_user(database)
    fedify_gateway = AsyncMock()
    fedify_gateway.publish_content.side_effect = RuntimeError("boom")
    service = _service(database, fedify_gateway)

    with pytest.raises(RuntimeError):
        await service.publish_thread_starter(
            thread=_thread(),
            starter_message=_starter_message(),
        )

    assert database.get_message_mapping_by_discord_message_id(300) is None
