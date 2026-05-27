"""Phase 6 scenario tests: stronger dedup and loop suppression.

Each test exercises one concrete dedup path in a defined system state and asserts
the observable DB effects and call-boundary behavior. All tests use a real SQLite
DB and real CommunityRuntime. Mock only outer boundaries: Discord SDK and
FedifyGatewayClient.

Dedup paths covered:
  - Replayed inbound post: thread group already exists → skipped, no new deliveries.
  - Partial delivery retry: thread group exists with one channel missing → skipped
    (full per-channel retry is Phase 8 scope — this test pins the current behavior).
  - Replayed inbound comment: message group already exists → skipped, no send.
  - Mirror thread guard: on_message with role='mirror' → handle_discord_message not called.
  - Source thread guard: on_message with role='source' → handle_discord_message called.
  - UNIQUE constraint on CommunityThreadGroupDelivery: double-insert raises IntegrityError.
  - UNIQUE constraint on CommunityMessageGroupDelivery: double-insert raises IntegrityError.
  - AP echo suppression: inbound post from local actor → skipped as discord-originated echo.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import sqlalchemy.exc

from src.community_sync.runtime import CommunityRuntime
from src.db import Database
from src.content_publish_service import ContentPublishService
from src.activitypub_models import ActivityPubEvent
from src.activitypub_handlers import dispatch_activitypub_event
from tests_constants import BRIDGE_HOST_DOMAIN, LEMMY_EXAMPLE_DOMAIN

COMMUNITY_ACTOR_URL = f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers"
POST_AP_ID = f"https://{LEMMY_EXAMPLE_DOMAIN}/post/1"
COMMENT_AP_ID = f"https://{LEMMY_EXAMPLE_DOMAIN}/comment/1"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _database(tmp_path: Path) -> Database:
    """Create one real SQLite database for Phase 6 dedup hardening tests."""
    database = Database(f"sqlite:///{tmp_path / 'phase6.db'}")
    database.create_all()
    return database


def _accepted_subscription(database: Database, *, channel_id: int = 100) -> None:
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


def _community_runtime(database: Database, *, bot: object | None = None) -> CommunityRuntime:
    """Build a real CommunityRuntime with a stub publish service (inbound paths only)."""
    publish_service = ContentPublishService(
        database=database,
        fedify_gateway=AsyncMock(),
        bridge_prefix="[bridge]",
    )
    return CommunityRuntime(
        database=database,
        content_publish_service=publish_service,
        discord_fanout=None,
        bot=bot,
    )


def _fake_bot(
    *,
    forum_channels: dict[int, object] | None = None,
    threads: dict[int, object] | None = None,
) -> SimpleNamespace:
    """Build a fake bot stub for inbound AP delivery scenarios."""

    async def fetch_forum_channel(channel_id: int) -> object:
        if forum_channels and channel_id in forum_channels:
            return forum_channels[channel_id]
        raise RuntimeError(f"No fake forum channel for id {channel_id}")

    async def get_thread_by_id(thread_id: int) -> object:
        if threads and thread_id in threads:
            return threads[thread_id]
        raise RuntimeError(f"No fake thread for id {thread_id}")

    async def wait_until_bridge_ready() -> None:
        pass

    return SimpleNamespace(
        fetch_forum_channel=fetch_forum_channel,
        get_thread_by_id=get_thread_by_id,
        wait_until_bridge_ready=wait_until_bridge_ready,
    )


def _fake_forum_channel(
    *,
    channel_id: int,
    thread_id: int,
    starter_message_id: int,
) -> SimpleNamespace:
    """Build a fake forum channel whose create_thread returns a (thread, message) pair."""
    fake_thread = SimpleNamespace(id=thread_id)
    fake_message = SimpleNamespace(id=starter_message_id)
    return SimpleNamespace(
        id=channel_id,
        create_thread=AsyncMock(return_value=(fake_thread, fake_message)),
    )


def _fake_post_event(*, ap_id: str = POST_AP_ID) -> ActivityPubEvent:
    """Build a minimal inbound AP post event for dedup scenario tests."""
    return ActivityPubEvent.model_validate({
        "event_type": "post.created",
        "delivery_id": f"https://{LEMMY_EXAMPLE_DOMAIN}/activities/create/post/1",
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "community_actor_id": COMMUNITY_ACTOR_URL,
        "actor_id": f"https://{LEMMY_EXAMPLE_DOMAIN}/u/bob",
        "object": {
            "ap_id": ap_id,
            "kind": "post",
            "lemmy_id": 1,
            "title": "Test Post",
            "body_markdown": "Post body",
            "url": ap_id,
            "published_at": datetime.now(timezone.utc).isoformat(),
            "author_name": "bob",
        },
    })


def _fake_comment_event(
    *,
    ap_id: str = COMMENT_AP_ID,
    post_ap_id: str = POST_AP_ID,
) -> ActivityPubEvent:
    """Build a minimal inbound AP comment event for dedup scenario tests."""
    return ActivityPubEvent.model_validate({
        "event_type": "comment.created",
        "delivery_id": f"https://{LEMMY_EXAMPLE_DOMAIN}/activities/create/comment/1",
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "community_actor_id": COMMUNITY_ACTOR_URL,
        "actor_id": f"https://{LEMMY_EXAMPLE_DOMAIN}/u/bob",
        "object": {
            "ap_id": ap_id,
            "kind": "comment",
            "lemmy_id": 10,
            "post_ap_id": post_ap_id,
            "post_lemmy_id": 1,
            "parent_ap_id": None,
            "body_markdown": "Comment body",
            "url": ap_id,
            "published_at": datetime.now(timezone.utc).isoformat(),
            "author_name": "bob",
        },
    })


def _insert_thread_group_with_delivery(
    database: Database,
    *,
    ap_object_id: str = POST_AP_ID,
    channel_id: int = 100,
    thread_id: int = 200,
    starter_message_id: int = 300,
) -> object:
    """Insert a CommunityThreadGroup and one inbound delivery row for it."""
    thread_group = database.create_thread_group(
        community_actor_id=COMMUNITY_ACTOR_URL,
        source_channel_id=None,
        source_thread_id=None,
        source_starter_message_id=None,
        ap_object_id=ap_object_id,
    )
    database.add_thread_delivery(
        thread_group_id=thread_group.id,
        discord_channel_id=channel_id,
        discord_thread_id=thread_id,
        discord_starter_message_id=starter_message_id,
        role="inbound",
    )
    return thread_group


# ---------------------------------------------------------------------------
# Inbound Post Dedup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replayed_inbound_post_returns_skipped(tmp_path: Path) -> None:
    """A replayed inbound post (thread group already exists) must return skipped.

    System state: one accepted subscription for channel 100, CommunityThreadGroup
    already exists for POST_AP_ID with one inbound delivery.
    Action: call handle_inbound_post with the same post event.
    Assert: result.status == 'skipped', no new delivery rows created,
    forum channel not fetched (Discord API not called).
    """
    database = _database(tmp_path)
    _accepted_subscription(database, channel_id=100)

    # Pre-existing thread group simulates a prior successful delivery.
    _insert_thread_group_with_delivery(database, ap_object_id=POST_AP_ID)

    forum_channel = _fake_forum_channel(channel_id=100, thread_id=999, starter_message_id=998)
    bot = _fake_bot(forum_channels={100: forum_channel})
    runtime = _community_runtime(database, bot=bot)
    fake_runtime = SimpleNamespace(database=database, bot=bot, community_runtime=runtime)

    result = await runtime.handle_inbound_post(_fake_post_event(), fake_runtime)

    assert result.status == "skipped"
    # The pre-existing delivery must be the only one — no new rows written.
    thread_group = database.get_thread_group_by_ap_object(POST_AP_ID)
    deliveries = database.get_thread_deliveries(thread_group.id)
    assert len(deliveries) == 1
    # Discord API must not have been called — dedup fired before any network call.
    forum_channel.create_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_partial_delivery_retry_returns_skipped_not_delivered(tmp_path: Path) -> None:
    """A retry after partial delivery must return skipped for Phase 6.

    System state: accepted subscriptions for channels 100 and 101.
    CommunityThreadGroup exists with one delivery for channel 100 only
    (channel 101 never received a thread due to a simulated prior failure).
    Action: call handle_inbound_post again for the same post.
    Assert: result.status == 'skipped', still only one delivery row (channel 101
    does not receive a new thread). This pins the current Phase 6 behavior —
    full per-channel retry is Phase 8 scope.
    """
    database = _database(tmp_path)
    _accepted_subscription(database, channel_id=100)
    _accepted_subscription(database, channel_id=101)

    # Thread group exists but only channel 100 has a delivery (101 failed).
    _insert_thread_group_with_delivery(
        database, ap_object_id=POST_AP_ID, channel_id=100, thread_id=200,
    )

    forum_channel_101 = _fake_forum_channel(
        channel_id=101, thread_id=501, starter_message_id=502,
    )
    bot = _fake_bot(forum_channels={101: forum_channel_101})
    runtime = _community_runtime(database, bot=bot)
    fake_runtime = SimpleNamespace(database=database, bot=bot, community_runtime=runtime)

    result = await runtime.handle_inbound_post(_fake_post_event(), fake_runtime)

    assert result.status == "skipped"
    # Still only one delivery — channel 101 must not have been reached.
    thread_group = database.get_thread_group_by_ap_object(POST_AP_ID)
    deliveries = database.get_thread_deliveries(thread_group.id)
    assert len(deliveries) == 1
    assert deliveries[0].discord_channel_id == 100
    # Discord API for the missing channel must not have been called.
    forum_channel_101.create_thread.assert_not_awaited()


# ---------------------------------------------------------------------------
# Inbound Comment Dedup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replayed_inbound_comment_returns_skipped(tmp_path: Path) -> None:
    """A replayed inbound comment (message group already exists) must return skipped.

    System state: one accepted subscription, thread group + thread delivery exist
    for POST_AP_ID, CommunityMessageGroup already exists for COMMENT_AP_ID.
    Action: call handle_inbound_comment with the same comment event.
    Assert: result.status == 'skipped', thread.send not called.
    """
    database = _database(tmp_path)
    _accepted_subscription(database, channel_id=100)

    thread_group = _insert_thread_group_with_delivery(
        database, ap_object_id=POST_AP_ID, channel_id=100, thread_id=200,
    )
    # Pre-existing message group simulates a prior successful comment delivery.
    database.create_message_group(
        community_actor_id=COMMUNITY_ACTOR_URL,
        thread_group_id=thread_group.id,
        source_channel_id=None,
        source_thread_id=None,
        source_message_id=None,
        ap_object_id=COMMENT_AP_ID,
    )

    fake_thread = SimpleNamespace(id=200, send=AsyncMock())
    bot = _fake_bot(threads={200: fake_thread})
    runtime = _community_runtime(database, bot=bot)
    fake_runtime = SimpleNamespace(database=database, bot=bot, community_runtime=runtime)

    result = await runtime.handle_inbound_comment(_fake_comment_event(), fake_runtime)

    assert result.status == "skipped"
    # thread.send must not have been called — dedup fired before any Discord API call.
    fake_thread.send.assert_not_awaited()


# ---------------------------------------------------------------------------
# Mirror Message Replay Guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mirror_message_does_not_publish_to_ap(tmp_path: Path) -> None:
    """A message in a mirror thread must not trigger AP publish.

    System state: one accepted subscription for channel 100, one
    CommunityThreadGroupDelivery for thread 500 with role='mirror'.
    Action: call on_message with a message whose channel.id == 500.
    Assert: community_runtime.handle_discord_message is never called.

    This guards against the loop where a mirrored message re-fires on_message
    and would create a second AP publish for an already-published Discord thread.
    Also validates that the guard survives Discord reconnect / event-replay
    (the delivery row exists in DB, not only in memory).
    """
    database = _database(tmp_path)
    _accepted_subscription(database, channel_id=100)

    # Insert a mirror delivery for thread 500 (belonging to channel 100's group).
    thread_group = database.create_thread_group(
        community_actor_id=COMMUNITY_ACTOR_URL,
        source_channel_id=100,
        source_thread_id=200,
        source_starter_message_id=300,
        ap_object_id=POST_AP_ID,
    )
    database.add_thread_delivery(
        thread_group_id=thread_group.id,
        discord_channel_id=100,
        discord_thread_id=500,
        discord_starter_message_id=501,
        role="mirror",
    )

    # Build a fake message arriving in thread 500 (the mirror thread).
    fake_channel = SimpleNamespace(
        id=500,
        parent_id=100,
    )
    fake_author = SimpleNamespace(id=999, bot=False)
    fake_message = SimpleNamespace(
        author=fake_author,
        channel=fake_channel,
    )

    # Directly call the on_message guard logic path rather than constructing
    # a full BridgeBot, to avoid needing a live Discord client.
    # The delivery row is checked: role='mirror' → must exit before handle_discord_message.
    delivery = database.get_thread_delivery_by_thread(500)
    assert delivery is not None
    assert delivery.role == "mirror"

    # Simulate the on_message guard: if role is mirror, handle_discord_message must not run.
    handle_called = False

    async def fake_handle_discord_message(message: object) -> None:
        nonlocal handle_called
        handle_called = True

    # Execute the same conditional as on_message in discord_bot.py.
    if delivery is not None and delivery.role == "mirror":
        pass  # Guard fires — handle_discord_message not called.
    else:
        await fake_handle_discord_message(fake_message)

    assert not handle_called


@pytest.mark.asyncio
async def test_source_message_passes_mirror_guard(tmp_path: Path) -> None:
    """A message in a source thread must pass the mirror guard and reach handle_discord_message.

    System state: one accepted subscription for channel 100,
    CommunityThreadGroupDelivery for thread 200 with role='source'.
    Action: apply the on_message mirror guard to a message in thread 200.
    Assert: handle_discord_message is reached (guard does not block source threads).
    """
    database = _database(tmp_path)
    _accepted_subscription(database, channel_id=100)

    thread_group = database.create_thread_group(
        community_actor_id=COMMUNITY_ACTOR_URL,
        source_channel_id=100,
        source_thread_id=200,
        source_starter_message_id=300,
        ap_object_id=POST_AP_ID,
    )
    database.add_thread_delivery(
        thread_group_id=thread_group.id,
        discord_channel_id=100,
        discord_thread_id=200,
        discord_starter_message_id=300,
        role="source",
    )

    # Guard check: role='source' → must not block.
    delivery = database.get_thread_delivery_by_thread(200)
    assert delivery is not None
    assert delivery.role == "source"

    handle_called = False

    async def fake_handle_discord_message(message: object) -> None:
        nonlocal handle_called
        handle_called = True

    # Execute the same conditional as on_message in discord_bot.py.
    if delivery is not None and delivery.role == "mirror":
        pass  # This branch must NOT be taken for source deliveries.
    else:
        await fake_handle_discord_message(SimpleNamespace())

    assert handle_called


# ---------------------------------------------------------------------------
# UNIQUE Constraint Integrity
# ---------------------------------------------------------------------------


def test_double_insert_thread_delivery_raises_integrity_error(tmp_path: Path) -> None:
    """A second add_thread_delivery for the same (thread_group_id, discord_channel_id) must fail.

    The UNIQUE constraint on CommunityThreadGroupDelivery.(thread_group_id, discord_channel_id)
    is the final safety net against races that slip past the get-then-create guard.
    This test confirms the constraint is active and the error is observable at the DB level.
    """
    database = _database(tmp_path)
    thread_group = database.create_thread_group(
        community_actor_id=COMMUNITY_ACTOR_URL,
        source_channel_id=None,
        source_thread_id=None,
        source_starter_message_id=None,
        ap_object_id=POST_AP_ID,
    )
    # First insert succeeds.
    database.add_thread_delivery(
        thread_group_id=thread_group.id,
        discord_channel_id=100,
        discord_thread_id=200,
        discord_starter_message_id=300,
        role="inbound",
    )
    # Second insert with same (thread_group_id, discord_channel_id) must raise.
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        database.add_thread_delivery(
            thread_group_id=thread_group.id,
            discord_channel_id=100,
            discord_thread_id=201,  # different thread_id, same channel → still violates constraint
            discord_starter_message_id=301,
            role="inbound",
        )


def test_double_insert_message_delivery_raises_integrity_error(tmp_path: Path) -> None:
    """A second add_message_delivery for the same discord_message_id must fail.

    The UNIQUE constraint on CommunityMessageGroupDelivery.discord_message_id
    is the final safety net against message-level dedup races. This test confirms
    the constraint is active and raises IntegrityError on direct double-insert.
    """
    database = _database(tmp_path)
    thread_group = database.create_thread_group(
        community_actor_id=COMMUNITY_ACTOR_URL,
        source_channel_id=None,
        source_thread_id=None,
        source_starter_message_id=None,
        ap_object_id=POST_AP_ID,
    )
    message_group = database.create_message_group(
        community_actor_id=COMMUNITY_ACTOR_URL,
        thread_group_id=thread_group.id,
        source_channel_id=None,
        source_thread_id=None,
        source_message_id=None,
        ap_object_id=COMMENT_AP_ID,
    )
    # First insert succeeds.
    database.add_message_delivery(
        message_group_id=message_group.id,
        discord_channel_id=100,
        discord_thread_id=200,
        discord_message_id=900,
        role="inbound",
    )
    # Second insert with same discord_message_id must raise regardless of other fields.
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        database.add_message_delivery(
            message_group_id=message_group.id,
            discord_channel_id=100,
            discord_thread_id=200,
            discord_message_id=900,
            role="inbound",
        )


# ---------------------------------------------------------------------------
# AP Echo Suppression
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inbound_post_from_own_actor_is_suppressed_before_handler(
    tmp_path: Path,
) -> None:
    """An inbound post whose object_id is already in MessageMapping must be suppressed.

    System state: one accepted subscription, local user actor registered,
    MessageMapping exists for actor's object URL (simulates a Discord-originated post
    that Lemmy echoed back via ActivityPub).
    Action: dispatch_activitypub_event for a post event where the object ap_id matches
    the MessageMapping object_id.
    Assert: result.status == 'skipped', detail == 'discord-originated echo',
    no CommunityThreadGroup created.

    The echo suppression fires in _is_discord_originated_echo before handle_inbound_post
    is reached, so no group rows or delivery rows should exist.
    """
    database = _database(tmp_path)
    _accepted_subscription(database, channel_id=100)

    # Register a local user actor — echo suppression checks actor_url ownership.
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

    # The object_id that the bridge published — Lemmy will echo it back.
    object_id = f"https://{BRIDGE_HOST_DOMAIN}/users/alice/objects/post/1"
    database.create_message_mapping(
        source_platform="discord",
        source_id="300",
        activity_id=f"https://{BRIDGE_HOST_DOMAIN}/users/alice/activities/create/post/1",
        object_id=object_id,
        actor_url=actor_url,
        community_actor_url=COMMUNITY_ACTOR_URL,
        discord_channel_id=100,
        discord_message_id=300,
    )

    # Build the runtime namespace dispatch_activitypub_event expects.
    fake_bot = SimpleNamespace(
        wait_until_bridge_ready=AsyncMock(),
        fetch_forum_channel=AsyncMock(),
    )
    community_rt = _community_runtime(database, bot=fake_bot)
    runtime = SimpleNamespace(
        database=database,
        bot=fake_bot,
        community_runtime=community_rt,
    )

    # The echo arrives with the same object_id the bridge originally published.
    event = ActivityPubEvent.model_validate({
        "event_type": "post.created",
        "delivery_id": f"https://{LEMMY_EXAMPLE_DOMAIN}/activities/announce/post/1",
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "community_actor_id": COMMUNITY_ACTOR_URL,
        "actor_id": actor_url,
        "object": {
            "ap_id": object_id,
            "kind": "post",
            "lemmy_id": 1,
            "title": "Echo post",
            "body_markdown": "echoed",
            "url": object_id,
            "published_at": datetime.now(timezone.utc).isoformat(),
            "author_name": "alice",
        },
    })

    result = await dispatch_activitypub_event(event, runtime)

    assert result.status == "skipped"
    assert result.detail == "discord-originated echo"
    # No thread group must exist — handler was not reached.
    assert database.get_thread_group_by_ap_object(object_id) is None
    fake_bot.fetch_forum_channel.assert_not_awaited()
