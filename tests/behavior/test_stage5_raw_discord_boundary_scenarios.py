"""Stage 5 behavior at the raw Discord mutation boundary."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.discord_bot import BridgeBot
from support.db import add_registered_user, build_database


def _published(database):
    user = database.users.get_user_by_discord_user_id("123")
    assert user is not None
    database.activitypub_objects.create_published_activity_object(
        actor_username=user.activitypub_username,
        actor_url=user.actor_url,
        community_actor_url="https://remote.example/c/group",
        activity_id="https://bridge.example/users/alice/activities/create/1",
        object_id="https://bridge.example/users/alice/post/1",
        kind="post",
        title="Topic",
        body_markdown="old",
        in_reply_to_object_id=None,
        discord_channel_id=10,
        discord_message_id=11,
    )


@pytest.mark.asyncio
async def test_partial_raw_update_fetch_failure_is_durable_before_handler_returns(
    tmp_path, monkeypatch
) -> None:
    """The actual raw callback records reconciliation work when Discord fetch fails."""
    database = build_database(tmp_path, "raw-edit.db")
    add_registered_user(database)
    _published(database)

    class FakeHTTPException(Exception):
        pass

    monkeypatch.setattr("src.discord_bot.discord.HTTPException", FakeHTTPException)
    bot = object.__new__(BridgeBot)
    bot.database = database
    bot._recent_edits = {}
    bot.event_router = SimpleNamespace(
        is_local_community_message=lambda _message_id: False,
    )
    bot.get_channel = lambda _channel_id: SimpleNamespace(
        fetch_message=AsyncMock(side_effect=FakeHTTPException("temporary"))
    )
    bot.fetch_channel = AsyncMock()
    wake = SimpleNamespace(wake=__import__("unittest.mock").mock.Mock())
    bot._runtime = SimpleNamespace(user_actor_delivery_worker=wake)

    payload = SimpleNamespace(
        message_id=11,
        channel_id=10,
        guild_id=1,
        data={"content": "new", "author": {"username": "alice"}},
    )
    # Remote-community source-role evidence is required before the bot admits it.
    group = database.discord_fanout_groups.create_thread_group(
        community_actor_id="https://remote.example/c/group",
        source_channel_id=10,
        source_thread_id=10,
        source_starter_message_id=99,
        ap_activity_id="https://bridge.example/users/alice/activities/create/root",
        ap_object_id="https://bridge.example/users/alice/post/root",
    )
    message_group = database.discord_fanout_groups.create_message_group(
        community_actor_id="https://remote.example/c/group",
        thread_group_id=group.id,
        source_channel_id=10,
        source_thread_id=10,
        source_message_id=11,
        ap_activity_id="https://bridge.example/users/alice/activities/create/1",
        ap_object_id="https://bridge.example/users/alice/post/1",
    )
    database.discord_fanout_groups.add_message_delivery(
        message_group_id=message_group.id,
        discord_channel_id=10,
        discord_thread_id=10,
        discord_message_id=11,
        role="source",
    )

    await BridgeBot.on_raw_message_edit(bot, payload)

    published = database.activitypub_objects.get_published_activity_object_by_object_id(
        "https://bridge.example/users/alice/post/1"
    )
    assert published.discord_reconciliation_needed is True
    assert published.discord_reconciliation_attempt_count == 0
    wake.wake.assert_called_once()


@pytest.mark.asyncio
async def test_raw_delete_has_no_timestamp_dependency_and_routes_terminal_intent(tmp_path) -> None:
    """The actual raw delete callback forwards a timestamp-less terminal mutation."""
    database = build_database(tmp_path, "raw-delete.db")
    add_registered_user(database)
    _published(database)

    router = SimpleNamespace(
        is_local_community_message=lambda _message_id: True,
        handle_message_delete=AsyncMock(),
    )
    bot = object.__new__(BridgeBot)
    bot.database = database
    bot.event_router = router
    bot._recent_deletes = {}
    bot._runtime = SimpleNamespace()
    payload = SimpleNamespace(message_id=11, channel_id=10, guild_id=1)

    await BridgeBot.on_raw_message_delete(bot, payload)

    router.handle_message_delete.assert_awaited_once_with(
        message_id=11, runtime=bot._runtime, guild_id=1
    )
