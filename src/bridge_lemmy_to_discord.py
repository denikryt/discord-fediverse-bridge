from __future__ import annotations

import logging

import discord

from .activitypub_models import ActivityPubEvent
from .db import Database
from .formatting import format_lemmy_comment_for_discord, format_lemmy_post_for_discord, format_thread_title_for_discord, normalize_text

logger = logging.getLogger(__name__)


async def _create_inbound_discord_thread(
    *,
    forum_channel: discord.ForumChannel,
    event: ActivityPubEvent,
) -> tuple[int, int]:
    """Create one Discord forum-thread for an inbound AP post.

    Returns (thread_id, starter_message_id). No DB writes — the caller
    (CommunityRuntime.handle_inbound_post) persists the delivery rows.
    """
    post = event.object
    title = format_thread_title_for_discord(post.title or "Untitled Lemmy Post")
    body = format_lemmy_post_for_discord(
        post.author_name,
        post.title or "Untitled Lemmy Post",
        normalize_text(post.body_markdown),
        post.url,
    )

    result = await forum_channel.create_thread(name=title, content=body)
    thread = getattr(result, "thread", None)
    message = getattr(result, "message", None)
    if thread is None and isinstance(result, tuple):
        thread = result[0]
    if message is None and isinstance(result, tuple):
        message = result[1]
    if thread is None:
        raise RuntimeError("Discord create_thread did not return a thread object")
    if message is None:
        # Some discord.py variants do not return the starter message, so recover
        # it immediately before returning — caller needs the starter_message_id.
        try:
            message = await thread.fetch_message(thread.id)
        except discord.HTTPException as exc:
            raise RuntimeError("Discord create_thread did not return a starter message") from exc

    logger.info("Created Discord forum thread %s from ActivityPub post %s", thread.id, post.ap_id)
    return thread.id, message.id


async def _send_inbound_comment(
    *,
    thread: discord.Thread,
    event: ActivityPubEvent,
    reference: discord.MessageReference | None,
) -> discord.Message:
    """Send one Discord message for an inbound AP comment.

    Returns the sent Discord message. No DB writes — the caller
    (CommunityRuntime.handle_inbound_comment) persists the delivery row.
    The reference is already resolved by the caller via _resolve_inbound_reference.
    """
    comment = event.object
    body = format_lemmy_comment_for_discord(
        comment.author_name,
        normalize_text(comment.body_markdown),
        comment.url,
    )
    message = await thread.send(body, reference=reference)
    logger.info("Created Discord message %s from ActivityPub comment %s", message.id, comment.ap_id)
    return message
