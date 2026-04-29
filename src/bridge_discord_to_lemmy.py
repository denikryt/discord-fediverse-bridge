from __future__ import annotations

import logging

import discord

from .db import Database
from .formatting import format_discord_body_for_lemmy, format_thread_title_for_discord
from .lemmy_client import LemmyClient

logger = logging.getLogger(__name__)


async def sync_forum_thread_to_lemmy(
    *,
    database: Database,
    lemmy: LemmyClient,
    community_id: int,
    bridge_prefix: str,
    thread: discord.Thread,
    starter_message: discord.Message,
) -> int:
    author_name = starter_message.author.display_name or starter_message.author.name
    title = format_thread_title_for_discord(thread.name)
    body = format_discord_body_for_lemmy(author_name, starter_message.content, bridge_prefix)

    payload = await lemmy.create_post(community_id=community_id, name=title, body=body)
    post_id = int(payload["post_view"]["post"]["id"])

    database.create_post_link(
        lemmy_post_id=post_id,
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

    author_name = message.author.display_name or message.author.name
    body = format_discord_body_for_lemmy(author_name, message.content, bridge_prefix)
    payload = await lemmy.create_comment(post_id=post_link.lemmy_post_id, content=body)
    comment_id = int(payload["comment_view"]["comment"]["id"])

    database.create_comment_link(
        lemmy_comment_id=comment_id,
        lemmy_post_id=post_link.lemmy_post_id,
        discord_forum_thread_id=message.channel.id,
        discord_message_id=message.id,
        direction="discord_to_lemmy",
    )
    logger.info("Created Lemmy comment %s from Discord message %s", comment_id, message.id)
    return comment_id
