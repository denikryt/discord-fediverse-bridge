from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands

from .bridge_discord_to_lemmy import sync_forum_thread_to_lemmy, sync_thread_message_to_lemmy_comment
from .config import Settings
from .db import Database
from .fedify_gateway_client import FedifyGatewayClient
from .lemmy_client import LemmyClient

logger = logging.getLogger(__name__)


class BridgeBot(discord.Client):
    # BridgeBot owns the Discord-side event loop and forwards forum channel/thread
    # activity to Lemmy based on persisted subscriptions.
    def __init__(
        self,
        *,
        settings: Settings,
        database: Database,
        fedify_gateway: FedifyGatewayClient,
        lemmy: LemmyClient,
    ) -> None:
        intents = discord.Intents.default()
        intents.guilds = True
        intents.messages = True
        intents.message_content = True
        super().__init__(intents=intents)
        self.settings = settings
        self.database = database
        self.fedify_gateway = fedify_gateway
        self.lemmy = lemmy
        self.tree = app_commands.CommandTree(self)
        self.bridge_ready = asyncio.Event()
        # Per-thread locks prevent duplicate Lemmy posts when Discord fires
        # on_thread_create twice for the same thread (e.g. on reconnect).
        self._thread_locks: dict[int, asyncio.Lock] = {}
        # Tracks thread IDs that have already been processed this session so
        # a second on_thread_create after lock cleanup still exits cleanly.
        self._synced_threads: set[int] = set()

    async def setup_hook(self) -> None:
        # setup_hook runs before the bot connects, making it the right place to
        # register slash commands and sync the tree with Discord.
        from .commands import list_subs, register, subscribe, unsubscribe
        register.register(self.tree, self.settings)
        subscribe.register(self.tree, self.database, self.lemmy, self.fedify_gateway)
        unsubscribe.register(self.tree, self.database)
        list_subs.register(self.tree, self.database)
        await self.tree.sync()

    async def on_ready(self) -> None:
        # Signal inbound event handlers that the bot is connected and Discord
        # API calls (thread/message creation) can proceed.
        self.bridge_ready.set()
        logger.info("Bridge bot is ready as %s", self.user)

    async def close(self) -> None:
        await self.fedify_gateway.close()
        await self.lemmy.close()
        await super().close()

    async def on_thread_create(self, thread: discord.Thread) -> None:
        # Only threads in subscribed channels should be forwarded to Lemmy.
        # Threads created by the bot itself are skipped to break the echo loop.
        if thread.parent_id is None:
            return
        subscription = self.database.get_subscription_by_channel(thread.parent_id)
        if subscription is None:
            return
        if self.user and thread.owner_id == self.user.id:
            return
        # lemmy_community_id can be None if the subscription was created before
        # the community was successfully resolved (e.g. legacy migration failure).
        if subscription.lemmy_community_id is None:
            logger.warning("Subscription for channel %s has no resolved community ID, skipping thread %s", thread.parent_id, thread.id)
            return

        # In-memory fast-path: if this session already processed the thread,
        # skip before acquiring any lock or hitting the DB.
        if thread.id in self._synced_threads:
            return

        # Acquire a per-thread lock so a second on_thread_create for the same
        # thread (Discord reconnect race) blocks until the first finishes.
        if thread.id not in self._thread_locks:
            self._thread_locks[thread.id] = asyncio.Lock()
        async with self._thread_locks[thread.id]:
            # Re-check both the in-memory set and the DB inside the lock.
            if thread.id in self._synced_threads:
                return
            if self.database.get_post_link_by_thread_id(thread.id) is not None:
                self._synced_threads.add(thread.id)
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
                community_id=subscription.lemmy_community_id,
                bridge_prefix=self.settings.bridge_display_prefix,
                thread=thread,
                starter_message=starter_message,
            )
            self._synced_threads.add(thread.id)

    async def on_message(self, message: discord.Message) -> None:
        # Only messages in threads inside subscribed channels are forwarded.
        # The subscription check guards against channels with no active mapping.
        if self.user and message.author.id == self.user.id:
            return
        if message.author.bot:
            return
        if not isinstance(message.channel, discord.Thread):
            return
        if message.channel.parent_id is None:
            return
        subscription = self.database.get_subscription_by_channel(message.channel.parent_id)
        if subscription is None:
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

    async def fetch_forum_channel(self, channel_id: int) -> discord.ForumChannel:
        # Tries the local cache first; falls back to an API call for channels
        # the bot hasn't seen in the current session.
        channel = self.get_channel(channel_id) or await self.fetch_channel(channel_id)
        if not isinstance(channel, discord.ForumChannel):
            raise RuntimeError(f"Channel {channel_id} is not a forum channel")
        return channel

    async def get_thread_by_id(self, thread_id: int) -> discord.Thread:
        cached = self.get_channel(thread_id)
        if isinstance(cached, discord.Thread):
            return cached
        channel = await self.fetch_channel(thread_id)
        if not isinstance(channel, discord.Thread):
            raise RuntimeError(f"Mapped Discord thread {thread_id} was not found")
        return channel
