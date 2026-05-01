from __future__ import annotations

import asyncio
import logging

import discord

from .bridge_discord_to_lemmy import sync_forum_thread_to_lemmy, sync_thread_message_to_lemmy_comment
from .config import Settings
from .db import Database
from .lemmy_client import LemmyClient

logger = logging.getLogger(__name__)


class BridgeBot(discord.Client):
    # BridgeBot owns the Discord-side event loop and forwards only the forum
    # channel/thread activity that belongs to this bridge instance.
    def __init__(
        self,
        *,
        settings: Settings,
        database: Database,
        lemmy: LemmyClient,
        lemmy_community_id: int,
    ) -> None:
        intents = discord.Intents.default()
        intents.guilds = True
        intents.messages = True
        intents.message_content = True
        super().__init__(intents=intents)
        self.settings = settings
        self.database = database
        self.lemmy = lemmy
        self.lemmy_community_id = lemmy_community_id
        self.forum_channel: discord.ForumChannel | None = None
        self.bridge_ready = asyncio.Event()

    async def on_ready(self) -> None:
        # Resolve the configured forum channel once and treat its presence as
        # the signal that inbound ActivityPub events may start using Discord.
        channel = self.get_channel(self.settings.discord_forum_channel_id)
        if channel is None:
            channel = await self.fetch_channel(self.settings.discord_forum_channel_id)
        if not isinstance(channel, discord.ForumChannel):
            raise RuntimeError(f"Configured channel {self.settings.discord_forum_channel_id} is not a Discord forum channel")

        self.forum_channel = channel
        self.bridge_ready.set()
        logger.info("Bridge bot is ready as %s", self.user)

    async def close(self) -> None:
        await self.lemmy.close()
        await super().close()

    async def on_thread_create(self, thread: discord.Thread) -> None:
        # Only user-created forum threads should fan out into Lemmy posts.
        if self.forum_channel is None:
            return
        if thread.parent_id != self.forum_channel.id:
            return
        if self.user and thread.owner_id == self.user.id:
            return
        if self.database.get_post_link_by_thread_id(thread.id) is not None:
            return

        starter_message = await self._fetch_starter_message(thread)
        if starter_message is None:
            logger.warning("Could not fetch starter message for Discord forum thread %s", thread.id)
            return
        if starter_message.author.bot:
            return

        await sync_forum_thread_to_lemmy(
            database=self.database,
            lemmy=self.lemmy,
            community_id=self.lemmy_community_id,
            bridge_prefix=self.settings.bridge_display_prefix,
            thread=thread,
            starter_message=starter_message,
        )

    async def on_message(self, message: discord.Message) -> None:
        # Only plain user messages inside mapped forum threads are candidates
        # for Lemmy comment creation.
        if self.user and message.author.id == self.user.id:
            return
        if message.author.bot:
            return
        if not isinstance(message.channel, discord.Thread):
            return
        if self.forum_channel is None or message.channel.parent_id != self.forum_channel.id:
            return

        await sync_thread_message_to_lemmy_comment(
            database=self.database,
            lemmy=self.lemmy,
            bridge_prefix=self.settings.bridge_display_prefix,
            message=message,
        )

    async def _fetch_starter_message(self, thread: discord.Thread) -> discord.Message | None:
        # Discord APIs are inconsistent here, so we try the direct starter
        # message path first and then fall back to oldest history.
        if thread.starter_message is not None:
            return thread.starter_message
        try:
            return await thread.fetch_message(thread.id)
        except discord.NotFound:
            try:
                history = [message async for message in thread.history(limit=1, oldest_first=True)]
                return history[0] if history else None
            except discord.HTTPException:
                return None
        except discord.HTTPException:
            logger.exception("Failed to fetch starter message for thread %s", thread.id)
            return None

    async def wait_until_bridge_ready(self) -> None:
        await self.bridge_ready.wait()

    def require_forum_channel(self) -> discord.ForumChannel:
        if self.forum_channel is None:
            raise RuntimeError("Discord forum channel is not initialized yet")
        return self.forum_channel

    async def get_thread_by_id(self, thread_id: int) -> discord.Thread:
        cached = self.get_channel(thread_id)
        if isinstance(cached, discord.Thread):
            return cached
        channel = await self.fetch_channel(thread_id)
        if not isinstance(channel, discord.Thread):
            raise RuntimeError(f"Mapped Discord thread {thread_id} was not found")
        return channel
