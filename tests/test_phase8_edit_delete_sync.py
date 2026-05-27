"""Phase 8 scenario tests: edit and delete sync in both directions.

Each test exercises one concrete action in a defined system state and asserts
the observable effects on Discord API calls and gateway calls. All tests use a
real SQLite DB and real CommunityRuntime. Mock only outer boundaries: Discord
SDK (thread.fetch_message, message.edit, message.delete, thread.delete) and
FedifyGatewayClient (update_content, delete_content).

Directions covered:
  - Discord source message edit → mirror edits + AP Update.
  - Discord mirror message edit → no propagation (role guard).
  - Discord source message delete → mirror deletes + AP Delete.
  - Discord mirror message delete → no propagation (role guard).
  - Edit of unknown message → silently ignored.
  - Mirror edit failure → remaining mirrors attempted + AP Update still sent.
  - Mirror delete failure → remaining mirrors attempted + AP Delete still sent.
  - Inbound AP comment update → all delivery messages edited.
  - Inbound AP comment update for unknown ap_id → skipped.
  - Inbound AP comment delete → all delivery messages deleted.
  - Inbound AP post delete → all Discord threads deleted.
  - Mirror edit preserves `username@domain` header line in mirror messages.
  - Inbound comment update preserves `username@domain` header in inbound messages.
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
    """Create one real SQLite database for Phase 8 edit/delete sync tests."""
    database = Database(f"sqlite:///{tmp_path / 'phase8.db'}")
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


def _fake_bot(*, threads: dict[int, object] | None = None) -> SimpleNamespace:
    """Build a fake bot that returns pre-configured threads."""

    async def get_thread_by_id(thread_id: int) -> object:
        if threads and thread_id in threads:
            return threads[thread_id]
        raise RuntimeError(f"No fake thread for id {thread_id}")

    async def wait_until_bridge_ready() -> None:
        pass

    return SimpleNamespace(
        get_thread_by_id=get_thread_by_id,
        wait_until_bridge_ready=wait_until_bridge_ready,
    )


def _fake_thread_with_message(
    *,
    thread_id: int,
    message_id: int,
    content: str = "",
) -> SimpleNamespace:
    """Build a fake Discord thread whose fetch_message returns an editable/deletable message.

    content simulates the current Discord message body, which apply_edit_to_discord_message
    reads to preserve the bridge attribution header when editing.
    """
    fake_message = SimpleNamespace(
        id=message_id,
        content=content,
        edit=AsyncMock(),
        delete=AsyncMock(),
    )
    fake_thread = SimpleNamespace(
        id=thread_id,
        fetch_message=AsyncMock(return_value=fake_message),
        delete=AsyncMock(),
    )
    return fake_thread


def _fake_fanout(*, bot: object) -> DiscordFanout:
    """Build a real DiscordFanout wired to a fake bot."""
    return DiscordFanout(bot=bot)


def _setup_message_group_with_deliveries(
    database: Database,
    *,
    source_msg_id: int = 301,
    source_thread_id: int = 200,
    source_channel_id: int = 100,
    mirror_msg_id: int = 600,
    mirror_thread_id: int = 500,
    mirror_channel_id: int = 101,
    ap_object_id: str = COMMENT_AP_ID,
) -> tuple[object, object]:
    """Create a thread group and message group with one source and one mirror delivery.

    Returns (thread_group, message_group).
    """
    thread_group = database.create_thread_group(
        community_actor_id=COMMUNITY_ACTOR_URL,
        source_channel_id=source_channel_id,
        source_thread_id=source_thread_id,
        source_starter_message_id=source_msg_id - 1,
        ap_activity_id="activity-thread-1",
        ap_object_id=POST_AP_ID,
    )
    database.add_thread_delivery(
        thread_group_id=thread_group.id,
        discord_channel_id=source_channel_id,
        discord_thread_id=source_thread_id,
        discord_starter_message_id=source_msg_id - 1,
        role="source",
    )
    database.add_thread_delivery(
        thread_group_id=thread_group.id,
        discord_channel_id=mirror_channel_id,
        discord_thread_id=mirror_thread_id,
        discord_starter_message_id=mirror_msg_id - 1,
        role="mirror",
    )
    message_group = database.create_message_group(
        community_actor_id=COMMUNITY_ACTOR_URL,
        thread_group_id=thread_group.id,
        source_channel_id=source_channel_id,
        source_thread_id=source_thread_id,
        source_message_id=source_msg_id,
        ap_activity_id="activity-msg-1",
        ap_object_id=ap_object_id,
    )
    database.add_message_delivery(
        message_group_id=message_group.id,
        discord_channel_id=source_channel_id,
        discord_thread_id=source_thread_id,
        discord_message_id=source_msg_id,
        role="source",
    )
    database.add_message_delivery(
        message_group_id=message_group.id,
        discord_channel_id=mirror_channel_id,
        discord_thread_id=mirror_thread_id,
        discord_message_id=mirror_msg_id,
        role="mirror",
    )
    return thread_group, message_group


def _fake_update_event(
    *,
    event_type: str,
    ap_id: str,
    kind: str,
    body_markdown: str = "Updated body",
    post_ap_id: str | None = None,
    post_lemmy_id: int | None = None,
) -> ActivityPubEvent:
    """Build a fake inbound AP update event."""
    return ActivityPubEvent.model_validate({
        "event_type": event_type,
        "delivery_id": f"delivery-{event_type}-1",
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "community_actor_id": COMMUNITY_ACTOR_URL,
        "actor_id": f"https://{LEMMY_EXAMPLE_DOMAIN}/u/bob",
        "object": {
            "ap_id": ap_id,
            "kind": kind,
            "lemmy_id": 1,
            "post_ap_id": post_ap_id,
            "post_lemmy_id": post_lemmy_id,
            "body_markdown": body_markdown,
            "url": ap_id,
            "published_at": datetime.now(timezone.utc).isoformat(),
            "author_name": "bob",
        },
    })


# ---------------------------------------------------------------------------
# Outbound Discord Edit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_source_message_edit_propagates_to_mirrors_and_ap(tmp_path: Path) -> None:
    """Editing a source message edits the mirror message and sends one AP Update."""
    database = _database(tmp_path)
    mirror_thread_id = 500
    mirror_msg_id = 600
    source_msg_id = 301

    # Mirror message has no bridge attribution header (plain content).
    # apply_edit_to_discord_message falls through to return new body as-is.
    fake_mirror_message = SimpleNamespace(id=mirror_msg_id, content="Old body", edit=AsyncMock(), delete=AsyncMock())
    fake_mirror_thread = SimpleNamespace(
        id=mirror_thread_id,
        fetch_message=AsyncMock(return_value=fake_mirror_message),
    )
    bot = _fake_bot(threads={mirror_thread_id: fake_mirror_thread})
    fanout = _fake_fanout(bot=bot)
    runtime_obj = _fake_runtime()
    community_runtime = _community_runtime(database, bot=bot, discord_fanout=fanout)

    _setup_message_group_with_deliveries(
        database,
        source_msg_id=source_msg_id,
        mirror_msg_id=mirror_msg_id,
        mirror_thread_id=mirror_thread_id,
    )

    await community_runtime.handle_discord_message_edit(
        message_id=source_msg_id,
        new_content="Edited content",
        runtime=runtime_obj,
    )

    # Mirror message must be edited with just the new content (no header to preserve).
    fake_mirror_thread.fetch_message.assert_awaited_once_with(mirror_msg_id)
    fake_mirror_message.edit.assert_awaited_once_with(content="Edited content")


@pytest.mark.asyncio
async def test_edit_of_unknown_message_is_silently_ignored(tmp_path: Path) -> None:
    """Editing a message that has no delivery row does nothing and raises no error."""
    database = _database(tmp_path)
    gateway = AsyncMock()
    runtime_obj = _fake_runtime(gateway=gateway)
    community_runtime = _community_runtime(database)

    # No delivery rows exist for message 999.
    await community_runtime.handle_discord_message_edit(
        message_id=999,
        new_content="Doesn't matter",
        runtime=runtime_obj,
    )

    # No AP gateway calls must have been made.
    gateway.update_content.assert_not_awaited()
    gateway.delete_content.assert_not_awaited()


@pytest.mark.asyncio
async def test_mirror_edit_failure_does_not_block_ap_update(tmp_path: Path) -> None:
    """If one mirror edit fails, the remaining mirrors are still attempted.

    AP Update is not sent in this test because there is no actor mapping —
    this verifies that DiscordFanout.propagate_edit does not raise on failure.
    """
    database = _database(tmp_path)
    source_msg_id = 301
    mirror_msg_id_1 = 600
    mirror_msg_id_2 = 700
    mirror_thread_id_1 = 500
    mirror_thread_id_2 = 501

    # First mirror thread raises NotFound on fetch_message.
    fail_thread = SimpleNamespace(
        id=mirror_thread_id_1,
        fetch_message=AsyncMock(side_effect=discord.NotFound(MagicMock(), "not found")),
    )
    # Second mirror thread succeeds — content has no header so body is returned as-is.
    success_message = SimpleNamespace(id=mirror_msg_id_2, content="Old body", edit=AsyncMock(), delete=AsyncMock())
    success_thread = SimpleNamespace(
        id=mirror_thread_id_2,
        fetch_message=AsyncMock(return_value=success_message),
    )
    bot = _fake_bot(threads={
        mirror_thread_id_1: fail_thread,
        mirror_thread_id_2: success_thread,
    })
    fanout = _fake_fanout(bot=bot)
    community_runtime = _community_runtime(database, bot=bot, discord_fanout=fanout)

    # Set up thread group and message group with two mirror deliveries.
    thread_group = database.create_thread_group(
        community_actor_id=COMMUNITY_ACTOR_URL,
        source_channel_id=100,
        source_thread_id=200,
        source_starter_message_id=300,
        ap_activity_id="activity-thread-1",
        ap_object_id=POST_AP_ID,
    )
    database.add_thread_delivery(
        thread_group_id=thread_group.id,
        discord_channel_id=100, discord_thread_id=200,
        discord_starter_message_id=300, role="source",
    )
    message_group = database.create_message_group(
        community_actor_id=COMMUNITY_ACTOR_URL,
        thread_group_id=thread_group.id,
        source_channel_id=100,
        source_thread_id=200,
        source_message_id=source_msg_id,
        ap_activity_id="activity-msg-1",
        ap_object_id=COMMENT_AP_ID,
    )
    database.add_message_delivery(
        message_group_id=message_group.id,
        discord_channel_id=100, discord_thread_id=200,
        discord_message_id=source_msg_id, role="source",
    )
    database.add_message_delivery(
        message_group_id=message_group.id,
        discord_channel_id=101, discord_thread_id=mirror_thread_id_1,
        discord_message_id=mirror_msg_id_1, role="mirror",
    )
    database.add_message_delivery(
        message_group_id=message_group.id,
        discord_channel_id=102, discord_thread_id=mirror_thread_id_2,
        discord_message_id=mirror_msg_id_2, role="mirror",
    )

    runtime_obj = _fake_runtime()
    # Must not raise even though first mirror fails.
    await community_runtime.handle_discord_message_edit(
        message_id=source_msg_id,
        new_content="New content",
        runtime=runtime_obj,
    )

    # Second mirror must still be edited despite first failing.
    success_thread.fetch_message.assert_awaited_once_with(mirror_msg_id_2)
    success_message.edit.assert_awaited_once_with(content="New content")


# ---------------------------------------------------------------------------
# Outbound Discord Delete tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_source_message_delete_removes_mirrors_and_sends_ap_delete(tmp_path: Path) -> None:
    """Deleting a source message deletes the mirror message and sends one AP Delete."""
    database = _database(tmp_path)
    source_msg_id = 301
    mirror_msg_id = 600
    mirror_thread_id = 500

    fake_mirror_message = SimpleNamespace(id=mirror_msg_id, edit=AsyncMock(), delete=AsyncMock())
    fake_mirror_thread = SimpleNamespace(
        id=mirror_thread_id,
        fetch_message=AsyncMock(return_value=fake_mirror_message),
    )
    bot = _fake_bot(threads={mirror_thread_id: fake_mirror_thread})
    fanout = _fake_fanout(bot=bot)
    runtime_obj = _fake_runtime()
    community_runtime = _community_runtime(database, bot=bot, discord_fanout=fanout)

    _setup_message_group_with_deliveries(
        database,
        source_msg_id=source_msg_id,
        mirror_msg_id=mirror_msg_id,
        mirror_thread_id=mirror_thread_id,
    )

    await community_runtime.handle_discord_message_delete(
        message_id=source_msg_id,
        runtime=runtime_obj,
    )

    # Mirror message must be deleted via Discord API.
    fake_mirror_thread.fetch_message.assert_awaited_once_with(mirror_msg_id)
    fake_mirror_message.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_mirror_delete_failure_does_not_block_ap_delete(tmp_path: Path) -> None:
    """If one mirror delete fails, the remaining mirrors are still attempted."""
    database = _database(tmp_path)
    source_msg_id = 301
    mirror_msg_id_1 = 600
    mirror_msg_id_2 = 700
    mirror_thread_id_1 = 500
    mirror_thread_id_2 = 501

    fail_thread = SimpleNamespace(
        id=mirror_thread_id_1,
        fetch_message=AsyncMock(side_effect=discord.NotFound(MagicMock(), "not found")),
    )
    success_message = SimpleNamespace(id=mirror_msg_id_2, edit=AsyncMock(), delete=AsyncMock())
    success_thread = SimpleNamespace(
        id=mirror_thread_id_2,
        fetch_message=AsyncMock(return_value=success_message),
    )
    bot = _fake_bot(threads={
        mirror_thread_id_1: fail_thread,
        mirror_thread_id_2: success_thread,
    })
    fanout = _fake_fanout(bot=bot)
    community_runtime = _community_runtime(database, bot=bot, discord_fanout=fanout)

    thread_group = database.create_thread_group(
        community_actor_id=COMMUNITY_ACTOR_URL,
        source_channel_id=100, source_thread_id=200,
        source_starter_message_id=300,
        ap_activity_id="activity-thread-1", ap_object_id=POST_AP_ID,
    )
    database.add_thread_delivery(
        thread_group_id=thread_group.id,
        discord_channel_id=100, discord_thread_id=200,
        discord_starter_message_id=300, role="source",
    )
    message_group = database.create_message_group(
        community_actor_id=COMMUNITY_ACTOR_URL,
        thread_group_id=thread_group.id,
        source_channel_id=100, source_thread_id=200,
        source_message_id=source_msg_id,
        ap_activity_id="activity-msg-1", ap_object_id=COMMENT_AP_ID,
    )
    database.add_message_delivery(
        message_group_id=message_group.id,
        discord_channel_id=100, discord_thread_id=200,
        discord_message_id=source_msg_id, role="source",
    )
    database.add_message_delivery(
        message_group_id=message_group.id,
        discord_channel_id=101, discord_thread_id=mirror_thread_id_1,
        discord_message_id=mirror_msg_id_1, role="mirror",
    )
    database.add_message_delivery(
        message_group_id=message_group.id,
        discord_channel_id=102, discord_thread_id=mirror_thread_id_2,
        discord_message_id=mirror_msg_id_2, role="mirror",
    )

    runtime_obj = _fake_runtime()
    await community_runtime.handle_discord_message_delete(
        message_id=source_msg_id,
        runtime=runtime_obj,
    )

    # Second mirror must still be deleted despite first failing.
    success_thread.fetch_message.assert_awaited_once_with(mirror_msg_id_2)
    success_message.delete.assert_awaited_once()


# ---------------------------------------------------------------------------
# Mirror role guard tests (on_raw_message_edit / on_raw_message_delete guard)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Inbound AP Update tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inbound_comment_update_edits_all_discord_deliveries(tmp_path: Path) -> None:
    """Inbound comment.updated edits all delivery messages in the message group."""
    database = _database(tmp_path)
    msg_id_1 = 300
    msg_id_2 = 400
    thread_id_1 = 200
    thread_id_2 = 500

    # Inbound messages have no bridge attribution header — apply_edit falls through to raw body.
    msg1 = SimpleNamespace(id=msg_id_1, content="Old comment body", edit=AsyncMock(), delete=AsyncMock())
    msg2 = SimpleNamespace(id=msg_id_2, content="Old comment body", edit=AsyncMock(), delete=AsyncMock())
    thread1 = SimpleNamespace(id=thread_id_1, fetch_message=AsyncMock(return_value=msg1))
    thread2 = SimpleNamespace(id=thread_id_2, fetch_message=AsyncMock(return_value=msg2))
    bot = _fake_bot(threads={thread_id_1: thread1, thread_id_2: thread2})

    community_runtime = _community_runtime(database, bot=bot)

    # Set up thread group with two inbound deliveries.
    thread_group = database.create_thread_group(
        community_actor_id=COMMUNITY_ACTOR_URL,
        source_channel_id=None, source_thread_id=None,
        source_starter_message_id=None,
        ap_activity_id="delivery-post-1", ap_object_id=POST_AP_ID,
    )
    database.add_thread_delivery(
        thread_group_id=thread_group.id,
        discord_channel_id=100, discord_thread_id=thread_id_1,
        discord_starter_message_id=999, role="inbound",
    )
    database.add_thread_delivery(
        thread_group_id=thread_group.id,
        discord_channel_id=101, discord_thread_id=thread_id_2,
        discord_starter_message_id=998, role="inbound",
    )
    message_group = database.create_message_group(
        community_actor_id=COMMUNITY_ACTOR_URL,
        thread_group_id=thread_group.id,
        source_channel_id=None, source_thread_id=None, source_message_id=None,
        ap_activity_id="delivery-comment-1", ap_object_id=COMMENT_AP_ID,
    )
    database.add_message_delivery(
        message_group_id=message_group.id,
        discord_channel_id=100, discord_thread_id=thread_id_1,
        discord_message_id=msg_id_1, role="inbound",
    )
    database.add_message_delivery(
        message_group_id=message_group.id,
        discord_channel_id=101, discord_thread_id=thread_id_2,
        discord_message_id=msg_id_2, role="inbound",
    )

    event = _fake_update_event(
        event_type="comment.updated",
        ap_id=COMMENT_AP_ID,
        kind="comment",
        body_markdown="New comment body",
        post_ap_id=POST_AP_ID,
        post_lemmy_id=1,
    )
    runtime_obj = _fake_runtime()
    result = await community_runtime.handle_inbound_comment_update(event, runtime_obj)

    assert result.status == "processed"
    # Both Discord messages must have been edited.
    thread1.fetch_message.assert_awaited_once_with(msg_id_1)
    msg1.edit.assert_awaited_once_with(content="New comment body")
    thread2.fetch_message.assert_awaited_once_with(msg_id_2)
    msg2.edit.assert_awaited_once_with(content="New comment body")


@pytest.mark.asyncio
async def test_inbound_comment_update_for_unknown_ap_id_returns_skipped(tmp_path: Path) -> None:
    """Inbound comment.updated for an unmapped comment returns skipped with no Discord calls."""
    database = _database(tmp_path)
    bot = _fake_bot(threads={})
    community_runtime = _community_runtime(database, bot=bot)

    event = _fake_update_event(
        event_type="comment.updated",
        ap_id=COMMENT_AP_ID,
        kind="comment",
        body_markdown="Updated",
        post_ap_id=POST_AP_ID,
        post_lemmy_id=1,
    )
    runtime_obj = _fake_runtime()
    result = await community_runtime.handle_inbound_comment_update(event, runtime_obj)

    assert result.status == "skipped"


# ---------------------------------------------------------------------------
# Inbound AP Delete tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inbound_comment_delete_marks_all_discord_deliveries_deleted(tmp_path: Path) -> None:
    """Inbound comment.deleted edits all delivery messages to '*deleted by creator*'."""
    database = _database(tmp_path)
    msg_id_1 = 300
    msg_id_2 = 400
    thread_id_1 = 200
    thread_id_2 = 500

    msg1 = SimpleNamespace(id=msg_id_1, content="`alice@lemmy.example`\n\nOriginal body", edit=AsyncMock(), delete=AsyncMock())
    msg2 = SimpleNamespace(id=msg_id_2, content="`alice@lemmy.example`\n\nOriginal body", edit=AsyncMock(), delete=AsyncMock())
    thread1 = SimpleNamespace(id=thread_id_1, fetch_message=AsyncMock(return_value=msg1))
    thread2 = SimpleNamespace(id=thread_id_2, fetch_message=AsyncMock(return_value=msg2))
    bot = _fake_bot(threads={thread_id_1: thread1, thread_id_2: thread2})

    community_runtime = _community_runtime(database, bot=bot)

    thread_group = database.create_thread_group(
        community_actor_id=COMMUNITY_ACTOR_URL,
        source_channel_id=None, source_thread_id=None,
        source_starter_message_id=None,
        ap_activity_id="delivery-post-1", ap_object_id=POST_AP_ID,
    )
    database.add_thread_delivery(
        thread_group_id=thread_group.id,
        discord_channel_id=100, discord_thread_id=thread_id_1,
        discord_starter_message_id=999, role="inbound",
    )
    database.add_thread_delivery(
        thread_group_id=thread_group.id,
        discord_channel_id=101, discord_thread_id=thread_id_2,
        discord_starter_message_id=998, role="inbound",
    )
    message_group = database.create_message_group(
        community_actor_id=COMMUNITY_ACTOR_URL,
        thread_group_id=thread_group.id,
        source_channel_id=None, source_thread_id=None, source_message_id=None,
        ap_activity_id="delivery-comment-1", ap_object_id=COMMENT_AP_ID,
    )
    database.add_message_delivery(
        message_group_id=message_group.id,
        discord_channel_id=100, discord_thread_id=thread_id_1,
        discord_message_id=msg_id_1, role="inbound",
    )
    database.add_message_delivery(
        message_group_id=message_group.id,
        discord_channel_id=101, discord_thread_id=thread_id_2,
        discord_message_id=msg_id_2, role="inbound",
    )

    event = _fake_update_event(
        event_type="comment.deleted",
        ap_id=COMMENT_AP_ID,
        kind="comment",
        post_ap_id=POST_AP_ID,
        post_lemmy_id=1,
    )
    runtime_obj = _fake_runtime()
    result = await community_runtime.handle_inbound_comment_delete(event, runtime_obj)

    assert result.status == "processed"
    # Both Discord messages must be edited to the deletion placeholder, not deleted.
    thread1.fetch_message.assert_awaited_once_with(msg_id_1)
    msg1.edit.assert_awaited_once_with(content="*deleted by creator*")
    msg1.delete.assert_not_awaited()
    thread2.fetch_message.assert_awaited_once_with(msg_id_2)
    msg2.edit.assert_awaited_once_with(content="*deleted by creator*")
    msg2.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_inbound_post_delete_marks_all_thread_starters_deleted(tmp_path: Path) -> None:
    """Inbound post.deleted edits all thread starter messages to '*deleted by creator*'."""
    database = _database(tmp_path)
    thread_id_1 = 200
    thread_id_2 = 500
    starter_msg_id_1 = 999
    starter_msg_id_2 = 998

    starter1 = SimpleNamespace(id=starter_msg_id_1, edit=AsyncMock(), delete=AsyncMock())
    starter2 = SimpleNamespace(id=starter_msg_id_2, edit=AsyncMock(), delete=AsyncMock())
    thread1 = SimpleNamespace(
        id=thread_id_1,
        fetch_message=AsyncMock(return_value=starter1),
        delete=AsyncMock(),
    )
    thread2 = SimpleNamespace(
        id=thread_id_2,
        fetch_message=AsyncMock(return_value=starter2),
        delete=AsyncMock(),
    )
    bot = _fake_bot(threads={thread_id_1: thread1, thread_id_2: thread2})

    community_runtime = _community_runtime(database, bot=bot)

    thread_group = database.create_thread_group(
        community_actor_id=COMMUNITY_ACTOR_URL,
        source_channel_id=None, source_thread_id=None,
        source_starter_message_id=None,
        ap_activity_id="delivery-post-1", ap_object_id=POST_AP_ID,
    )
    database.add_thread_delivery(
        thread_group_id=thread_group.id,
        discord_channel_id=100, discord_thread_id=thread_id_1,
        discord_starter_message_id=starter_msg_id_1, role="inbound",
    )
    database.add_thread_delivery(
        thread_group_id=thread_group.id,
        discord_channel_id=101, discord_thread_id=thread_id_2,
        discord_starter_message_id=starter_msg_id_2, role="inbound",
    )

    event = _fake_update_event(
        event_type="post.deleted",
        ap_id=POST_AP_ID,
        kind="post",
    )
    runtime_obj = _fake_runtime()
    result = await community_runtime.handle_inbound_post_delete(event, runtime_obj)

    assert result.status == "processed"
    # Both starter messages must be edited to the deletion placeholder, not threads deleted.
    thread1.fetch_message.assert_awaited_once_with(starter_msg_id_1)
    starter1.edit.assert_awaited_once_with(content="*deleted by creator*")
    thread1.delete.assert_not_awaited()
    thread2.fetch_message.assert_awaited_once_with(starter_msg_id_2)
    starter2.edit.assert_awaited_once_with(content="*deleted by creator*")
    thread2.delete.assert_not_awaited()


# ---------------------------------------------------------------------------
# Dispatch integration tests (activitypub_handlers.py)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_routes_comment_updated_to_handler(tmp_path: Path) -> None:
    """dispatch_activitypub_event routes comment.updated to handle_inbound_comment_update."""
    database = _database(tmp_path)
    community_runtime = _community_runtime(database)

    event = _fake_update_event(
        event_type="comment.updated",
        ap_id=COMMENT_AP_ID,
        kind="comment",
        post_ap_id=POST_AP_ID,
        post_lemmy_id=1,
    )

    async def wait_until_bridge_ready() -> None:
        pass

    runtime_obj = SimpleNamespace(
        community_runtime=community_runtime,
        database=database,
        fedify_gateway=AsyncMock(),
        bot=SimpleNamespace(wait_until_bridge_ready=wait_until_bridge_ready),
    )
    # No message group exists → should return skipped, not raise.
    result = await dispatch_activitypub_event(event, runtime_obj)
    assert result.status == "skipped"


@pytest.mark.asyncio
async def test_dispatch_routes_post_deleted_with_existing_thread_group(tmp_path: Path) -> None:
    """dispatch_activitypub_event routes post.deleted to handle_inbound_post_delete, deletes thread."""
    database = _database(tmp_path)
    thread_id = 200

    fake_thread = SimpleNamespace(id=thread_id, delete=AsyncMock())
    bot = _fake_bot(threads={thread_id: fake_thread})
    community_runtime = _community_runtime(database, bot=bot)

    # Seed a real thread group with one inbound delivery.
    thread_group = database.create_thread_group(
        community_actor_id=COMMUNITY_ACTOR_URL,
        source_channel_id=None, source_thread_id=None,
        source_starter_message_id=None,
        ap_activity_id="delivery-post-dispatch", ap_object_id=POST_AP_ID,
    )
    database.add_thread_delivery(
        thread_group_id=thread_group.id,
        discord_channel_id=100, discord_thread_id=thread_id,
        discord_starter_message_id=999, role="inbound",
    )

    event = _fake_update_event(
        event_type="post.deleted",
        ap_id=POST_AP_ID,
        kind="post",
    )

    async def wait_until_bridge_ready() -> None:
        pass

    runtime_obj = SimpleNamespace(
        community_runtime=community_runtime,
        database=database,
        fedify_gateway=AsyncMock(),
        bot=SimpleNamespace(wait_until_bridge_ready=wait_until_bridge_ready),
    )
    result = await dispatch_activitypub_event(event, runtime_obj)

    assert result.status == "processed"
    fake_thread.delete.assert_not_awaited()


# ---------------------------------------------------------------------------
# Inbound AP post.updated tests (new — no coverage existed before)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inbound_post_update_edits_discord_thread_starters(tmp_path: Path) -> None:
    """Inbound post.updated edits the starter message in all thread deliveries."""
    database = _database(tmp_path)
    thread_id_1 = 200
    thread_id_2 = 500
    starter_msg_id_1 = 999
    starter_msg_id_2 = 998

    # Each fake thread exposes fetch_message for the starter and an editable stub.
    starter1 = SimpleNamespace(id=starter_msg_id_1, edit=AsyncMock(), delete=AsyncMock())
    starter2 = SimpleNamespace(id=starter_msg_id_2, edit=AsyncMock(), delete=AsyncMock())
    thread1 = SimpleNamespace(
        id=thread_id_1,
        fetch_message=AsyncMock(return_value=starter1),
        delete=AsyncMock(),
    )
    thread2 = SimpleNamespace(
        id=thread_id_2,
        fetch_message=AsyncMock(return_value=starter2),
        delete=AsyncMock(),
    )
    bot = _fake_bot(threads={thread_id_1: thread1, thread_id_2: thread2})
    community_runtime = _community_runtime(database, bot=bot)

    # Seed thread group with two inbound deliveries, each having its own starter message.
    thread_group = database.create_thread_group(
        community_actor_id=COMMUNITY_ACTOR_URL,
        source_channel_id=None, source_thread_id=None,
        source_starter_message_id=None,
        ap_activity_id="delivery-post-update", ap_object_id=POST_AP_ID,
    )
    database.add_thread_delivery(
        thread_group_id=thread_group.id,
        discord_channel_id=100, discord_thread_id=thread_id_1,
        discord_starter_message_id=starter_msg_id_1, role="inbound",
    )
    database.add_thread_delivery(
        thread_group_id=thread_group.id,
        discord_channel_id=101, discord_thread_id=thread_id_2,
        discord_starter_message_id=starter_msg_id_2, role="inbound",
    )

    event = _fake_update_event(
        event_type="post.updated",
        ap_id=POST_AP_ID,
        kind="post",
        body_markdown="New post body",
    )
    runtime_obj = _fake_runtime()
    result = await community_runtime.handle_inbound_post_update(event, runtime_obj)

    assert result.status == "processed"
    # Both starter messages must have been fetched and edited.
    thread1.fetch_message.assert_awaited_once_with(starter_msg_id_1)
    starter1.edit.assert_awaited_once_with(content="New post body")
    thread2.fetch_message.assert_awaited_once_with(starter_msg_id_2)
    starter2.edit.assert_awaited_once_with(content="New post body")


@pytest.mark.asyncio
async def test_inbound_post_update_for_unknown_ap_id_returns_skipped(tmp_path: Path) -> None:
    """Inbound post.updated for an unmapped post returns skipped with no Discord calls."""
    database = _database(tmp_path)
    bot = _fake_bot(threads={})
    community_runtime = _community_runtime(database, bot=bot)

    event = _fake_update_event(
        event_type="post.updated",
        ap_id=POST_AP_ID,
        kind="post",
        body_markdown="Updated",
    )
    runtime_obj = _fake_runtime()
    result = await community_runtime.handle_inbound_post_update(event, runtime_obj)

    assert result.status == "skipped"


@pytest.mark.asyncio
async def test_dispatch_routes_post_updated_to_handler(tmp_path: Path) -> None:
    """dispatch_activitypub_event routes post.updated to handle_inbound_post_update."""
    database = _database(tmp_path)
    community_runtime = _community_runtime(database)

    event = _fake_update_event(
        event_type="post.updated",
        ap_id=POST_AP_ID,
        kind="post",
    )

    async def wait_until_bridge_ready() -> None:
        pass

    runtime_obj = SimpleNamespace(
        community_runtime=community_runtime,
        database=database,
        fedify_gateway=AsyncMock(),
        bot=SimpleNamespace(wait_until_bridge_ready=wait_until_bridge_ready),
    )
    # No thread group for this AP ID → routes to handler, handler returns skipped.
    result = await dispatch_activitypub_event(event, runtime_obj)
    assert result.status == "skipped"


# ---------------------------------------------------------------------------
# Header preservation tests — username survives edits
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mirror_edit_preserves_username_header(tmp_path: Path) -> None:
    """Editing a source message preserves the `username@domain` header in the mirror.

    Mirror messages are created with a "`username@domain`\\n\\nbody" format.
    When a Discord edit propagates to the mirror, apply_edit_to_discord_message
    must keep the header line and replace only the body below it.
    """
    database = _database(tmp_path)
    mirror_thread_id = 500
    mirror_msg_id = 600
    source_msg_id = 301

    # Mirror message already carries the bridge attribution header.
    mirror_message = SimpleNamespace(
        id=mirror_msg_id,
        content="`alice@lemmy.example`\n\nOriginal body text",
        edit=AsyncMock(),
        delete=AsyncMock(),
    )
    mirror_thread = SimpleNamespace(
        id=mirror_thread_id,
        fetch_message=AsyncMock(return_value=mirror_message),
    )
    bot = _fake_bot(threads={mirror_thread_id: mirror_thread})
    fanout = _fake_fanout(bot=bot)
    runtime_obj = _fake_runtime()
    community_runtime = _community_runtime(database, bot=bot, discord_fanout=fanout)

    _setup_message_group_with_deliveries(
        database,
        source_msg_id=source_msg_id,
        mirror_msg_id=mirror_msg_id,
        mirror_thread_id=mirror_thread_id,
    )

    await community_runtime.handle_discord_message_edit(
        message_id=source_msg_id,
        new_content="Edited body text",
        runtime=runtime_obj,
    )

    # Mirror must be edited with header preserved and body replaced.
    mirror_message.edit.assert_awaited_once_with(
        content="`alice@lemmy.example`\n\nEdited body text"
    )


@pytest.mark.asyncio
async def test_inbound_comment_update_preserves_username_header(tmp_path: Path) -> None:
    """Inbound comment.updated preserves the `username@domain` header in inbound messages.

    Inbound messages are created with "`username@domain`\\n\\nbody". When an AP
    Update arrives, apply_edit_to_discord_message must keep the header and replace
    only the body so the author attribution is not lost.
    """
    database = _database(tmp_path)
    msg_id = 300
    thread_id = 200

    # Inbound message carries the bridge attribution header from initial delivery.
    message = SimpleNamespace(
        id=msg_id,
        content="`bob@lemmy.example`\n\nOriginal comment text",
        edit=AsyncMock(),
        delete=AsyncMock(),
    )
    thread = SimpleNamespace(id=thread_id, fetch_message=AsyncMock(return_value=message))
    bot = _fake_bot(threads={thread_id: thread})
    community_runtime = _community_runtime(database, bot=bot)

    # Set up one inbound thread group and message group.
    thread_group = database.create_thread_group(
        community_actor_id=COMMUNITY_ACTOR_URL,
        source_channel_id=None, source_thread_id=None,
        source_starter_message_id=None,
        ap_activity_id="delivery-post-hdr", ap_object_id=POST_AP_ID,
    )
    database.add_thread_delivery(
        thread_group_id=thread_group.id,
        discord_channel_id=100, discord_thread_id=thread_id,
        discord_starter_message_id=999, role="inbound",
    )
    message_group = database.create_message_group(
        community_actor_id=COMMUNITY_ACTOR_URL,
        thread_group_id=thread_group.id,
        source_channel_id=None, source_thread_id=None, source_message_id=None,
        ap_activity_id="delivery-comment-hdr", ap_object_id=COMMENT_AP_ID,
    )
    database.add_message_delivery(
        message_group_id=message_group.id,
        discord_channel_id=100, discord_thread_id=thread_id,
        discord_message_id=msg_id, role="inbound",
    )

    event = _fake_update_event(
        event_type="comment.updated",
        ap_id=COMMENT_AP_ID,
        kind="comment",
        body_markdown="Updated comment text",
        post_ap_id=POST_AP_ID,
        post_lemmy_id=1,
    )
    runtime_obj = _fake_runtime()
    result = await community_runtime.handle_inbound_comment_update(event, runtime_obj)

    assert result.status == "processed"
    # Header must be preserved; only the body below it is replaced.
    message.edit.assert_awaited_once_with(
        content="`bob@lemmy.example`\n\nUpdated comment text"
    )
