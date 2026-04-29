from __future__ import annotations

import logging
from typing import Any

import discord

from .db import Database
from .formatting import format_lemmy_comment_for_discord, format_lemmy_post_for_discord, format_thread_title_for_discord, normalize_text

logger = logging.getLogger(__name__)


def _get_creator_name(item: dict[str, Any]) -> str:
    creator = item.get("creator") or {}
    return creator.get("name") or creator.get("display_name") or "unknown"


def _get_post_data(item: dict[str, Any]) -> dict[str, Any]:
    return item.get("post") or {}


def _get_comment_data(item: dict[str, Any]) -> dict[str, Any]:
    return item.get("comment") or {}


async def create_discord_thread_for_lemmy_post(
    *,
    database: Database,
    forum_channel: discord.ForumChannel,
    post_item: dict[str, Any],
    lemmy_base_url: str,
) -> int:
    post = _get_post_data(post_item)
    creator_name = _get_creator_name(post_item)
    post_id = int(post["id"])
    title = format_thread_title_for_discord(post.get("name") or "Untitled Lemmy Post")
    url = f"{lemmy_base_url}/post/{post_id}"
    body = format_lemmy_post_for_discord(
        creator_name,
        post.get("name") or "Untitled Lemmy Post",
        normalize_text(post.get("body")),
        url,
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
        lemmy_post_id=post_id,
        discord_forum_thread_id=thread.id,
        discord_starter_message_id=message.id,
        direction="lemmy_to_discord",
    )
    logger.info("Created Discord forum thread %s from Lemmy post %s", thread.id, post_id)
    return thread.id


async def create_discord_message_for_lemmy_comment(
    *,
    database: Database,
    thread: discord.Thread,
    comment_item: dict[str, Any],
    lemmy_base_url: str,
) -> int:
    comment = _get_comment_data(comment_item)
    creator_name = _get_creator_name(comment_item)
    comment_id = int(comment["id"])
    post_id = int(comment["post_id"])
    url = f"{lemmy_base_url}/comment/{comment_id}"
    body = format_lemmy_comment_for_discord(creator_name, normalize_text(comment.get("content")), url)
    message = await thread.send(body)

    database.create_comment_link(
        lemmy_comment_id=comment_id,
        lemmy_post_id=post_id,
        discord_forum_thread_id=thread.id,
        discord_message_id=message.id,
        direction="lemmy_to_discord",
    )
    logger.info("Created Discord message %s from Lemmy comment %s", message.id, comment_id)
    return message.id
