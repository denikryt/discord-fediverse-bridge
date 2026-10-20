"""Discord SDK shell for commands and normalized Gateway callbacks."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import discord

from .config import Settings
from .bridge_policy import BridgePolicyService
from .db import Database
from .discord_event_router import DiscordEventRouter
from .discord_adapter import DiscordAdapter
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
        # A keyed admission lock protects the gap between AP publish and the
        # canonical SQLite receipt for concurrent THREAD_CREATE callbacks.
        # Unlike a seen-set, it never claims a failed delivery has succeeded.
        self._thread_locks: dict[tuple[str, int], asyncio.Lock] = {}
        # Raw edit/delete mutation handling and ready-gated personal-feed
        # delivery are true bot/router cycles, so they use guarded one-time
        # binds instead of raw attribute writes from the composition root.
        self._message_mutation_handler: DiscordMessageMutationHandler | None = None
        self._personal_feed_delivery_worker: object | None = None
        self.discord_adapter: DiscordAdapter | None = None

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

    async def _on_ready_admitted(self) -> None:
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
        """Close only the borrowed SDK; Runtime alone owns the Fedify gateway."""
        await super().close()

    def bind_discord_adapter(self, adapter: DiscordAdapter) -> None:
        """Attach the one SDK-owning transport after construction, before Gateway start."""
        if self.discord_adapter is not None or adapter.connector.key != 'discord-primary':
            raise RuntimeError('Discord adapter already configured or wrong connector')
        self.discord_adapter = adapter

    def _enter_gateway_callback(self) -> bool:
        """Reject late SDK event tasks synchronously before accessing database state."""
        adapter = self.discord_adapter
        if adapter is None:
            # Legacy test constructors are SDK-only until Stage 03 cutover.
            return True
        return adapter.enter_ingress()

    def _leave_gateway_callback(self) -> None:
        """Ensure active ingress is unregistered in success/error/cancellation paths."""
        if self.discord_adapter is not None:
            self.discord_adapter.leave_ingress()

    async def on_ready(self) -> None:
        """Gate worker startup on the same callback lifetime as other Gateway work."""
        if not self._enter_gateway_callback():
            return
        try:
            await self._on_ready_admitted()
        finally:
            self._leave_gateway_callback()

    async def on_thread_create(self, thread: discord.Thread) -> None:
        """Admit one thread event before dereferencing database or Fedify state."""
        if not self._enter_gateway_callback():
            return
        try:
            await self._on_thread_create_admitted(thread)
        finally:
            self._leave_gateway_callback()

    async def on_message(self, message: discord.Message) -> None:
        """Admit one message callback before durable domain routing."""
        if not self._enter_gateway_callback():
            return
        try:
            await self._on_message_admitted(message)
        finally:
            self._leave_gateway_callback()

    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        """Drain a raw edit even if its reconciler is blocked on Fedify."""
        if not self._enter_gateway_callback():
            return
        try:
            await self._require_discord_adapter().ingest_raw_message_edit(payload)
        finally:
            self._leave_gateway_callback()

    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        """Drain an ID-only raw delete independently of Discord SDK I/O."""
        if not self._enter_gateway_callback():
            return
        try:
            await self._require_discord_adapter().ingest_raw_message_delete(payload)
        finally:
            self._leave_gateway_callback()

    async def _on_thread_create_admitted(self, thread: discord.Thread) -> None:
        """Send subscribed forum and Personal Feed threads through one typed sink.

        Canonical deduplication lives in SQLite; a process-local seen set could
        suppress recovery following a failed or interrupted federation call.
        """
        parent_id = getattr(thread, 'parent_id', None)
        if parent_id is None or (self.user and thread.owner_id == self.user.id):
            return
        personal_root = self.database.activitypub_objects.get_published_activity_object_by_discord_message_id(
            thread.id
        )
        personal = bool(
            personal_root is not None
            and personal_root.publication_context == 'personal_feed'
            and int(personal_root.discord_channel_id) == int(parent_id)
        )
        if not (personal or self.event_router.is_remote_subscription_forum(parent_id)
                or self.event_router.is_local_community_forum(parent_id)):
            return
        # Keep concurrent notifications of this physical thread serialized
        # through publication and SQLite receipt persistence. A replay after
        # failure is still admitted; the durable repositories decide dedup.
        key = ('discord-primary', int(thread.id))
        lock = self._thread_locks.setdefault(key, asyncio.Lock())
        async with lock:
            await self._require_discord_adapter().ingest_thread_created(thread)

    async def _on_message_admitted(self, message: discord.Message) -> None:
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
                # Production uses the verified SDK -> neutral event boundary.
                # Legacy direct-test bot instances are still constructed without
                # an adapter while the other Create routes are being cut over.
                if getattr(self, 'discord_adapter', None) is not None:
                    await self._require_discord_adapter().ingest_message_created(message)
                else:
                    await self.event_router.handle_personal_feed_root(
                        message=message, binding=binding
                    )
                return
        if not isinstance(message.channel, discord.Thread):
            return
        if message.channel.parent_id is None:
            return
        # A reply may arrive before THREAD_CREATE has installed the conversation
        # mapping. Detect only an existing published Personal Feed root with the
        # same physical thread/root ID and parent; the canonical runtime will
        # independently recheck ownership before adopting or publishing it.
        personal_thread = self.event_router.is_personal_feed_thread(message.channel.id)
        possible_root = None
        if not personal_thread:
            possible_root = self.database.activitypub_objects.get_published_activity_object_by_discord_message_id(
                message.channel.id
            )
        personal_unadopted = bool(
            possible_root is not None
            and possible_root.publication_context == 'personal_feed'
            and int(possible_root.discord_channel_id) == int(message.channel.parent_id)
        )
        if not (
            personal_thread or personal_unadopted
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

        # Personal Feed and subscribed/local Community replies now share the
        # exact same normalized sink; never invoke the old SDK publisher too.
        await self._require_discord_adapter().ingest_thread_message_created(message)

    async def on_raw_thread_update(self, payload: object) -> None:
        """Normalize an uncached thread update under the Gateway shutdown gate."""
        if not self._enter_gateway_callback():
            return
        try:
            await self._require_discord_adapter().ingest_raw_thread_update(payload)
        finally:
            self._leave_gateway_callback()

    async def on_raw_thread_delete(self, payload: object) -> None:
        """Normalize an uncached thread deletion without inventing an AP Delete."""
        if not self._enter_gateway_callback():
            return
        try:
            await self._require_discord_adapter().ingest_raw_thread_delete(payload)
        finally:
            self._leave_gateway_callback()

    def _require_discord_adapter(self) -> DiscordAdapter:
        """Reject an incomplete production bind instead of using a raw fallback."""
        if self.discord_adapter is None:
            raise RuntimeError('Discord Adapter is not bound')
        return self.discord_adapter

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
