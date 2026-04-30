from __future__ import annotations

import logging

import discord

from .activitypub_models import ActivityPubEvent
from .db import Database
from .formatting import format_lemmy_comment_for_discord, format_lemmy_post_for_discord, format_thread_title_for_discord, normalize_text

logger = logging.getLogger(__name__)


async def create_discord_thread_for_activitypub_post(
    *,
    database: Database,
    forum_channel: discord.ForumChannel,
    event: ActivityPubEvent,
) -> int:
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
        try:
            message = await thread.fetch_message(thread.id)
        except discord.HTTPException as exc:
            raise RuntimeError("Discord create_thread did not return a starter message") from exc

    database.create_post_link(
        lemmy_post_id=post.lemmy_id,
        lemmy_post_ap_id=post.ap_id,
        discord_forum_thread_id=thread.id,
        discord_starter_message_id=message.id,
        direction="lemmy_to_discord",
    )
    logger.info("Created Discord forum thread %s from ActivityPub post %s", thread.id, post.ap_id)
    return thread.id


async def create_discord_message_for_activitypub_comment(
    *,
    database: Database,
    thread: discord.Thread,
    event: ActivityPubEvent,
) -> int:
    comment = event.object
    body = format_lemmy_comment_for_discord(comment.author_name, normalize_text(comment.body_markdown), comment.url)
    message = await thread.send(body)

    database.create_comment_link(
        lemmy_comment_id=comment.lemmy_id,
        lemmy_comment_ap_id=comment.ap_id,
        lemmy_parent_comment_ap_id=comment.parent_ap_id,
        lemmy_post_id=comment.post_lemmy_id or 0,
        discord_forum_thread_id=thread.id,
        discord_message_id=message.id,
        direction="lemmy_to_discord",
    )
    logger.info("Created Discord message %s from ActivityPub comment %s", message.id, comment.ap_id)
    return message.id
