"""Phase 2 scenario tests: local Discord thread fanout via CommunityRuntime.

Each test exercises a concrete action in a defined system state and asserts
observable DB effects. All five tests use a real SQLite DB, real CommunityRuntime,
and real ContentPublishService. Mock only outer boundaries: FedifyGatewayClient,
bot.fetch_forum_channel, and forum_channel.create_thread.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.community_sync.discord_fanout import DiscordFanout
from support.db import (
    COMMUNITY_ACTOR_URL,
    add_accepted_subscription,
    add_registered_user,
    build_database,
)
from support.discord import (
    build_forum_channel_object_result,
    build_starter_message,
    build_thread,
)
from support.gateway import build_gateway_mock
from support.runtime import build_community_runtime, build_test_policy_service


@pytest.mark.asyncio
async def test_phase2_single_subscription_no_mirror_created(tmp_path: Path) -> None:
    """With one subscription, AP publishes normally and no mirror delivery is created.

    System state: one accepted subscription for channel 100, one registered user.
    Action: call handle_discord_thread_create for thread 200 in channel 100.
    Assert: result status is 'published', CommunityThreadGroup exists, one source
    delivery row exists, no mirror delivery row exists, gateway called once.
    """
    database = build_database(tmp_path, "phase2-fanout.db")
    add_accepted_subscription(database, channel_id=100)
    add_registered_user(database)
    gateway = build_gateway_mock()
    runtime = build_community_runtime(database, fedify_gateway=gateway)
    thread = build_thread(thread_id=200, channel_id=100)
    starter = build_starter_message(message_id=300)

    result = await runtime.handle_discord_thread_create(thread=thread, starter_message=starter)

    thread_group = database.discord_fanout_groups.get_thread_group_by_source_thread(200)
    deliveries = database.discord_fanout_groups.get_thread_deliveries(thread_group.id) if thread_group else []
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
    database = build_database(tmp_path, "phase2-fanout.db")
    add_accepted_subscription(database, channel_id=100)
    add_accepted_subscription(database, channel_id=101)
    add_registered_user(database)
    gateway = build_gateway_mock()

    # Fake mirror forum channel with a controllable create_thread return.
    fake_forum_channel = build_forum_channel_object_result(
        channel_id=101, thread_id=500, starter_message_id=501
    )
    fake_bot = SimpleNamespace(
        fetch_forum_channel=AsyncMock(return_value=fake_forum_channel),
    )
    fanout = DiscordFanout(
        bot=fake_bot,
        mutation_tracker=fake_bot,
        database=database,
        policy_service=build_test_policy_service(database),
    )
    runtime = build_community_runtime(
        database, fedify_gateway=gateway, discord_fanout=fanout
    )
    thread = build_thread(thread_id=200, channel_id=100)
    starter = build_starter_message(message_id=300)

    result = await runtime.handle_discord_thread_create(thread=thread, starter_message=starter)

    thread_group = database.discord_fanout_groups.get_thread_group_by_source_thread(200)
    deliveries = database.discord_fanout_groups.get_thread_deliveries(thread_group.id) if thread_group else []
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
    database = build_database(tmp_path, "phase2-fanout.db")
    add_accepted_subscription(database, channel_id=100)
    add_registered_user(database)
    # Pre-insert the thread group to simulate a prior successful publish.
    database.discord_fanout_groups.create_thread_group(
        community_actor_id=COMMUNITY_ACTOR_URL,
        source_channel_id=100,
        source_thread_id=200,
        source_starter_message_id=300,
        ap_activity_id="https://example.com/activity/1",
        ap_object_id="https://example.com/object/1",
    )
    gateway = build_gateway_mock()
    runtime = build_community_runtime(database, fedify_gateway=gateway)
    thread = build_thread(thread_id=200, channel_id=100)
    starter = build_starter_message(message_id=300)

    result = await runtime.handle_discord_thread_create(thread=thread, starter_message=starter)

    thread_group = database.discord_fanout_groups.get_thread_group_by_source_thread(200)
    # No delivery rows should have been created by the duplicate call.
    deliveries = database.discord_fanout_groups.get_thread_deliveries(thread_group.id) if thread_group else []

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
    database = build_database(tmp_path, "phase2-fanout.db")
    add_accepted_subscription(database, channel_id=100)
    add_accepted_subscription(database, channel_id=101)
    add_registered_user(database)
    gateway = build_gateway_mock()

    # Simulate a Discord error when trying to mirror into channel 101.
    fake_bot = SimpleNamespace(
        fetch_forum_channel=AsyncMock(side_effect=RuntimeError("discord error")),
    )
    fanout = DiscordFanout(
        bot=fake_bot,
        mutation_tracker=fake_bot,
        database=database,
        policy_service=build_test_policy_service(database),
    )
    runtime = build_community_runtime(
        database, fedify_gateway=gateway, discord_fanout=fanout
    )
    thread = build_thread(thread_id=200, channel_id=100)
    starter = build_starter_message(message_id=300)

    result = await runtime.handle_discord_thread_create(thread=thread, starter_message=starter)

    thread_group = database.discord_fanout_groups.get_thread_group_by_source_thread(200)
    deliveries = database.discord_fanout_groups.get_thread_deliveries(thread_group.id) if thread_group else []
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
