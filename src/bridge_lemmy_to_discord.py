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
    # Posts become forum threads plus a starter message, and both IDs are
    # persisted because later comment sync needs the thread mapping.
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
        # Some discord.py variants do not hand back the starter message, so we
        # recover it immediately before persisting the mapping.
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
    # Comments are appended to an existing mapped Discord thread and use a
    # Discord reply when the Lemmy parent comment is already mapped locally.
    comment = event.object
    body = format_lemmy_comment_for_discord(comment.author_name, normalize_text(comment.body_markdown), comment.url)
    parent_message = await resolve_parent_discord_message(
        database=database,
        thread=thread,
        parent_comment_ap_id=comment.parent_ap_id,
    )
    message = await thread.send(body, reference=parent_message)

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


async def resolve_parent_discord_message(
    *,
    database: Database,
    thread: discord.Thread,
    parent_comment_ap_id: str | None,
) -> discord.Message | None:
    # Reply threading is best-effort: if the parent comment is unknown or the
    # mapped Discord message can no longer be fetched, fall back to a plain
    # thread message instead of dropping the comment.
    if not parent_comment_ap_id:
        return None

    parent_link = database.get_comment_link_by_lemmy_comment_ap_id(parent_comment_ap_id)
    if parent_link is None:
        logger.debug(
            "No Discord parent mapping found for Lemmy parent comment %s",
            parent_comment_ap_id,
        )
        return None

    if parent_link.discord_forum_thread_id != thread.id:
        logger.debug(
            "Mapped parent comment %s belongs to another Discord thread, skipping reply linkage",
            parent_comment_ap_id,
        )
        return None

    try:
        return await thread.fetch_message(parent_link.discord_message_id)
    except discord.HTTPException:
        logger.warning(
            "Failed to fetch Discord parent message %s for Lemmy parent comment %s",
            parent_link.discord_message_id,
            parent_comment_ap_id,
        )
        return None
