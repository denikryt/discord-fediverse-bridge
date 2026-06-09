"""Phase 3 scenario tests: local Discord message fanout via CommunityRuntime.

Each test exercises a concrete action in a defined system state and asserts
observable DB effects. All five tests use a real SQLite DB, real CommunityRuntime,
and real ContentPublishService. Mock only outer boundaries: FedifyGatewayClient,
bot.get_thread_by_id, and thread.send.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.community_sync.discord_fanout import DiscordFanout
from src.community_sync.runtime import CommunityRuntime
from src.db import Database
from src.content_publish_service import ContentPublishService
from src.fedify_gateway_client import PublishContentResult
from tests_constants import BRIDGE_HOST_DOMAIN, LEMMY_EXAMPLE_DOMAIN

COMMUNITY_ACTOR_URL = f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers"


def _database(tmp_path: Path) -> Database:
    """Create one real SQLite repository for Phase 3 message fanout scenario tests."""
    database = Database(f"sqlite:///{tmp_path / 'phase3-fanout.db'}")
    database.create_all()
    return database


def _accepted_subscription(database: Database, *, channel_id: int) -> None:
    """Insert one accepted community subscription for the shared hackers community."""
    database.remote_subscriptions.create_subscription(
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
    database.users.create_user(
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
        activity_id=activity_id or f"https://{BRIDGE_HOST_DOMAIN}/users/alice/activities/create/comment/1",
        object_id=object_id or f"https://{BRIDGE_HOST_DOMAIN}/users/alice/objects/comment/1",
        community_actor_url=COMMUNITY_ACTOR_URL,
    )
    return gateway


def _publish_service(database: Database, gateway: AsyncMock) -> ContentPublishService:
    """Build a ContentPublishService wired to one fake gateway boundary."""
    return ContentPublishService(
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
        content_publish_service=_publish_service(database, gateway),
        discord_fanout=discord_fanout,
    )


def _fake_message(
    *,
    message_id: int = 400,
    thread_id: int = 200,
    channel_id: int = 100,
    author_id: int = 123,
) -> SimpleNamespace:
    """Return one fake Discord thread message with the minimal attributes required."""
    # channel simulates a discord.Thread: it has .id (thread) and .parent_id (forum channel).
    channel = SimpleNamespace(id=thread_id, parent_id=channel_id)
    author = SimpleNamespace(id=author_id, display_name="Alice", name="alice")
    return SimpleNamespace(
        id=message_id,
        content="hello from inside the thread",
        author=author,
        channel=channel,
    )


def _create_thread_group_with_source_delivery(
    database: Database,
    *,
    channel_id: int = 100,
    thread_id: int = 200,
    starter_message_id: int = 300,
) -> object:
    """Insert a CommunityThreadGroup, PostLink, and one source delivery row.

    Simulates the DB state left by a successful Phase 2 publish_thread_starter
    + handle_discord_thread_create pair. publish_thread_message requires a PostLink
    with lemmy_post_ap_id to resolve comment context, so this helper creates both
    the Phase 2 thread group rows and the legacy PostLink that the existing service
    path depends on.
    """
    ap_object_id = f"https://{BRIDGE_HOST_DOMAIN}/objects/post/1"
    ap_activity_id = f"https://{BRIDGE_HOST_DOMAIN}/activities/create/post/1"
    # PostLink is required by publish_thread_message to resolve post context.
    database.legacy_lemmy_mappings.create_post_link(
        lemmy_post_id=-thread_id,
        lemmy_post_ap_id=ap_object_id,
        discord_forum_channel_id=channel_id,
        discord_forum_thread_id=thread_id,
        discord_starter_message_id=starter_message_id,
        direction="discord_to_activitypub",
    )
    thread_group = database.discord_fanout_groups.create_thread_group(
        community_actor_id=COMMUNITY_ACTOR_URL,
        source_channel_id=channel_id,
        source_thread_id=thread_id,
        source_starter_message_id=starter_message_id,
        ap_activity_id=ap_activity_id,
        ap_object_id=ap_object_id,
    )
    database.discord_fanout_groups.add_thread_delivery(
        thread_group_id=thread_group.id,
        discord_channel_id=channel_id,
        discord_thread_id=thread_id,
        discord_starter_message_id=starter_message_id,
        role="source",
    )
    return thread_group


@pytest.mark.asyncio
async def test_phase3_single_subscription_message_published_no_mirror(tmp_path: Path) -> None:
    """With one subscription, message is AP-published and one source delivery row is written.

    System state: one accepted subscription for channel 100, one registered user,
    CommunityThreadGroup for source_thread_id=200 with one source delivery row.
    Action: call handle_discord_message for message 400 in thread 200 (channel 100).
    Assert: result status is 'published', CommunityMessageGroup exists with correct
    source_message_id, exactly one delivery row with role='source', no mirror rows,
    gateway called once.
    """
    database = _database(tmp_path)
    _accepted_subscription(database, channel_id=100)
    _registered_user(database)
    _create_thread_group_with_source_delivery(database, channel_id=100, thread_id=200)
    gateway = _publish_gateway()
    runtime = _community_runtime(database, gateway)
    message = _fake_message(message_id=400, thread_id=200, channel_id=100)

    result = await runtime.handle_discord_message(message=message)

    message_group = database.discord_fanout_groups.get_message_group_by_source_message(400)
    deliveries = database.discord_fanout_groups.get_message_deliveries(message_group.id) if message_group else []
    mirror_deliveries = [d for d in deliveries if d.role == "mirror"]

    assert result.status == "published"
    # Message group must exist with the correct source message id.
    assert message_group is not None
    assert message_group.source_message_id == 400
    # Exactly one source delivery row for thread 200.
    assert len(deliveries) == 1
    assert deliveries[0].role == "source"
    assert deliveries[0].discord_thread_id == 200
    assert deliveries[0].discord_channel_id == 100
    # No mirror rows — only one subscription, no siblings.
    assert len(mirror_deliveries) == 0
    gateway.publish_content.assert_awaited_once()


@pytest.mark.asyncio
async def test_phase3_two_subscriptions_message_mirrored_to_sibling_thread(tmp_path: Path) -> None:
    """With two subscriptions, a message is delivered into the sibling mirror thread.

    System state: two accepted subscriptions for channels 100 and 101, one registered
    user, CommunityThreadGroup for source thread 200 (channel 100), source delivery
    row for thread 200 and mirror delivery row for thread 500 (channel 101).
    bot.get_thread_by_id(500) returns a fake thread whose send() returns a fake message.
    Action: call handle_discord_message for message 400 in thread 200 (channel 100).
    Assert: result status is 'published', CommunityMessageGroup exists, two delivery
    rows (source for thread 200, mirror for thread 500), fake_thread.send called once,
    gateway called once.
    """
    database = _database(tmp_path)
    _accepted_subscription(database, channel_id=100)
    _accepted_subscription(database, channel_id=101)
    _registered_user(database)
    thread_group = _create_thread_group_with_source_delivery(
        database, channel_id=100, thread_id=200
    )
    # Add mirror delivery row for sibling thread 500 in channel 101.
    database.discord_fanout_groups.add_thread_delivery(
        thread_group_id=thread_group.id,
        discord_channel_id=101,
        discord_thread_id=500,
        discord_starter_message_id=501,
        role="mirror",
    )
    gateway = _publish_gateway()

    # Fake mirror thread: send() returns a fake sent message with id=600.
    fake_sent_message = SimpleNamespace(id=600)
    fake_mirror_thread = SimpleNamespace(
        id=500,
        send=AsyncMock(return_value=fake_sent_message),
    )
    fake_bot = SimpleNamespace(
        get_thread_by_id=AsyncMock(return_value=fake_mirror_thread),
    )
    fanout = DiscordFanout(bot=fake_bot, mutation_tracker=fake_bot)
    runtime = _community_runtime(database, gateway, discord_fanout=fanout)
    message = _fake_message(message_id=400, thread_id=200, channel_id=100)

    result = await runtime.handle_discord_message(message=message)

    message_group = database.discord_fanout_groups.get_message_group_by_source_message(400)
    deliveries = database.discord_fanout_groups.get_message_deliveries(message_group.id) if message_group else []
    source_deliveries = [d for d in deliveries if d.role == "source"]
    mirror_deliveries = [d for d in deliveries if d.role == "mirror"]

    assert result.status == "published"
    assert message_group is not None
    # Source delivery for thread 200.
    assert len(source_deliveries) == 1
    assert source_deliveries[0].discord_thread_id == 200
    assert source_deliveries[0].discord_channel_id == 100
    # Mirror delivery for thread 500 with the sent message id.
    assert len(mirror_deliveries) == 1
    assert mirror_deliveries[0].discord_thread_id == 500
    assert mirror_deliveries[0].discord_channel_id == 101
    assert mirror_deliveries[0].discord_message_id == 600
    # send() must have been called exactly once (for thread 500 only).
    fake_mirror_thread.send.assert_awaited_once()
    # Gateway called exactly once for AP publish.
    gateway.publish_content.assert_awaited_once()


@pytest.mark.asyncio
async def test_phase3_duplicate_message_is_ignored(tmp_path: Path) -> None:
    """A duplicate message event (e.g. Discord reconnect) is skipped silently.

    System state: one accepted subscription for channel 100, one registered user,
    CommunityMessageGroup already exists for source_message_id=400.
    Action: call handle_discord_message for message 400 again.
    Assert: result status is 'ignored', gateway not called, no new delivery rows.
    """
    database = _database(tmp_path)
    _accepted_subscription(database, channel_id=100)
    _registered_user(database)
    thread_group = _create_thread_group_with_source_delivery(
        database, channel_id=100, thread_id=200
    )
    # Pre-insert the message group to simulate a prior successful publish.
    database.discord_fanout_groups.create_message_group(
        community_actor_id=COMMUNITY_ACTOR_URL,
        thread_group_id=thread_group.id,
        source_channel_id=100,
        source_thread_id=200,
        source_message_id=400,
        ap_activity_id=f"https://{BRIDGE_HOST_DOMAIN}/activities/create/comment/1",
        ap_object_id=f"https://{BRIDGE_HOST_DOMAIN}/objects/comment/1",
    )
    gateway = _publish_gateway()
    runtime = _community_runtime(database, gateway)
    message = _fake_message(message_id=400, thread_id=200, channel_id=100)

    result = await runtime.handle_discord_message(message=message)

    message_group = database.discord_fanout_groups.get_message_group_by_source_message(400)
    deliveries = database.discord_fanout_groups.get_message_deliveries(message_group.id) if message_group else []

    assert result.status == "ignored"
    # Gateway must not have been called — AP was already done on the first event.
    gateway.publish_content.assert_not_awaited()
    # No new delivery rows from the duplicate call.
    assert len(deliveries) == 0


@pytest.mark.asyncio
async def test_phase3_message_in_legacy_thread_is_ignored_without_thread_group(tmp_path: Path) -> None:
    """A message in a pre-Phase-2 thread (PostLink only) is ignored — no CommunityThreadGroup.

    System state: one accepted subscription for channel 100, one registered user,
    PostLink for thread 200 (legacy — no CommunityThreadGroup exists).
    Action: call handle_discord_message for message 400 in thread 200.
    Assert: result status is 'ignored' with reason 'no_post_context', gateway not called.
    Phase 5 removes the PostLink fallback path — only CommunityThreadGroup is used.
    """
    database = _database(tmp_path)
    _accepted_subscription(database, channel_id=100)
    _registered_user(database)
    # Insert a legacy PostLink for thread 200 but no CommunityThreadGroup.
    # Phase 5: publish_thread_message only consults CommunityThreadGroup, so this
    # thread has no resolvable post context and the message is ignored.
    database.legacy_lemmy_mappings.create_post_link(
        lemmy_post_id=-200,
        lemmy_post_ap_id=f"https://{BRIDGE_HOST_DOMAIN}/objects/post/legacy",
        discord_forum_channel_id=100,
        discord_forum_thread_id=200,
        discord_starter_message_id=300,
        direction="discord_to_activitypub",
    )
    gateway = _publish_gateway()
    runtime = _community_runtime(database, gateway)
    message = _fake_message(message_id=400, thread_id=200, channel_id=100)

    result = await runtime.handle_discord_message(message=message)

    message_group = database.discord_fanout_groups.get_message_group_by_source_message(400)

    assert result.status == "ignored"
    assert result.reason == "no_post_context"
    # No message group created — no thread group found.
    assert message_group is None
    # Gateway must not have been called.
    gateway.publish_content.assert_not_awaited()


@pytest.mark.asyncio
async def test_phase3_mirror_message_failure_does_not_block_source_publish(tmp_path: Path) -> None:
    """A mirror message delivery failure must not roll back the source AP publish.

    System state: two accepted subscriptions for channels 100 and 101, one registered
    user, CommunityThreadGroup for source thread 200 with source delivery, mirror
    delivery row for thread 500. bot.get_thread_by_id(500) raises RuntimeError.
    Action: call handle_discord_message for message 400 in thread 200.
    Assert: result status is 'published', CommunityMessageGroup exists, one source
    delivery row, no mirror delivery row (mirror failed), gateway called once.
    """
    database = _database(tmp_path)
    _accepted_subscription(database, channel_id=100)
    _accepted_subscription(database, channel_id=101)
    _registered_user(database)
    thread_group = _create_thread_group_with_source_delivery(
        database, channel_id=100, thread_id=200
    )
    # Add mirror delivery row for sibling thread 500.
    database.discord_fanout_groups.add_thread_delivery(
        thread_group_id=thread_group.id,
        discord_channel_id=101,
        discord_thread_id=500,
        discord_starter_message_id=501,
        role="mirror",
    )
    gateway = _publish_gateway()

    # Simulate a Discord error when fetching the mirror thread.
    fake_bot = SimpleNamespace(
        get_thread_by_id=AsyncMock(side_effect=RuntimeError("discord error")),
    )
    fanout = DiscordFanout(bot=fake_bot, mutation_tracker=fake_bot)
    runtime = _community_runtime(database, gateway, discord_fanout=fanout)
    message = _fake_message(message_id=400, thread_id=200, channel_id=100)

    result = await runtime.handle_discord_message(message=message)

    message_group = database.discord_fanout_groups.get_message_group_by_source_message(400)
    deliveries = database.discord_fanout_groups.get_message_deliveries(message_group.id) if message_group else []
    source_deliveries = [d for d in deliveries if d.role == "source"]
    mirror_deliveries = [d for d in deliveries if d.role == "mirror"]

    # AP publish succeeded despite mirror failure.
    assert result.status == "published"
    assert message_group is not None
    # Source delivery must exist — the mirror failure must not roll back the source.
    assert len(source_deliveries) == 1
    assert source_deliveries[0].discord_thread_id == 200
    # No mirror delivery — the sibling thread raised an error.
    assert len(mirror_deliveries) == 0
    gateway.publish_content.assert_awaited_once()
