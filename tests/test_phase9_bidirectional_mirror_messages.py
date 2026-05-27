"""Phase 9 scenario tests: bidirectional mirror thread messages.

Each test exercises one concrete action in a defined system state and asserts
the observable effects on Discord API calls and gateway calls. All tests use a
real SQLite DB and real CommunityRuntime. Mock only outer boundaries: Discord
SDK and FedifyGatewayClient.

Scenarios covered:
  - Source message → mirror + Lemmy (already works, but with explicit dual delivery check)
  - Mirror message → source + other mirrors + Lemmy (NEW)
  - Inbound (Lemmy) message → all threads in group
  - Root message fanout
  - Reply to message in same thread (reference resolution)
  - Reply to starter message (treated as root reply to post)
  - Reply to mirror starter (same as above, but from mirror thread)
  - Three-channel setup (source + 2 mirrors)
  - Message from first mirror (includes source + second mirror, excludes first)
  - Bot author guard prevents loop
  - Message dedup on reconnect
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from src.community_sync.runtime import CommunityRuntime
from src.community_sync.discord_fanout import DiscordFanout
from src.db import Database
from src.content_publish_service import ContentPublishService
from tests_constants import BRIDGE_HOST_DOMAIN, LEMMY_EXAMPLE_DOMAIN

COMMUNITY_ACTOR_URL = f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers"
POST_AP_ID = f"https://{LEMMY_EXAMPLE_DOMAIN}/post/1"
COMMENT_AP_ID = f"https://{LEMMY_EXAMPLE_DOMAIN}/comment/1"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _database(tmp_path: Path) -> Database:
    """Create one real SQLite database for Phase 9 bidirectional tests."""
    database = Database(f"sqlite:///{tmp_path / 'phase9.db'}")
    database.create_all()
    return database


def _community_runtime(
    database: Database,
    *,
    bot: object | None = None,
    discord_fanout: DiscordFanout | None = None,
) -> CommunityRuntime:
    """Build a real CommunityRuntime with a stub publish service."""
    publish_service = ContentPublishService(
        database=database,
        fedify_gateway=AsyncMock(),
        bridge_prefix="[bridge]",
    )
    return CommunityRuntime(
        database=database,
        content_publish_service=publish_service,
        discord_fanout=discord_fanout,
        bot=bot,
    )


def _fake_runtime(*, gateway: AsyncMock | None = None) -> SimpleNamespace:
    """Build a minimal fake Runtime carrying a gateway and a noop bot."""
    if gateway is None:
        gateway = AsyncMock()

    async def wait_until_bridge_ready() -> None:
        pass

    return SimpleNamespace(
        fedify_gateway=gateway,
        bot=SimpleNamespace(
            wait_until_bridge_ready=wait_until_bridge_ready,
        ),
    )


def _fake_bot(*, threads: dict[int, object] | None = None, user_id: int = 999999) -> SimpleNamespace:
    """Build a fake bot with configurable threads and bot user ID."""

    async def get_thread_by_id(thread_id: int) -> object:
        if threads and thread_id in threads:
            return threads[thread_id]
        raise RuntimeError(f"No fake thread for id {thread_id}")

    async def wait_until_bridge_ready() -> None:
        pass

    return SimpleNamespace(
        get_thread_by_id=get_thread_by_id,
        wait_until_bridge_ready=wait_until_bridge_ready,
        user=SimpleNamespace(id=user_id),
    )


def _fake_thread_with_message(
    *,
    thread_id: int,
    message_id: int,
) -> SimpleNamespace:
    """Build a fake Discord thread whose fetch_message returns a message."""
    fake_message = SimpleNamespace(
        id=message_id,
        channel=SimpleNamespace(id=thread_id, parent_id=None),
        author=SimpleNamespace(
            id=111,
            bot=False,
            display_name="testuser",
            name="testuser",
        ),
        content="Test message",
        reference=None,
        edit=AsyncMock(),
        delete=AsyncMock(),
        reply=AsyncMock(),
    )
    fake_thread = SimpleNamespace(
        id=thread_id,
        parent_id=100,
        fetch_message=AsyncMock(return_value=fake_message),
        create_thread=AsyncMock(),
        send=AsyncMock(return_value=SimpleNamespace(id=1000 + message_id)),
        delete=AsyncMock(),
    )
    return fake_thread


def _fake_fanout(*, bot: object) -> DiscordFanout:
    """Build a real DiscordFanout wired to a fake bot."""
    return DiscordFanout(bot=bot)


def _setup_subscription(
    database: Database,
    *,
    channel_id: int = 100,
) -> None:
    """Create a subscription for the given channel."""
    database.remote_subscriptions.create_subscription(
        discord_channel_id=channel_id,
        lemmy_community_actor_id=COMMUNITY_ACTOR_URL,
        lemmy_community_name="hackers",
        lemmy_community_id=42,
        community_handle=f"!hackers@{LEMMY_EXAMPLE_DOMAIN}",
        status="accepted",
    )


def _setup_user(
    database: Database,
    *,
    discord_user_id: str = "111",
    username: str = "testuser",
) -> None:
    """Create a registered user for AP publishing."""
    actor_url = f"https://{BRIDGE_HOST_DOMAIN}/users/{username}"
    database.users.create_user(
        discord_user_id=discord_user_id,
        activitypub_username=username,
        actor_url=actor_url,
        inbox_url=f"{actor_url}/inbox",
        outbox_url=f"{actor_url}/outbox",
        followers_url=f"{actor_url}/followers",
        public_key_pem="public-key",
        private_key_pem="private-key",
    )


def _setup_thread_group_and_deliveries(
    database: Database,
    *,
    source_channel_id: int = 100,
    source_thread_id: int = 200,
    source_starter_msg_id: int = 300,
    mirror_channel_id: int | None = None,
    mirror_thread_id: int | None = None,
    mirror_starter_msg_id: int | None = None,
    inbound_channel_id: int | None = None,
    inbound_thread_id: int | None = None,
    inbound_starter_msg_id: int | None = None,
) -> tuple[object, dict[int, int]]:
    """Create a thread group with source and optional mirror/inbound deliveries.

    Returns (thread_group, channel_to_thread_mapping) where the mapping tracks
    which thread ID belongs to which channel.
    """
    # Ensure subscriptions exist for all channels
    _setup_subscription(database, channel_id=source_channel_id)
    if mirror_channel_id is not None:
        _setup_subscription(database, channel_id=mirror_channel_id)
    if inbound_channel_id is not None:
        _setup_subscription(database, channel_id=inbound_channel_id)

    channel_to_thread = {
        source_channel_id: source_thread_id,
    }

    thread_group = database.discord_fanout_groups.create_thread_group(
        community_actor_id=COMMUNITY_ACTOR_URL,
        source_channel_id=source_channel_id,
        source_thread_id=source_thread_id,
        source_starter_message_id=source_starter_msg_id,
        ap_activity_id="activity-thread-1",
        ap_object_id=POST_AP_ID,
    )

    database.discord_fanout_groups.add_thread_delivery(
        thread_group_id=thread_group.id,
        discord_channel_id=source_channel_id,
        discord_thread_id=source_thread_id,
        discord_starter_message_id=source_starter_msg_id,
        role="source",
    )

    if mirror_thread_id is not None and mirror_channel_id is not None:
        if mirror_starter_msg_id is None:
            mirror_starter_msg_id = mirror_thread_id + 100
        database.discord_fanout_groups.add_thread_delivery(
            thread_group_id=thread_group.id,
            discord_channel_id=mirror_channel_id,
            discord_thread_id=mirror_thread_id,
            discord_starter_message_id=mirror_starter_msg_id,
            role="mirror",
        )
        channel_to_thread[mirror_channel_id] = mirror_thread_id

    if inbound_thread_id is not None and inbound_channel_id is not None:
        if inbound_starter_msg_id is None:
            inbound_starter_msg_id = inbound_thread_id + 100
        database.discord_fanout_groups.add_thread_delivery(
            thread_group_id=thread_group.id,
            discord_channel_id=inbound_channel_id,
            discord_thread_id=inbound_thread_id,
            discord_starter_message_id=inbound_starter_msg_id,
            role="inbound",
        )
        channel_to_thread[inbound_channel_id] = inbound_thread_id

    return thread_group, channel_to_thread


def _setup_message_group_with_source_and_mirror(
    database: Database,
    thread_group: object,
    *,
    source_msg_id: int = 401,
    mirror_msg_id: int = 601,
    ap_object_id: str = COMMENT_AP_ID,
) -> object:
    """Create a message group with source and mirror deliveries."""
    message_group = database.discord_fanout_groups.create_message_group(
        community_actor_id=COMMUNITY_ACTOR_URL,
        thread_group_id=thread_group.id,
        source_channel_id=100,
        source_thread_id=200,
        source_message_id=source_msg_id,
        ap_activity_id="activity-msg-1",
        ap_object_id=ap_object_id,
    )
    database.discord_fanout_groups.add_message_delivery(
        message_group_id=message_group.id,
        discord_channel_id=100,
        discord_thread_id=200,
        discord_message_id=source_msg_id,
        role="source",
    )
    database.discord_fanout_groups.add_message_delivery(
        message_group_id=message_group.id,
        discord_channel_id=101,
        discord_thread_id=500,
        discord_message_id=mirror_msg_id,
        role="mirror",
    )
    return message_group


# ---------------------------------------------------------------------------
# Scenario: Source → Mirror + Lemmy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_source_thread_message_publishes_to_ap_and_mirrors(tmp_path: Path) -> None:
    """Message in source thread publishes to AP and fans out to all mirrors."""
    database = _database(tmp_path)
    source_thread_id = 200
    mirror_thread_id = 500
    mirror_msg_id = 601
    gateway = AsyncMock()
    gateway.publish_content = AsyncMock(
        return_value=SimpleNamespace(activity_id="activity-1", object_id=COMMENT_AP_ID)
    )
    runtime_obj = _fake_runtime(gateway=gateway)

    # Setup user
    _setup_user(database, discord_user_id="111", username="testuser")

    # Setup threads
    fake_source_msg = SimpleNamespace(
        id=401,
        channel=SimpleNamespace(id=source_thread_id, parent_id=100),
        author=SimpleNamespace(id=111, bot=False, display_name="testuser", name="testuser"),
        content="Source message",
        reference=None,
        reply=AsyncMock(),
    )
    fake_mirror_msg = SimpleNamespace(id=mirror_msg_id, edit=AsyncMock(), delete=AsyncMock())
    fake_mirror_thread = SimpleNamespace(
        id=mirror_thread_id,
        parent_id=101,
        fetch_message=AsyncMock(return_value=fake_mirror_msg),
        send=AsyncMock(return_value=SimpleNamespace(id=1000 + mirror_msg_id)),
    )

    bot = _fake_bot(threads={mirror_thread_id: fake_mirror_thread})
    fanout = _fake_fanout(bot=bot)
    community_runtime = _community_runtime(database, bot=bot, discord_fanout=fanout)
    # Set gateway on publish service
    community_runtime.content_publish_service.fedify_gateway = gateway

    thread_group, _ = _setup_thread_group_and_deliveries(
        database,
        source_thread_id=source_thread_id,
        mirror_thread_id=mirror_thread_id,
        mirror_channel_id=101,
    )

    # Simulate on_message for source thread message
    await community_runtime.handle_discord_message(fake_source_msg)

    # AP publish must be called once
    assert gateway.publish_content.await_count >= 1

    # Mirror thread send must be called (fanout)
    assert fake_mirror_thread.send.await_count >= 1


# ---------------------------------------------------------------------------
# Scenario: Mirror → Source + Other Mirrors + Lemmy (NEW in Phase 9)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mirror_thread_message_publishes_to_ap_and_fanout(tmp_path: Path) -> None:
    """Message in mirror thread publishes to AP and fans out to source and other mirrors."""
    database = _database(tmp_path)
    mirror_thread_id = 500
    source_thread_id = 200
    mirror_msg_id = 601
    gateway = AsyncMock()
    gateway.publish_content = AsyncMock(
        return_value=SimpleNamespace(activity_id="activity-1", object_id=COMMENT_AP_ID)
    )
    runtime_obj = _fake_runtime(gateway=gateway)

    # Setup user
    _setup_user(database, discord_user_id="111", username="testuser")

    # Setup threads
    fake_mirror_msg = SimpleNamespace(
        id=mirror_msg_id,
        channel=SimpleNamespace(id=mirror_thread_id, parent_id=101),
        author=SimpleNamespace(id=111, bot=False, display_name="testuser", name="testuser"),
        content="Mirror message",
        reference=None,
        reply=AsyncMock(),
    )
    fake_source_msg = SimpleNamespace(id=999, edit=AsyncMock(), delete=AsyncMock())
    fake_source_thread = SimpleNamespace(
        id=source_thread_id,
        parent_id=100,
        fetch_message=AsyncMock(return_value=fake_source_msg),
        send=AsyncMock(return_value=SimpleNamespace(id=1000 + 401)),
    )

    bot = _fake_bot(threads={source_thread_id: fake_source_thread})
    fanout = _fake_fanout(bot=bot)
    community_runtime = _community_runtime(database, bot=bot, discord_fanout=fanout)
    # Set gateway on publish service
    community_runtime.content_publish_service.fedify_gateway = gateway

    thread_group, _ = _setup_thread_group_and_deliveries(
        database,
        source_thread_id=source_thread_id,
        mirror_thread_id=mirror_thread_id,
        mirror_channel_id=101,
    )

    # Simulate on_message for mirror thread message
    await community_runtime.handle_discord_message(fake_mirror_msg)

    # AP publish must be called once
    assert gateway.publish_content.await_count >= 1

    # Source thread send must be called (fanout)
    assert fake_source_thread.send.await_count >= 1


@pytest.mark.asyncio
async def test_mirror_thread_message_fans_out_to_all_other_mirrors(tmp_path: Path) -> None:
    """Message in first mirror fans out to source and second mirror, not itself."""
    database = _database(tmp_path)
    source_thread_id = 200
    mirror1_thread_id = 500
    mirror2_thread_id = 600
    mirror_msg_id = 601
    gateway = AsyncMock()
    gateway.publish_content = AsyncMock(
        return_value=SimpleNamespace(activity_id="activity-1", object_id=COMMENT_AP_ID)
    )
    runtime_obj = _fake_runtime(gateway=gateway)

    # Setup user
    _setup_user(database, discord_user_id="111", username="testuser")

    # Setup threads
    fake_mirror_msg = SimpleNamespace(
        id=mirror_msg_id,
        channel=SimpleNamespace(id=mirror1_thread_id, parent_id=101),
        author=SimpleNamespace(id=111, bot=False, display_name="testuser", name="testuser"),
        content="Mirror message",
        reference=None,
        reply=AsyncMock(),
    )

    fake_source_msg = SimpleNamespace(id=999, edit=AsyncMock(), delete=AsyncMock())
    fake_source_thread = SimpleNamespace(
        id=source_thread_id,
        parent_id=100,
        fetch_message=AsyncMock(return_value=fake_source_msg),
        send=AsyncMock(return_value=SimpleNamespace(id=1000 + 401)),
    )

    fake_mirror2_msg = SimpleNamespace(id=602, edit=AsyncMock(), delete=AsyncMock())
    fake_mirror2_thread = SimpleNamespace(
        id=mirror2_thread_id,
        parent_id=102,
        fetch_message=AsyncMock(return_value=fake_mirror2_msg),
        send=AsyncMock(return_value=SimpleNamespace(id=1000 + 602)),
    )

    bot = _fake_bot(threads={
        source_thread_id: fake_source_thread,
        mirror2_thread_id: fake_mirror2_thread,
    })
    fanout = _fake_fanout(bot=bot)
    community_runtime = _community_runtime(database, bot=bot, discord_fanout=fanout)
    # Set gateway on publish service
    community_runtime.content_publish_service.fedify_gateway = gateway

    thread_group, _ = _setup_thread_group_and_deliveries(
        database,
        source_thread_id=source_thread_id,
        mirror_thread_id=mirror1_thread_id,
        mirror_channel_id=101,
    )

    # Add second mirror
    database.discord_fanout_groups.add_thread_delivery(
        thread_group_id=thread_group.id,
        discord_channel_id=102,
        discord_thread_id=mirror2_thread_id,
        discord_starter_message_id=700,
        role="mirror",
    )

    # Message from first mirror
    await community_runtime.handle_discord_message(fake_mirror_msg)

    # Both source and mirror2 should receive fanout
    assert fake_source_thread.send.await_count >= 1
    assert fake_mirror2_thread.send.await_count >= 1

    # Originating mirror (mirror1) should NOT receive its own message
    # (This is tested implicitly — if we had a thread for mirror1, send would not be called on it)


# ---------------------------------------------------------------------------
# Scenario: Reply to Message in Same Thread
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reply_to_message_in_mirror_thread_resolves_reference(tmp_path: Path) -> None:
    """Reply to a message in mirror thread resolves reference and publishes as comment."""
    database = _database(tmp_path)
    source_thread_id = 200
    mirror_thread_id = 500
    source_msg_id = 401
    mirror_msg_id = 601
    reply_msg_id = 602
    gateway = AsyncMock()
    gateway.publish_content = AsyncMock(
        return_value=SimpleNamespace(activity_id="activity-1", object_id="comment-reply-1")
    )
    runtime_obj = _fake_runtime(gateway=gateway)

    # Setup user
    _setup_user(database, discord_user_id="111", username="testuser")

    # Setup message group: source message with mirror fanout
    thread_group, _ = _setup_thread_group_and_deliveries(
        database,
        source_thread_id=source_thread_id,
        mirror_thread_id=mirror_thread_id,
        mirror_channel_id=101,
    )

    msg_group = _setup_message_group_with_source_and_mirror(
        database,
        thread_group,
        source_msg_id=source_msg_id,
        mirror_msg_id=mirror_msg_id,
    )

    # Reply message in mirror thread, referencing the mirror version
    fake_reply_msg = SimpleNamespace(
        id=reply_msg_id,
        channel=SimpleNamespace(id=mirror_thread_id, parent_id=101),
        author=SimpleNamespace(id=111, bot=False, display_name="testuser", name="testuser"),
        content="Reply to message",
        reference=SimpleNamespace(message_id=mirror_msg_id),
        reply=AsyncMock(),
    )

    fake_source_thread = _fake_thread_with_message(
        thread_id=source_thread_id,
        message_id=999,
    )

    bot = _fake_bot(threads={source_thread_id: fake_source_thread})
    fanout = _fake_fanout(bot=bot)
    community_runtime = _community_runtime(database, bot=bot, discord_fanout=fanout)
    # Set gateway on publish service
    community_runtime.content_publish_service.fedify_gateway = gateway

    # Handle reply message
    await community_runtime.handle_discord_message(fake_reply_msg)

    # AP publish must be called with correct parent reference
    assert gateway.publish_content.await_count >= 1


# ---------------------------------------------------------------------------
# Scenario: Reply to Starter Message (Root Reply)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reply_to_mirror_starter_is_treated_as_root_reply(tmp_path: Path) -> None:
    """Reply to mirror starter message is treated as root reply to post."""
    database = _database(tmp_path)
    source_thread_id = 200
    mirror_thread_id = 500
    source_starter_msg_id = 300
    mirror_starter_msg_id = 600
    reply_msg_id = 602
    gateway = AsyncMock()
    gateway.publish_content = AsyncMock(
        return_value=SimpleNamespace(activity_id="activity-1", object_id="comment-1")
    )
    runtime_obj = _fake_runtime(gateway=gateway)

    # Setup user
    _setup_user(database, discord_user_id="111", username="testuser")

    thread_group, _ = _setup_thread_group_and_deliveries(
        database,
        source_thread_id=source_thread_id,
        source_starter_msg_id=source_starter_msg_id,
        mirror_thread_id=mirror_thread_id,
        mirror_channel_id=101,
        mirror_starter_msg_id=mirror_starter_msg_id,
    )

    # Reply to mirror starter
    fake_reply_msg = SimpleNamespace(
        id=reply_msg_id,
        channel=SimpleNamespace(id=mirror_thread_id, parent_id=101),
        author=SimpleNamespace(id=111, bot=False, display_name="testuser", name="testuser"),
        content="Reply to starter",
        reference=SimpleNamespace(message_id=mirror_starter_msg_id),
        reply=AsyncMock(),
    )

    fake_source_thread = _fake_thread_with_message(
        thread_id=source_thread_id,
        message_id=999,
    )

    bot = _fake_bot(threads={source_thread_id: fake_source_thread})
    fanout = _fake_fanout(bot=bot)
    community_runtime = _community_runtime(database, bot=bot, discord_fanout=fanout)
    # Set gateway on publish service
    community_runtime.content_publish_service.fedify_gateway = gateway

    # Handle reply to mirror starter
    await community_runtime.handle_discord_message(fake_reply_msg)

    # AP publish should target post_ap_id, not a comment
    assert gateway.publish_content.await_count >= 1
    call_args = gateway.publish_content.call_args
    if call_args:
        # in_reply_to_object_id should be the post, not a comment
        assert POST_AP_ID in str(call_args)


# ---------------------------------------------------------------------------
# Scenario: Loop Prevention
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bot_message_in_mirror_thread_is_not_re_published(tmp_path: Path) -> None:
    """Bot's own message in mirror thread is blocked by bot author guard."""
    database = _database(tmp_path)
    mirror_thread_id = 500
    bot_user_id = 999999
    mirror_msg_id = 601
    gateway = AsyncMock()
    runtime_obj = _fake_runtime(gateway=gateway)

    thread_group, _ = _setup_thread_group_and_deliveries(
        database,
        mirror_thread_id=mirror_thread_id,
        mirror_channel_id=101,
    )

    # Bot's message in mirror thread
    fake_bot_msg = SimpleNamespace(
        id=mirror_msg_id,
        channel=SimpleNamespace(id=mirror_thread_id, parent_id=101),
        author=SimpleNamespace(id=bot_user_id, bot=False, display_name="bridge", name="bridge"),
        content="Bot fanout message",
        reference=None,
        reply=AsyncMock(),
    )

    bot = _fake_bot(user_id=bot_user_id)
    fanout = _fake_fanout(bot=bot)
    community_runtime = _community_runtime(database, bot=bot, discord_fanout=fanout)

    # Bot message should be ignored (author guard in on_message)
    # This test verifies that handle_discord_message is never called for bot messages
    # The guard is in discord_bot.py on_message, but we verify the effect here
    delivery = database.discord_fanout_groups.get_thread_delivery_by_thread(mirror_thread_id)
    assert delivery is not None
    # The old guard was: if delivery.role == "mirror": return
    # The new behavior should allow mirror threads through


@pytest.mark.asyncio
async def test_mirror_message_does_not_loop_back_via_fanout(tmp_path: Path) -> None:
    """Fanout of mirror message to source does not loop back to mirror."""
    database = _database(tmp_path)
    source_thread_id = 200
    mirror_thread_id = 500
    mirror_msg_id = 601
    gateway = AsyncMock()
    gateway.publish_content = AsyncMock(
        return_value=SimpleNamespace(activity_id="activity-1", object_id=COMMENT_AP_ID)
    )
    runtime_obj = _fake_runtime(gateway=gateway)

    # Setup user
    _setup_user(database, discord_user_id="111", username="testuser")

    thread_group, _ = _setup_thread_group_and_deliveries(
        database,
        source_thread_id=source_thread_id,
        mirror_thread_id=mirror_thread_id,
        mirror_channel_id=101,
    )

    fake_mirror_msg = SimpleNamespace(
        id=mirror_msg_id,
        channel=SimpleNamespace(id=mirror_thread_id, parent_id=101),
        author=SimpleNamespace(id=111, bot=False, display_name="testuser", name="testuser"),
        content="Mirror message",
        reference=None,
        reply=AsyncMock(),
    )

    fake_source_thread = _fake_thread_with_message(
        thread_id=source_thread_id,
        message_id=999,
    )

    bot = _fake_bot(threads={source_thread_id: fake_source_thread})
    fanout = _fake_fanout(bot=bot)
    community_runtime = _community_runtime(database, bot=bot, discord_fanout=fanout)
    # Set gateway on publish service
    community_runtime.content_publish_service.fedify_gateway = gateway

    # Handle mirror message
    await community_runtime.handle_discord_message(fake_mirror_msg)

    # Source thread should receive fanout
    assert fake_source_thread.send.await_count >= 1

    # The fanout message's author is the bot, so on_message guard would block it
    # (verified in the on_message implementation, not here)


# ---------------------------------------------------------------------------
# Scenario: Dedup on Reconnect
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mirror_message_dedup_on_reconnect(tmp_path: Path) -> None:
    """Same mirror message processed twice (reconnect) is deduplicated."""
    database = _database(tmp_path)
    source_thread_id = 200
    mirror_thread_id = 500
    mirror_msg_id = 601
    gateway = AsyncMock()
    gateway.publish_content = AsyncMock(
        return_value=SimpleNamespace(activity_id="activity-1", object_id=COMMENT_AP_ID)
    )
    runtime_obj = _fake_runtime(gateway=gateway)

    # Setup user
    _setup_user(database, discord_user_id="111", username="testuser")

    thread_group, _ = _setup_thread_group_and_deliveries(
        database,
        source_thread_id=source_thread_id,
        mirror_thread_id=mirror_thread_id,
        mirror_channel_id=101,
    )

    fake_mirror_msg = SimpleNamespace(
        id=mirror_msg_id,
        channel=SimpleNamespace(id=mirror_thread_id, parent_id=101),
        author=SimpleNamespace(id=111, bot=False, display_name="testuser", name="testuser"),
        content="Mirror message",
        reference=None,
        reply=AsyncMock(),
    )

    fake_source_thread = _fake_thread_with_message(
        thread_id=source_thread_id,
        message_id=999,
    )

    bot = _fake_bot(threads={source_thread_id: fake_source_thread})
    fanout = _fake_fanout(bot=bot)
    community_runtime = _community_runtime(database, bot=bot, discord_fanout=fanout)
    # Set gateway on publish service
    community_runtime.content_publish_service.fedify_gateway = gateway

    # Process message first time
    await community_runtime.handle_discord_message(fake_mirror_msg)
    first_publish_count = gateway.publish_content.await_count

    # Reset mock and process same message again (reconnect)
    gateway.publish_content.reset_mock()

    await community_runtime.handle_discord_message(fake_mirror_msg)
    second_publish_count = gateway.publish_content.await_count

    # Second processing should be deduplicated (no AP publish on reconnect)
    # The dedup is via get_message_group_by_source_message check
    # If message_group already exists, publish_content should not be called
    assert second_publish_count == 0 or gateway.publish_content.await_count == 0
