"""Discord-side fake builders used by scenario tests.

The builders are intentionally explicit because the current suite relies on two
different `create_thread()` return shapes and several distinct bot surfaces.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock


def build_thread(*, thread_id: int = 200, channel_id: int = 100, name: str = "Thread title") -> SimpleNamespace:
    """Build a fake Discord forum thread with the minimal shape used by tests."""
    return SimpleNamespace(id=thread_id, parent_id=channel_id, name=name)


def build_starter_message(
    *,
    message_id: int = 300,
    author_id: int = 123,
    content: str = "hello from discord",
    display_name: str = "Alice",
    name: str = "alice",
) -> SimpleNamespace:
    """Build a fake thread starter message."""
    return SimpleNamespace(
        id=message_id,
        content=content,
        author=SimpleNamespace(id=author_id, display_name=display_name, name=name),
        reply=AsyncMock(),
    )


def build_thread_message(
    *,
    message_id: int = 301,
    thread_id: int = 200,
    channel_id: int = 100,
    author_id: int = 123,
    content: str = "hello comment",
    display_name: str = "Alice",
    name: str = "alice",
    reference_message_id: int | None = None,
) -> SimpleNamespace:
    """Build a fake Discord thread message with an optional reply reference."""
    reference = (
        SimpleNamespace(message_id=reference_message_id)
        if reference_message_id is not None
        else None
    )
    return SimpleNamespace(
        id=message_id,
        content=content,
        author=SimpleNamespace(id=author_id, display_name=display_name, name=name),
        channel=SimpleNamespace(id=thread_id, parent_id=channel_id),
        reference=reference,
        reply=AsyncMock(),
    )


def build_forum_channel_tuple_result(
    *,
    channel_id: int,
    thread_id: int,
    starter_message_id: int,
) -> SimpleNamespace:
    """Build a fake forum channel whose `create_thread()` returns a tuple.

    Inbound routing tests use this shape because the lower-level helper returns
    `(thread, message)`.
    """
    fake_thread = SimpleNamespace(id=thread_id)
    fake_message = SimpleNamespace(id=starter_message_id)
    return SimpleNamespace(
        id=channel_id,
        create_thread=AsyncMock(return_value=(fake_thread, fake_message)),
    )


def build_forum_channel_object_result(
    *,
    channel_id: int,
    thread_id: int,
    starter_message_id: int,
) -> SimpleNamespace:
    """Build a fake forum channel whose `create_thread()` returns an object.

    Fanout tests use the object shape with `.thread` and `.message`.
    """
    return SimpleNamespace(
        id=channel_id,
        create_thread=AsyncMock(
            return_value=SimpleNamespace(
                thread=SimpleNamespace(id=thread_id),
                message=SimpleNamespace(id=starter_message_id),
            )
        ),
    )


def build_send_thread(*, thread_id: int, sent_message_id: int) -> SimpleNamespace:
    """Build a fake thread whose `send()` returns one fake message."""
    return SimpleNamespace(
        id=thread_id,
        send=AsyncMock(return_value=SimpleNamespace(id=sent_message_id)),
    )


def build_bot(
    *,
    forum_channels: dict[int, object] | None = None,
    threads: dict[int, object] | None = None,
    users: dict[int | str, object] | None = None,
) -> SimpleNamespace:
    """Build a fake bot with opt-in async lookup methods.

    The current test suite does not use one universal bot shape, so the helper
    exposes only the methods the scenarios need.
    """

    async def fetch_forum_channel(channel_id: int) -> object:
        if forum_channels and channel_id in forum_channels:
            return forum_channels[channel_id]
        raise RuntimeError(f"No fake forum channel for id {channel_id}")

    async def get_thread_by_id(thread_id: int) -> object:
        if threads and thread_id in threads:
            return threads[thread_id]
        raise RuntimeError(f"No fake thread for id {thread_id}")

    async def fetch_user(user_id: int | str) -> object:
        if users and user_id in users:
            return users[user_id]
        raise RuntimeError(f"No fake user for id {user_id}")

    async def wait_until_bridge_ready() -> None:
        return None

    return SimpleNamespace(
        fetch_forum_channel=fetch_forum_channel,
        get_thread_by_id=get_thread_by_id,
        fetch_user=fetch_user,
        wait_until_bridge_ready=wait_until_bridge_ready,
    )


def build_dm_user() -> SimpleNamespace:
    """Build a fake Discord user with an async DM boundary."""
    return SimpleNamespace(send=AsyncMock())

