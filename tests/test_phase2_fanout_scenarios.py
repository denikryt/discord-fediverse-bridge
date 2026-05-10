"""Phase 2 scenario tests: local Discord thread fanout via CommunityRuntime.

Each test exercises a concrete action in a defined system state and asserts
observable DB effects. All five tests use a real SQLite DB, real CommunityRuntime,
and real DiscordPublishService. Mock only outer boundaries: FedifyGatewayClient,
bot.fetch_forum_channel, and forum_channel.create_thread.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.community_sync.discord_fanout import DiscordFanout
from src.community_sync.runtime import CommunityRuntime
from src.db import Database
from src.discord_publish_service import DiscordPublishService
from src.fedify_gateway_client import PublishContentResult
from tests_constants import BRIDGE_HOST_DOMAIN, LEMMY_EXAMPLE_DOMAIN

COMMUNITY_ACTOR_URL = f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers"


def _database(tmp_path: Path) -> Database:
    """Create one real SQLite repository for Phase 2 fanout scenario tests."""
    database = Database(f"sqlite:///{tmp_path / 'phase2-fanout.db'}")
    database.create_all()
    return database


def _accepted_subscription(database: Database, *, channel_id: int) -> None:
    """Insert one accepted community subscription for the shared hackers community."""
    database.create_subscription(
        discord_channel_id=channel_id,
        lemmy_community_actor_id=COMMUNITY_ACTOR_URL,
        lemmy_community_name="hackers",
        lemmy_community_id=42,
        community_handle=f"!hackers@{LEMMY_EXAMPLE_DOMAIN}",
        community_inbox_url=f"{COMMUNITY_ACTOR_URL}/inbox",
        follow_activity_id=f"https://{BRIDGE_HOST_DOMAIN}/activities/follow/{channel_id}",
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


def _publish_gateway(
    *,
    activity_id: str | None = None,
    object_id: str | None = None,
) -> AsyncMock:
    """Build a mocked FedifyGatewayClient that returns a valid PublishContentResult."""
    gateway = AsyncMock()
    gateway.publish_content.return_value = PublishContentResult(
        activity_id=activity_id or f"https://{BRIDGE_HOST_DOMAIN}/users/alice/activities/create/post/1",
        object_id=object_id or f"https://{BRIDGE_HOST_DOMAIN}/users/alice/objects/post/1",
        community_actor_url=COMMUNITY_ACTOR_URL,
    )
    return gateway


def _publish_service(database: Database, gateway: AsyncMock) -> DiscordPublishService:
    """Build a DiscordPublishService wired to one fake gateway boundary."""
    return DiscordPublishService(
        database=database,
        fedify_gateway=gateway,
        bridge_prefix="[bridge]",
    )


def _community_runtime(
    database: Database,
    gateway: AsyncMock,
    *,
    discord_fanout: DiscordFanout | None = None,
) -> CommunityRuntime:
    """Build a real CommunityRuntime with optional DiscordFanout."""
    return CommunityRuntime(
        database=database,
        discord_publish_service=_publish_service(database, gateway),
        discord_fanout=discord_fanout,
    )


def _fake_thread(*, thread_id: int = 200, channel_id: int = 100) -> SimpleNamespace:
    """Return one fake Discord forum thread."""
    return SimpleNamespace(id=thread_id, parent_id=channel_id, name="Thread title")


def _fake_starter_message(*, message_id: int = 300, author_id: int = 123) -> SimpleNamespace:
    """Return one fake Discord starter message."""
    return SimpleNamespace(
        id=message_id,
        content="hello from discord",
        author=SimpleNamespace(id=author_id, display_name="Alice", name="alice"),
        reply=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_phase2_single_subscription_no_mirror_created(tmp_path: Path) -> None:
    """With one subscription, AP publishes normally and no mirror delivery is created.

    System state: one accepted subscription for channel 100, one registered user.
    Action: call handle_discord_thread_create for thread 200 in channel 100.
    Assert: result status is 'published', CommunityThreadGroup exists, one source
    delivery row exists, no mirror delivery row exists, gateway called once.
    """
    database = _database(tmp_path)
    _accepted_subscription(database, channel_id=100)
    _registered_user(database)
    gateway = _publish_gateway()
    runtime = _community_runtime(database, gateway)
    thread = _fake_thread(thread_id=200, channel_id=100)
    starter = _fake_starter_message(message_id=300)

    result = await runtime.handle_discord_thread_create(thread=thread, starter_message=starter)

    thread_group = database.get_thread_group_by_source_thread(200)
    deliveries = database.get_thread_deliveries(thread_group.id) if thread_group else []
    mirror_deliveries = [d for d in deliveries if d.role == "mirror"]

    assert result.status == "published"
    # CommunityThreadGroup must exist with the source thread id.
    assert thread_group is not None
    assert thread_group.source_thread_id == 200
    # Exactly one source delivery row.
    assert len(deliveries) == 1
    assert deliveries[0].role == "source"
    assert deliveries[0].discord_channel_id == 100
    assert deliveries[0].discord_thread_id == 200
    # No mirror rows — only one subscription exists.
    assert len(mirror_deliveries) == 0
    # Gateway must have been called exactly once (one AP publish per thread create).
    gateway.publish_content.assert_awaited_once()


@pytest.mark.asyncio
async def test_phase2_two_subscriptions_mirror_thread_created_in_sibling(tmp_path: Path) -> None:
    """With two subscriptions, a mirror thread is created in the sibling channel.

    System state: two accepted subscriptions for channels 100 and 101 (same community),
    one registered user. bot.fetch_forum_channel(101) returns a fake forum channel
    whose create_thread returns a fake mirror thread and message.
    Action: call handle_discord_thread_create for thread 200 in channel 100.
    Assert: result status is 'published', two delivery rows (source + mirror),
    mirror row has channel 101 and the fake mirror thread id, gateway called once.
    """
    database = _database(tmp_path)
    _accepted_subscription(database, channel_id=100)
    _accepted_subscription(database, channel_id=101)
    _registered_user(database)
    gateway = _publish_gateway()

    # Fake mirror forum channel with a controllable create_thread return.
    fake_mirror_thread = SimpleNamespace(id=500)
    fake_mirror_message = SimpleNamespace(id=501)
    fake_forum_channel = SimpleNamespace(
        id=101,
        create_thread=AsyncMock(
            return_value=SimpleNamespace(
                thread=fake_mirror_thread,
                message=fake_mirror_message,
            )
        ),
    )
    fake_bot = SimpleNamespace(
        fetch_forum_channel=AsyncMock(return_value=fake_forum_channel),
    )
    fanout = DiscordFanout(bot=fake_bot)
    runtime = _community_runtime(database, gateway, discord_fanout=fanout)
    thread = _fake_thread(thread_id=200, channel_id=100)
    starter = _fake_starter_message(message_id=300)

    result = await runtime.handle_discord_thread_create(thread=thread, starter_message=starter)

    thread_group = database.get_thread_group_by_source_thread(200)
    deliveries = database.get_thread_deliveries(thread_group.id) if thread_group else []
    source_deliveries = [d for d in deliveries if d.role == "source"]
    mirror_deliveries = [d for d in deliveries if d.role == "mirror"]

    assert result.status == "published"
    assert thread_group is not None
    # Source delivery for channel 100 / thread 200.
    assert len(source_deliveries) == 1
    assert source_deliveries[0].discord_channel_id == 100
    assert source_deliveries[0].discord_thread_id == 200
    # Mirror delivery for channel 101 / mirror thread 500.
    assert len(mirror_deliveries) == 1
    assert mirror_deliveries[0].discord_channel_id == 101
    assert mirror_deliveries[0].discord_thread_id == 500
    assert mirror_deliveries[0].discord_starter_message_id == 501
    # forum_channel.create_thread was called once (for channel 101 only).
    fake_forum_channel.create_thread.assert_awaited_once()
    # Gateway called exactly once for AP publish.
    gateway.publish_content.assert_awaited_once()


@pytest.mark.asyncio
async def test_phase2_duplicate_thread_create_is_ignored(tmp_path: Path) -> None:
    """A duplicate thread-create event (e.g. Discord reconnect) is skipped silently.

    System state: one accepted subscription for channel 100, one registered user,
    CommunityThreadGroup already exists for source_thread_id=200.
    Action: call handle_discord_thread_create for thread 200.
    Assert: result status is 'ignored', gateway not called, no new delivery rows.
    """
    database = _database(tmp_path)
    _accepted_subscription(database, channel_id=100)
    _registered_user(database)
    # Pre-insert the thread group to simulate a prior successful publish.
    database.create_thread_group(
        community_actor_id=COMMUNITY_ACTOR_URL,
        source_channel_id=100,
        source_thread_id=200,
        source_starter_message_id=300,
        ap_activity_id="https://example.com/activity/1",
        ap_object_id="https://example.com/object/1",
    )
    gateway = _publish_gateway()
    runtime = _community_runtime(database, gateway)
    thread = _fake_thread(thread_id=200, channel_id=100)
    starter = _fake_starter_message(message_id=300)

    result = await runtime.handle_discord_thread_create(thread=thread, starter_message=starter)

    thread_group = database.get_thread_group_by_source_thread(200)
    # No delivery rows should have been created by the duplicate call.
    deliveries = database.get_thread_deliveries(thread_group.id) if thread_group else []

    assert result.status == "ignored"
    # Gateway must not have been called — AP was already done on the first event.
    gateway.publish_content.assert_not_awaited()
    # No delivery rows from the duplicate call.
    assert len(deliveries) == 0


@pytest.mark.asyncio
async def test_phase2_mirror_failure_does_not_block_source_publish(tmp_path: Path) -> None:
    """A mirror delivery failure must not roll back the source AP publish.

    System state: two accepted subscriptions for channels 100 and 101,
    one registered user. bot.fetch_forum_channel(101) raises RuntimeError.
    Action: call handle_discord_thread_create for thread 200 in channel 100.
    Assert: result status is 'published', CommunityThreadGroup exists, one source
    delivery row exists, no mirror delivery row (mirror failed), gateway called once.
    """
    database = _database(tmp_path)
    _accepted_subscription(database, channel_id=100)
    _accepted_subscription(database, channel_id=101)
    _registered_user(database)
    gateway = _publish_gateway()

    # Simulate a Discord error when trying to mirror into channel 101.
    fake_bot = SimpleNamespace(
        fetch_forum_channel=AsyncMock(side_effect=RuntimeError("discord error")),
    )
    fanout = DiscordFanout(bot=fake_bot)
    runtime = _community_runtime(database, gateway, discord_fanout=fanout)
    thread = _fake_thread(thread_id=200, channel_id=100)
    starter = _fake_starter_message(message_id=300)

    result = await runtime.handle_discord_thread_create(thread=thread, starter_message=starter)

    thread_group = database.get_thread_group_by_source_thread(200)
    deliveries = database.get_thread_deliveries(thread_group.id) if thread_group else []
    source_deliveries = [d for d in deliveries if d.role == "source"]
    mirror_deliveries = [d for d in deliveries if d.role == "mirror"]

    # AP publish succeeded despite mirror failure.
    assert result.status == "published"
    assert thread_group is not None
    # Source delivery must exist — the mirror failure must not roll back the source.
    assert len(source_deliveries) == 1
    assert source_deliveries[0].discord_channel_id == 100
    # No mirror delivery — the sibling channel raised an error.
    assert len(mirror_deliveries) == 0
    gateway.publish_content.assert_awaited_once()
