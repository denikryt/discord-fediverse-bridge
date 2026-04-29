from __future__ import annotations

import logging

import discord

from .bridge_discord_to_lemmy import sync_forum_thread_to_lemmy, sync_thread_message_to_lemmy_comment
from .config import Settings
from .db import Database
from .lemmy_client import LemmyClient
from .poller import LemmyPoller

logger = logging.getLogger(__name__)


class BridgeBot(discord.Client):
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
        self.poller: LemmyPoller | None = None

    async def on_ready(self) -> None:
        channel = self.get_channel(self.settings.discord_forum_channel_id)
        if channel is None:
            channel = await self.fetch_channel(self.settings.discord_forum_channel_id)
        if not isinstance(channel, discord.ForumChannel):
            raise RuntimeError(f"Configured channel {self.settings.discord_forum_channel_id} is not a Discord forum channel")

        self.forum_channel = channel
        if self.poller is None:
            self.poller = LemmyPoller(
                database=self.database,
                lemmy=self.lemmy,
                forum_channel=channel,
                lemmy_base_url=self.settings.normalized_lemmy_base_url,
                community_id=self.lemmy_community_id,
                community_name=self.settings.lemmy_community_name,
                poll_interval_seconds=self.settings.poll_interval_seconds,
            )
            self.poller.start()
        logger.info("Bridge bot is ready as %s", self.user)

    async def close(self) -> None:
        if self.poller is not None:
            await self.poller.stop()
        await self.lemmy.close()
        await super().close()

    async def on_thread_create(self, thread: discord.Thread) -> None:
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
