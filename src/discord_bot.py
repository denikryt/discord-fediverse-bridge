"""Discord adapter for slash commands and forum thread/message callbacks."""

from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands

from .config import Settings
from .db import Database
from .discord_event_router import DiscordEventRouter
from .fedify_gateway_client import FedifyGatewayClient

logger = logging.getLogger(__name__)


class BridgeBot(discord.Client):
    """Own the Discord-side event loop and forward forum channel/thread activity through CommunityRuntime."""

    # BridgeBot owns the Discord-side event loop and forwards forum channel/thread
    # activity to Lemmy based on persisted subscriptions. All publish decisions
    # go through CommunityRuntime rather than ContentPublishService directly.
    def __init__(
        self,
        *,
        settings: Settings,
        database: Database,
        fedify_gateway: FedifyGatewayClient,
        event_router: DiscordEventRouter,
    ) -> None:
        """Initialise the bot with shared services and Discord intent configuration."""
        intents = discord.Intents.default()
        intents.guilds = True
        intents.messages = True
        intents.message_content = True
        super().__init__(intents=intents)
        self.settings = settings
        self.database = database
        self.fedify_gateway = fedify_gateway
        self.event_router = event_router
        # Keep the current runtime reachable for edit/delete paths that still
        # belong only to the remote-subscription mode today.
        self.community_runtime = event_router.community_runtime
        self.tree = app_commands.CommandTree(self)
        self.bridge_ready = asyncio.Event()
        # Per-thread locks prevent duplicate Lemmy posts when Discord fires
        # on_thread_create twice for the same thread (e.g. on reconnect).
        self._thread_locks: dict[int, asyncio.Lock] = {}
        # Tracks thread IDs that have already been processed this session so
        # a second on_thread_create after lock cleanup still exits cleanly.
        self._synced_threads: set[int] = set()
        # Dedup edit/delete events fired by mirror updates we just made.
        # When we update a mirror message in Discord, Discord fires on_raw_message_edit.
        # We track recently-edited message IDs to break the echo loop.
        self._recent_edits: dict[int, float] = {}  # message_id -> timestamp
        self._recent_deletes: dict[int, float] = {}  # message_id -> timestamp

    async def setup_hook(self) -> None:
        # setup_hook runs before the bot connects, making it the right place to
        # register slash commands and sync the tree with Discord.
        from .commands import create_community, list_subs, register, subscribe, unsubscribe
        register.register(self.tree, self.settings)
        subscribe.register(self.tree, self.database, self.fedify_gateway, self.settings)
        unsubscribe.register(self.tree, self.database, self.fedify_gateway, self.settings)
        list_subs.register(self.tree, self.database)
        create_community.register(self.tree, self.database, self.settings)
        await self.tree.sync()

    async def on_ready(self) -> None:
        # Signal inbound event handlers that the bot is connected and Discord
        # API calls (thread/message creation) can proceed.
        self.bridge_ready.set()
        logger.info("Bridge bot is ready as %s", self.user)

    async def close(self) -> None:
        await self.fedify_gateway.close()
        await super().close()

    async def on_thread_create(self, thread: discord.Thread) -> None:
        # Only threads in subscribed channels should be forwarded to Lemmy.
        # Threads created by the bot itself are skipped to break the echo loop.
        if thread.parent_id is None:
            return
        if not (
            self.event_router.is_remote_subscription_forum(thread.parent_id)
            or self.event_router.is_local_community_forum(thread.parent_id)
        ):
            return
        if self.user and thread.owner_id == self.user.id:
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
            if self.database.legacy_lemmy_mappings.get_post_link_by_thread_id(thread.id) is not None:
                self._synced_threads.add(thread.id)
                return

            starter_message = await self._fetch_starter_message(thread)
            if starter_message is None:
                logger.warning("Could not fetch starter message for Discord forum thread %s", thread.id)
                return
            if starter_message.author.bot:
                return

            result = await self.event_router.handle_thread_create(
                thread=thread,
                starter_message=starter_message,
            )
            if result.status == "published":
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
        if not (
            self.event_router.is_remote_subscription_forum(message.channel.parent_id)
            or self.event_router.is_local_community_forum(message.channel.parent_id)
        ):
            return

        delivery = self.database.discord_fanout_groups.get_thread_delivery_by_thread(message.channel.id)
        logger.info(
            "[on_message] msg=%s thread=%s channel=%s author=%s delivery_role=%s",
            getattr(message, "id", None), message.channel.id, message.channel.parent_id,
            getattr(getattr(message, "author", None), "id", None),
            delivery.role if delivery else None,
        )

        # Phase 9: Allow all delivery roles (source, mirror, inbound) to proceed.
        # The bot-author guard (line 119 above) is the sole loop-prevention mechanism.
        await self.event_router.handle_message(
            message=message,
        )

    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        """Forward an edited Discord message to CommunityRuntime for propagation.

        Uses the raw event so edits fire for all messages, not just cached ones.
        Only processes Discord-originated messages (those in MessageMapping).
        Inbound AP messages (starter threads from Lemmy) are handled via AP events,
        not Discord edit events, so they're skipped here.

        Dedup: when we edit a mirror message in Discord, Discord fires on_raw_message_edit.
        We track recently-edited messages to avoid re-processing our own edits and
        creating infinite loops (edit→fanout→edit→fanout...).
        """
        import time
        logger.debug("[on_raw_message_edit] msg=%s", payload.message_id)

        # Dedup: if we edited this message recently, skip to avoid loop.
        now = time.time()
        if payload.message_id in self._recent_edits:
            edit_age = now - self._recent_edits[payload.message_id]
            if edit_age < 5.0:  # Within 5 seconds of our edit
                logger.debug("[on_raw_message_edit] skipping recent edit (age=%.1fs)", edit_age)
                return

        is_local_message = self.event_router.is_local_community_message(payload.message_id)
        delivery = None if is_local_message else self.database.discord_fanout_groups.get_message_delivery_by_message(payload.message_id)
        if delivery is None and not is_local_message:
            logger.debug("[on_raw_message_edit] no delivery found for msg=%s", payload.message_id)
            return

        # Only propagate edits from source messages — those written by the user.
        # Mirror messages are bot-owned copies; edits to them (including our own
        # propagation writes) must not re-trigger fanout or an infinite loop results.
        # Inbound AP messages are handled via AP Update events, not Discord events.
        if delivery is not None and delivery.role != "source":
            logger.debug("[on_raw_message_edit] role=%s — skipping (not source)", delivery.role)
            return

        # Extract updated content from the raw payload data.
        # Discord raw edit events include the full message data dict.
        new_content = payload.data.get("content", "")
        logger.debug(
            "[on_raw_message_edit] extracted content from payload: len=%d",
            len(new_content) if new_content else 0,
        )
        if not new_content:
            logger.debug("[on_raw_message_edit] no content in payload for msg=%s", payload.message_id)
            return

        # Extract author display name from payload for mirror header attribution.
        # global_name is the Display Name set by the user; falls back to username.
        author_data = payload.data.get("author") or {}
        author_display_name = author_data.get("global_name") or author_data.get("username") or ""

        from .runtime import Runtime
        runtime = self._get_runtime()
        if runtime is None:
            logger.warning("[on_raw_message_edit] no runtime available for msg=%s", payload.message_id)
            return

        logger.info("Discord message edit msg=%s thread=%s", payload.message_id, payload.channel_id)
        try:
            await self.event_router.handle_message_edit(
                message_id=payload.message_id,
                new_content=new_content,
                author_display_name=author_display_name,
                runtime=runtime,
            )
        except Exception:
            logger.exception("[on_raw_message_edit] exception in handle_discord_message_edit for msg=%s", payload.message_id)

    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        """Forward a deleted Discord message to CommunityRuntime for propagation.

        Uses the raw event so deletes fire for all messages, not just cached ones.
        Only processes Discord-originated messages (those in MessageMapping).
        Inbound AP messages (starter threads from Lemmy) are handled via AP events,
        not Discord delete events, so they're skipped here.

        Dedup: when we delete a mirror message in Discord, Discord fires on_raw_message_delete.
        We track recently-deleted messages to avoid re-processing our own deletes.
        """
        import time
        logger.debug("[on_raw_message_delete] msg=%s", payload.message_id)

        # Dedup: if we deleted this message recently, skip to avoid loop.
        now = time.time()
        if payload.message_id in self._recent_deletes:
            delete_age = now - self._recent_deletes[payload.message_id]
            if delete_age < 5.0:  # Within 5 seconds of our delete
                logger.debug("[on_raw_message_delete] skipping recent delete (age=%.1fs)", delete_age)
                return

        is_local_message = self.event_router.is_local_community_message(payload.message_id)
        delivery = None if is_local_message else self.database.discord_fanout_groups.get_message_delivery_by_message(payload.message_id)
        if delivery is None and not is_local_message:
            logger.debug("[on_raw_message_delete] no delivery found for msg=%s", payload.message_id)
            return

        # Only process Discord-originated messages (source or mirror role).
        # Inbound AP messages should not be deleted via Discord events.
        if delivery is not None and delivery.role == "inbound":
            logger.debug("[on_raw_message_delete] inbound message — skipping (handled via AP events)")
            return

        from .runtime import Runtime
        runtime = self._get_runtime()
        if runtime is None:
            logger.warning("[on_raw_message_delete] no runtime available for msg=%s", payload.message_id)
            return

        logger.info("Discord message delete msg=%s thread=%s", payload.message_id, payload.channel_id)
        try:
            await self.event_router.handle_message_delete(
                message_id=payload.message_id,
                runtime=runtime,
            )
        except Exception:
            logger.exception("[on_raw_message_delete] exception in handle_discord_message_delete for msg=%s", payload.message_id)

    def _get_runtime(self) -> object | None:
        """Return the bridge Runtime instance, if available.

        The Runtime is injected after construction via set_runtime(); it must be
        set before any outbound AP calls can be made. Returns None if not set,
        which causes edit/delete propagation to be skipped for that event.
        """
        return getattr(self, "_runtime", None)

    def set_runtime(self, runtime: object) -> None:
        """Inject the bridge Runtime so edit/delete handlers can call the AP gateway."""
        self._runtime = runtime

    def track_message_edit(self, message_id: int) -> None:
        """Record that we just edited this message to dedup on_raw_message_edit."""
        import time
        self._recent_edits[message_id] = time.time()

    def track_message_delete(self, message_id: int) -> None:
        """Record that we just deleted this message to dedup on_raw_message_delete."""
        import time
        self._recent_deletes[message_id] = time.time()

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
