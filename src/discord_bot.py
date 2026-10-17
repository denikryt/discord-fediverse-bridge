"""Discord adapter for slash commands and forum thread/message callbacks."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import discord

from .config import Settings
from .bridge_policy import BridgePolicyService
from .db import Database
from .discord_event_router import DiscordEventRouter
from .discord_message_mutations import DiscordMessageMutationHandler
from .fedify_gateway_client import FedifyGatewayClient
from .discord_directory import refresh_discord_directory_from_bot
from .command_tree import BridgeCommandTree
from .user_bans import UserBanService

if TYPE_CHECKING:
    from .user_profiles import UserProfileService
    from .personal_feeds import PersonalFeedService

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
        bridge_policy_service: BridgePolicyService,
        user_profile_service: UserProfileService | None = None,
        personal_feed_service: PersonalFeedService | None = None,
        user_ban_service: UserBanService | None = None,
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
        self.bridge_policy_service = bridge_policy_service
        self.user_profile_service = user_profile_service
        self.personal_feed_service = personal_feed_service
        # Keep the current runtime reachable for edit/delete paths that still
        # belong only to the remote-subscription mode today.
        self.community_runtime = event_router.community_runtime
        self.user_ban_service = user_ban_service or UserBanService(
            database=database, settings=settings
        )
        self.tree = BridgeCommandTree(self)
        self.bridge_ready = asyncio.Event()
        # Per-thread locks prevent duplicate Lemmy posts when Discord fires
        # on_thread_create twice for the same thread (e.g. on reconnect).
        self._thread_locks: dict[int, asyncio.Lock] = {}
        # Tracks thread IDs that have already been processed this session so
        # a second on_thread_create after lock cleanup still exits cleanly.
        self._synced_threads: set[int] = set()
        # Raw edit/delete mutation handling and ready-gated personal-feed
        # delivery are true bot/router cycles, so they use guarded one-time
        # binds instead of raw attribute writes from the composition root.
        self._message_mutation_handler: DiscordMessageMutationHandler | None = None
        self._personal_feed_delivery_worker: object | None = None

    async def setup_hook(self) -> None:
        # setup_hook runs before the bot connects, making it the right place to
        # register slash commands and sync the tree with Discord.
        from .commands import (
            account_settings,
            ban_user,
            bridge_policy,
            create_community,
            edit_community,
            list_banned_users,
            list_subs,
            publish_guild_invite,
            register,
            remove_guild_invite,
            subscribe,
            unban_user,
            unsubscribe,
        )

        register.register(
            self.tree, self.database, self.settings, self.bridge_policy_service
        )
        subscribe.register(
            self.tree,
            self.database,
            self.fedify_gateway,
            self.settings,
            self.bridge_policy_service,
        )
        unsubscribe.register(
            self.tree,
            self.database,
            self.fedify_gateway,
            self.settings,
            self.bridge_policy_service,
        )
        list_subs.register(
            self.tree, self.database, self.settings, self.bridge_policy_service
        )
        create_community.register(
            self.tree, self.database, self.settings, self.bridge_policy_service
        )
        edit_community.register(
            self.tree, self.database, self.settings, self.bridge_policy_service
        )
        ban_user.register(
            self.tree, self.database, self.settings, self.bridge_policy_service
        )
        unban_user.register(
            self.tree, self.database, self.settings, self.bridge_policy_service
        )
        list_banned_users.register(
            self.tree, self.database, self.settings, self.bridge_policy_service
        )
        publish_guild_invite.register(
            self.tree, self.database, self.settings, self.bridge_policy_service
        )
        remove_guild_invite.register(
            self.tree, self.database, self.settings, self.bridge_policy_service
        )
        bridge_policy.register(
            self.tree, self.database, self.bridge_policy_service, self.user_ban_service
        )
        if self.user_profile_service is not None:
            account_settings.register(
                self.tree,
                self.database,
                self.settings,
                self.bridge_policy_service,
                self.user_profile_service,
                self.personal_feed_service,
            )
        await self.tree.sync()

    async def on_ready(self) -> None:
        # Signal inbound event handlers that the bot is connected and Discord
        # API calls (thread/message creation) can proceed.
        self.bridge_ready.set()
        # Discord delivery recovery starts only after the client can resolve and
        # fetch channels. Starting earlier would consume retry attempts during
        # normal connection establishment rather than actual delivery failures.
        self._require_personal_feed_delivery_worker().start()
        # Refresh last-known Discord labels at startup so old rows and renamed
        # guilds/forums converge without live lookups during dashboard requests.
        refresh_discord_directory_from_bot(self.database, self)
        logger.info("Bridge bot is ready as %s", self.user)

    async def close(self) -> None:
        await self.fedify_gateway.close()
        await super().close()

    # MULTIPLATFORM_STAGE02_TEMP: BridgeBot callbacks still forward Discord SDK objects directly; Stage 03 must emit normalized PlatformInboundEvent values and retire this legacy ingress after regression tests.
    async def on_thread_create(self, thread: discord.Thread) -> None:
        # Only threads in subscribed channels should be forwarded to Lemmy.
        # Threads created by the bot itself are skipped to break the echo loop.
        if thread.parent_id is None:
            return
        personal_root = self.database.activitypub_objects.get_published_activity_object_by_discord_message_id(
            thread.id
        )
        if not (
            (
                personal_root is not None
                and personal_root.publication_context == "personal_feed"
            )
            or self.event_router.is_remote_subscription_forum(thread.parent_id)
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
            if (
                self.database.legacy_lemmy_mappings.get_post_link_by_thread_id(
                    thread.id
                )
                is not None
            ):
                self._synced_threads.add(thread.id)
                return

            starter_message = await self._fetch_starter_message(thread)
            if starter_message is None:
                logger.warning(
                    "Could not fetch starter message for Discord forum thread %s",
                    thread.id,
                )
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
        # Personal-feed roots are exclusive text-like surfaces. The durable
        # binding lookup remains the second gate before publication.
        from .discord_channel_types import is_personal_feed_root_channel

        if is_personal_feed_root_channel(message.channel):
            guild_id = getattr(getattr(message, "guild", None), "id", None)
            binding = (
                self.event_router.resolve_personal_feed_root(
                    guild_id, message.channel.id
                )
                if guild_id
                else None
            )
            if binding is not None:
                # Personal-feed channels are exclusive routing surfaces. The
                # publisher performs a second durable binding/owner check.
                await self.event_router.handle_personal_feed_root(
                    message=message, binding=binding
                )
                return
        if not isinstance(message.channel, discord.Thread):
            return
        if message.channel.parent_id is None:
            return
        if not (
            self.event_router.is_personal_feed_thread(message.channel.id)
            or self.event_router.is_remote_subscription_forum(message.channel.parent_id)
            or self.event_router.is_local_community_forum(message.channel.parent_id)
        ):
            return

        delivery = self.database.discord_fanout_groups.get_thread_delivery_by_thread(
            message.channel.id
        )
        logger.info(
            "[on_message] msg=%s thread=%s channel=%s author=%s delivery_role=%s",
            getattr(message, "id", None),
            message.channel.id,
            message.channel.parent_id,
            getattr(getattr(message, "author", None), "id", None),
            delivery.role if delivery else None,
        )

        # Phase 9: Allow all delivery roles (source, mirror, inbound) to proceed.
        # The bot-author guard (line 119 above) is the sole loop-prevention mechanism.
        await self.event_router.handle_message(
            message=message,
        )

    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        """Delegate raw Discord edit orchestration to the mutation handler."""
        await self._require_message_mutation_handler().handle_raw_message_edit(payload)

    async def on_raw_message_delete(
        self, payload: discord.RawMessageDeleteEvent
    ) -> None:
        """Delegate raw Discord delete orchestration to the mutation handler."""
        await self._require_message_mutation_handler().handle_raw_message_delete(payload)

    def set_message_mutation_handler(
        self, handler: DiscordMessageMutationHandler
    ) -> None:
        """Attach the raw Discord mutation handler after the bot exists."""
        if self._message_mutation_handler is not None:
            raise RuntimeError("Discord message mutation handler is already configured")
        self._message_mutation_handler = handler

    def _require_message_mutation_handler(self) -> DiscordMessageMutationHandler:
        """Return the configured raw mutation handler or fail loudly."""
        handler = getattr(self, "_message_mutation_handler", None)
        if handler is None:
            raise RuntimeError("Discord message mutation handler is not configured")
        return handler


    def bind_personal_feed_delivery_worker(self, worker: object) -> None:
        """Attach the ready-gated personal-feed Discord delivery worker once."""
        if self._personal_feed_delivery_worker is not None:
            raise RuntimeError("Personal-feed Discord delivery worker is already configured")
        self._personal_feed_delivery_worker = worker

    def _require_personal_feed_delivery_worker(self) -> object:
        """Return the ready-gated delivery worker or fail during startup callbacks."""
        worker = getattr(self, "_personal_feed_delivery_worker", None)
        if worker is None:
            raise RuntimeError("Personal-feed Discord delivery worker is not configured")
        return worker

    async def fetch_message_state(
        self, channel_id: int, message_id: int
    ) -> tuple[str, object | None]:
        """Fetch authoritative Discord message state for durable reconciliation."""
        try:
            channel = self.get_channel(channel_id) or await self.fetch_channel(
                channel_id
            )
            return "present", await channel.fetch_message(message_id)
        except discord.NotFound:
            return "missing", None

    async def _fetch_starter_message(
        self, thread: discord.Thread
    ) -> discord.Message | None:
        # Discord APIs are inconsistent here, so we try the direct starter
        # message path first and then fall back to oldest history.
        if thread.starter_message is not None:
            return thread.starter_message
        try:
            return await thread.fetch_message(thread.id)
        except discord.NotFound:
            try:
                history = [
                    message
                    async for message in thread.history(limit=1, oldest_first=True)
                ]
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
