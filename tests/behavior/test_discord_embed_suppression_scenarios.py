"""Remote-content Discord delivery keeps URLs but suppresses preview cards."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.bridge_lemmy_to_discord import (
    _create_inbound_discord_thread,
    _send_inbound_comment,
)


@pytest.mark.asyncio
async def test_lemmy_post_and_reply_keep_links_without_embeds() -> None:
    """Inbound Lemmy content creates a forum starter and reply without link cards.

    State: an incoming post and reply contain public URLs. Action: deliver both
    through the real Discord-facing boundary. Result: both operations preserve
    their content and request embed suppression at the SDK boundary.
    """
    thread = SimpleNamespace(id=200)
    starter = SimpleNamespace(id=300)
    forum = SimpleNamespace(create_thread=AsyncMock(return_value=(thread, starter)))
    post = SimpleNamespace(
        actor_id="https://lemmy.example/u/alice",
        object=SimpleNamespace(
            author_name="alice", title="Links", body_markdown="https://example.org/post",
            url="https://lemmy.example/post/1", ap_id="https://lemmy.example/post/1",
        ),
    )
    thread_id, starter_id = await _create_inbound_discord_thread(forum_channel=forum, event=post)
    assert (thread_id, starter_id) == (200, 300)
    assert forum.create_thread.await_args.kwargs["suppress_embeds"] is True
    assert "https://example.org/post" in forum.create_thread.await_args.kwargs["content"]

    thread.send = AsyncMock(return_value=SimpleNamespace(id=400))
    reply = SimpleNamespace(
        actor_id=post.actor_id,
        object=SimpleNamespace(
            author_name="alice", body_markdown="https://example.org/reply",
            url="https://lemmy.example/comment/2", ap_id="https://lemmy.example/comment/2",
        ),
    )
    sent = await _send_inbound_comment(thread=thread, event=reply, reference=None)
    assert sent.id == 400
    assert thread.send.await_args.kwargs["suppress_embeds"] is True
    assert "https://example.org/reply" in thread.send.await_args.args[0]
