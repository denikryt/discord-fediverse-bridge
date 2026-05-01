from __future__ import annotations

import logging
from dataclasses import dataclass

import discord

from .db import Database
from .formatting import format_discord_body_for_lemmy, format_thread_title_for_discord
from .lemmy_client import LemmyClient
from .models import PostLink

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ResolvedCommentTarget:
    # Discord replies can either map to a concrete Lemmy parent comment or
    # degrade safely to a top-level comment under the thread's mapped post.
    lemmy_post_id: int
    lemmy_parent_comment_id: int | None
    lemmy_parent_comment_ap_id: str | None


async def sync_forum_thread_to_lemmy(
    *,
    database: Database,
    lemmy: LemmyClient,
    community_id: int,
    bridge_prefix: str,
    thread: discord.Thread,
    starter_message: discord.Message,
) -> int:
    # The first user message in a forum thread becomes the Lemmy post body so
    # thread creation and post creation stay aligned.
    author_name = starter_message.author.display_name or starter_message.author.name
    title = format_thread_title_for_discord(thread.name)
    body = format_discord_body_for_lemmy(author_name, starter_message.content, bridge_prefix)

    payload = await lemmy.create_post(community_id=community_id, name=title, body=body)
    post_id = int(payload["post_view"]["post"]["id"])
    post_ap_id = payload["post_view"]["post"].get("ap_id")

    database.create_post_link(
        lemmy_post_id=post_id,
        lemmy_post_ap_id=post_ap_id,
        discord_forum_thread_id=thread.id,
        discord_starter_message_id=starter_message.id,
        direction="discord_to_lemmy",
    )
    logger.info("Created Lemmy post %s from Discord forum thread %s", post_id, thread.id)
    return post_id


async def sync_thread_message_to_lemmy_comment(
    *,
    database: Database,
    lemmy: LemmyClient,
    bridge_prefix: str,
    message: discord.Message,
) -> int | None:
    # Message sync is guarded by local mappings so we never create orphaned or
    # duplicate Lemmy comments.
    if not isinstance(message.channel, discord.Thread):
        return None

    post_link = database.get_post_link_by_thread_id(message.channel.id)
    if post_link is None:
        logger.debug("Ignoring Discord message %s because thread is not mapped", message.id)
        return None

    if post_link.discord_starter_message_id == message.id:
        logger.debug("Ignoring Discord starter message %s because thread creation already handled it", message.id)
        return None

    if database.has_comment_link_for_discord_message(message.id):
        logger.debug("Ignoring Discord message %s because it was already synced", message.id)
        return None
    if post_link.lemmy_post_id is None:
        logger.warning("Cannot create Lemmy comment for Discord message %s because mapped post has no numeric Lemmy ID", message.id)
        return None

    target = resolve_comment_target(
        database=database,
        post_link=post_link,
        message=message,
    )
    author_name = message.author.display_name or message.author.name
    body = format_discord_body_for_lemmy(author_name, message.content, bridge_prefix)
    payload = await lemmy.create_comment(
        post_id=target.lemmy_post_id,
        content=body,
        parent_id=target.lemmy_parent_comment_id,
    )
    comment_id = int(payload["comment_view"]["comment"]["id"])
    comment_ap_id = payload["comment_view"]["comment"].get("ap_id")

    database.create_comment_link(
        lemmy_comment_id=comment_id,
        lemmy_comment_ap_id=comment_ap_id,
        lemmy_parent_comment_ap_id=target.lemmy_parent_comment_ap_id,
        lemmy_post_id=target.lemmy_post_id,
        discord_forum_thread_id=message.channel.id,
        discord_message_id=message.id,
        direction="discord_to_lemmy",
    )
    logger.info("Created Lemmy comment %s from Discord message %s", comment_id, message.id)
    return comment_id


def resolve_comment_target(
    *,
    database: Database,
    post_link: PostLink,
    message: discord.Message,
) -> ResolvedCommentTarget:
    # Parent lookup uses existing local mappings only; if the reply target is
    # unknown, the bridge still creates a valid top-level Lemmy comment.
    lemmy_post_id = post_link.lemmy_post_id
    starter_message_id = post_link.discord_starter_message_id
    if lemmy_post_id is None:
        raise RuntimeError(f"Mapped post for thread {message.channel.id} has no Lemmy post id")

    reference = message.reference
    reference_message_id = reference.message_id if reference is not None else None
    if reference_message_id is None:
        return ResolvedCommentTarget(
            lemmy_post_id=lemmy_post_id,
            lemmy_parent_comment_id=None,
            lemmy_parent_comment_ap_id=None,
        )

    if reference_message_id == starter_message_id:
        logger.debug(
            "Discord message %s replies to starter message %s, using top-level Lemmy comment",
            message.id,
            reference_message_id,
        )
        return ResolvedCommentTarget(
            lemmy_post_id=lemmy_post_id,
            lemmy_parent_comment_id=None,
            lemmy_parent_comment_ap_id=None,
        )

    parent_link = database.get_comment_link_by_discord_message_id(reference_message_id)
    if parent_link is None:
        logger.debug(
            "Discord message %s references unknown parent message %s, using top-level Lemmy comment",
            message.id,
            reference_message_id,
        )
        return ResolvedCommentTarget(
            lemmy_post_id=lemmy_post_id,
            lemmy_parent_comment_id=None,
            lemmy_parent_comment_ap_id=None,
        )

    if parent_link.discord_forum_thread_id != message.channel.id:
        logger.debug(
            "Discord message %s references parent message %s from another thread, using top-level Lemmy comment",
            message.id,
            reference_message_id,
        )
        return ResolvedCommentTarget(
            lemmy_post_id=lemmy_post_id,
            lemmy_parent_comment_id=None,
            lemmy_parent_comment_ap_id=None,
        )

    return ResolvedCommentTarget(
        lemmy_post_id=lemmy_post_id,
        lemmy_parent_comment_id=parent_link.lemmy_comment_id,
        lemmy_parent_comment_ap_id=parent_link.lemmy_comment_ap_id,
    )
