from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

import discord

from .bridge_lemmy_to_discord import create_discord_message_for_lemmy_comment, create_discord_thread_for_lemmy_post
from .db import Database
from .lemmy_client import LemmyClient

logger = logging.getLogger(__name__)

POST_CHECKPOINT_KEY = "lemmy_last_post_published_at"
COMMENT_CHECKPOINT_KEY = "lemmy_last_comment_published_at"


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _get_post_published_at(item: dict[str, Any]) -> str:
    post = item.get("post", {})
    return post.get("published_at") or post.get("published") or ""


def _get_comment_published_at(item: dict[str, Any]) -> str:
    comment = item.get("comment", {})
    return comment.get("published_at") or comment.get("published") or ""


def _get_post_id(item: dict[str, Any]) -> int:
    return int(item["post"]["id"])


def _get_comment_id(item: dict[str, Any]) -> int:
    return int(item["comment"]["id"])


def _get_comment_post_id(item: dict[str, Any]) -> int:
    return int(item["comment"]["post_id"])


def _get_comment_community_id(item: dict[str, Any]) -> int | None:
    post = item.get("post") or {}
    community_id = post.get("community_id")
    return int(community_id) if community_id is not None else None


def _get_creator_id(item: dict[str, Any]) -> int | None:
    creator = item.get("creator") or {}
    creator_id = creator.get("id")
    return int(creator_id) if creator_id is not None else None


class LemmyPoller:
    def __init__(
        self,
        *,
        database: Database,
        lemmy: LemmyClient,
        forum_channel: discord.ForumChannel,
        lemmy_base_url: str,
        community_id: int,
        community_name: str,
        poll_interval_seconds: int,
    ) -> None:
        self.database = database
        self.lemmy = lemmy
        self.forum_channel = forum_channel
        self.lemmy_base_url = lemmy_base_url
        self.community_id = community_id
        self.community_name = community_name
        self.poll_interval_seconds = poll_interval_seconds
        self._task: asyncio.Task[None] | None = None
        self._stopped = asyncio.Event()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self.run(), name="lemmy-poller")

    async def stop(self) -> None:
        self._stopped.set()
        if self._task is not None:
            await self._task

    async def run(self) -> None:
        while not self._stopped.is_set():
            try:
                await self.sync_once()
            except Exception:
                logger.exception("Lemmy polling cycle failed")
            if self._stopped.is_set():
                break
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=self.poll_interval_seconds)
            except asyncio.TimeoutError:
                continue

    async def sync_once(self) -> None:
        await self.sync_posts()
        await self.sync_comments()

    async def sync_posts(self) -> None:
        checkpoint = _parse_ts(self.database.get_sync_state(POST_CHECKPOINT_KEY))
        items = await self.lemmy.list_posts(
            community_id=self.community_id,
            community_name=self.community_name,
        )
        items.sort(key=_get_post_published_at)

        newest_seen = checkpoint
        for item in items:
            published_at = _parse_ts(_get_post_published_at(item))
            if published_at is None:
                continue
            if checkpoint is not None and published_at <= checkpoint:
                continue
            if self.lemmy.person_id is not None and _get_creator_id(item) == self.lemmy.person_id:
                newest_seen = published_at
                continue
            post_id = _get_post_id(item)
            if self.database.get_post_link_by_lemmy_post_id(post_id) is None:
                await create_discord_thread_for_lemmy_post(
                    database=self.database,
                    forum_channel=self.forum_channel,
                    post_item=item,
                    lemmy_base_url=self.lemmy_base_url,
                )
            newest_seen = published_at

        if newest_seen is not None:
            self.database.set_sync_state(POST_CHECKPOINT_KEY, newest_seen.isoformat())

    async def sync_comments(self) -> None:
        checkpoint = _parse_ts(self.database.get_sync_state(COMMENT_CHECKPOINT_KEY))
        items = await self.lemmy.list_comments(limit=50)
        items.sort(key=_get_comment_published_at)

        newest_seen = checkpoint
        for item in items:
            published_at = _parse_ts(_get_comment_published_at(item))
            if published_at is None:
                continue
            if checkpoint is not None and published_at <= checkpoint:
                continue
            community_id = _get_comment_community_id(item)
            if community_id is None or community_id != self.community_id:
                newest_seen = published_at
                continue
            if self.lemmy.person_id is not None and _get_creator_id(item) == self.lemmy.person_id:
                newest_seen = published_at
                continue
            comment_id = _get_comment_id(item)
            if self.database.has_comment_link_for_lemmy_comment(comment_id):
                newest_seen = published_at
                continue
            post_link = self.database.get_post_link_by_lemmy_post_id(_get_comment_post_id(item))
            if post_link is None:
                logger.debug("Skipping Lemmy comment %s because parent post is not mapped yet", comment_id)
                newest_seen = published_at
                continue
            channel = self.forum_channel.guild.get_thread(post_link.discord_forum_thread_id)
            thread = channel
            if thread is None:
                thread = await self.forum_channel.guild.fetch_channel(post_link.discord_forum_thread_id)
            if not isinstance(thread, discord.Thread):
                logger.warning("Mapped Discord thread %s was not found for Lemmy comment %s", post_link.discord_forum_thread_id, comment_id)
                newest_seen = published_at
                continue
            await create_discord_message_for_lemmy_comment(
                database=self.database,
                thread=thread,
                comment_item=item,
                lemmy_base_url=self.lemmy_base_url,
            )
            newest_seen = published_at

        if newest_seen is not None:
            self.database.set_sync_state(COMMENT_CHECKPOINT_KEY, newest_seen.isoformat())
