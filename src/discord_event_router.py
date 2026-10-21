"""Discord event routing boundary between bridge modes.

This dispatcher keeps `BridgeBot` as a Discord adapter. It decides whether a
forum event belongs to the existing remote-subscription mode, the new
local-community mode, or neither, then forwards the event to the right runtime.
"""

from __future__ import annotations

from .db import Database
from .bridge_policy import BridgePolicyService
from .community_sync.runtime import CommunityRuntime
from .local_communities.runtime import LocalCommunityRuntime
from .local_communities.participant_routing import (
    resolve_local_community_source_for_forum,
)
from .personal_feeds import PersonalFeedBindingView, PersonalFeedService
from .platform_types import (
    MessageCreated, MessageView, ThreadCreated, PlatformOperationError,
)
from .discord_legacy_projection import discord_id_to_legacy_int
from .runtime_ports import DiscordMutationContext


class DiscordEventRouter:
    """Route Discord thread/message events to the correct bridge runtime."""

    def __init__(
        self,
        *,
        database: Database,
        community_runtime: CommunityRuntime,
        local_community_runtime: LocalCommunityRuntime,
        bridge_policy_service: BridgePolicyService,
        personal_feed_service: PersonalFeedService | None = None,
        personal_feed_publisher: object | None = None,
        personal_feed_conversation_runtime: object | None = None,
    ) -> None:
        """Initialise the router with the runtimes for both bridge modes."""
        self.database = database
        self.community_runtime = community_runtime
        self.local_community_runtime = local_community_runtime
        self.bridge_policy_service = bridge_policy_service
        # Default construction preserves direct test/runtime builders while the
        # production composition root injects the shared service explicitly.
        self.personal_feed_service = personal_feed_service or PersonalFeedService(
            database
        )
        self.personal_feed_publisher = personal_feed_publisher
        self.personal_feed_conversation_runtime = personal_feed_conversation_runtime
        self._reply_notice_adapter: object | None = None

    def bind_reply_notice_adapter(self, adapter: object) -> None:
        """Bind the existing physical adapter for best-effort rejection notices."""
        if self._reply_notice_adapter is not None:
            raise RuntimeError('Personal Feed reply notice adapter already bound')
        self._reply_notice_adapter = adapter

    def _is_guild_allowed(self, guild_id: int | str | None) -> bool:
        """Evaluate one Discord event against the current guild policy."""
        return self.bridge_policy_service.is_discord_guild_allowed(guild_id)

    def resolve_personal_feed_root(
        self, guild_id: int, channel_id: int
    ) -> PersonalFeedBindingView | None:
        """Resolve an allowed guild/channel pair to its current personal-feed owner."""
        if not self._is_guild_allowed(guild_id):
            return None
        return self.personal_feed_service.resolve_root_binding(guild_id, channel_id)

    async def handle_personal_feed_root(
        self, *, message: object, binding: PersonalFeedBindingView
    ) -> object:
        """Forward one classified root to the dedicated personal-feed publisher."""
        if self.personal_feed_publisher is None:
            return None
        return await self.personal_feed_publisher.publish_root(
            message=message, binding=binding
        )

    async def handle_platform_message_create(self, event: MessageCreated) -> object:
        """Route a verified neutral Create to one owning canonical runtime.

        Personal Feed and forum sources never share publication ownership;
        Local Community takes precedence over Remote Community for forum IDs.
        """
        if event.connector_key != 'discord-primary':
            raise PlatformOperationError('invalid_request', 'Discord root connector mismatch')
        if event.thread_context is not None:
            # Text/announcement replies are Personal Feed only when the
            # canonical root and thread parent have already been proven. A
            # reply can arrive ahead of THREAD_CREATE, so adopt that exact root
            # via the existing repository before attempting publication.
            if (event.parent_channel is not None
                and event.parent_channel.kind in ('text', 'announcement')
                and self.personal_feed_conversation_runtime is not None):
                if (event.external_guild_id is not None and event.external_guild_id.isdecimal()
                    and self._is_guild_allowed(int(event.external_guild_id))):
                    thread_id = discord_id_to_legacy_int(event.thread_context)
                    if not self.is_personal_feed_thread(thread_id):
                        self.personal_feed_conversation_runtime.adopt_normalized_thread(
                            ThreadCreated(
                                connector_key=event.connector_key,
                                thread=event.thread_context, name='',
                                external_guild_id=event.external_guild_id,
                            )
                        )
                    if self.is_personal_feed_thread(thread_id):
                        return await self.personal_feed_conversation_runtime.publish_normalized_reply(
                            event, adapter=self._reply_notice_adapter,
                        )
                return None
            if (event.parent_channel is not None
                and event.parent_channel.kind == 'forum'):
                if (event.external_guild_id is None
                    or not event.external_guild_id.isdecimal()
                    or not self._is_guild_allowed(int(event.external_guild_id))):
                    return None
                if self._reply_notice_adapter is None:
                    raise RuntimeError('Discord adapter is not bound for forum replies')
                forum_id = discord_id_to_legacy_int(event.thread_context, component='parent')
                # Local host/subscriber ownership wins over remote subscription
                # just as in the old SDK router; each runtime independently
                # checks the physical source's current authorization.
                if self.is_local_community_forum(forum_id):
                    return await self.local_community_runtime.handle_normalized_message_create(
                        event, adapter=self._reply_notice_adapter,
                    )
                if self.is_remote_subscription_forum(forum_id):
                    return await self.community_runtime.handle_normalized_message_create(
                        event, adapter=self._reply_notice_adapter,
                    )
                return None
            # A normalized event with no verified parent cannot be classified
            # into a canonical community or Personal Feed publication.
            raise PlatformOperationError('invalid_request', 'unverified thread parent kind')
        if event.parent_channel is None or event.parent_channel.kind not in ('text', 'announcement'):
            raise RuntimeError('normalized community create ingress is not bound')
        if event.content.attachments:
            raise PlatformOperationError('unsupported', 'personal feed attachment publication is not supported')
        if (event.author.kind != 'human'
            or event.author.external_actor_id is None
            or event.external_guild_id is None
            or event.created_at is None):
            return None
        try:
            if not event.external_guild_id.isdecimal() or int(event.external_guild_id) <= 0:
                return None
            guild_id = int(event.external_guild_id)
            channel_id = discord_id_to_legacy_int(event.parent_channel)
        except PlatformOperationError:
            return None
        binding = self.resolve_personal_feed_root(guild_id, channel_id)
        if binding is None or self.personal_feed_publisher is None:
            return None
        # Publisher rechecks current binding and author before any AP render.
        return await self.personal_feed_publisher.publish_normalized_root(
            event=event, binding=binding,
        )

    async def handle_platform_thread_create(self, event: ThreadCreated) -> object:
        """Route verified forum starters or adopt existing Personal Feed roots."""
        if event.connector_key != 'discord-primary':
            raise PlatformOperationError('invalid_request', 'Discord thread connector mismatch')
        if event.thread.channel.kind == 'forum':
            if (event.external_guild_id is None
                or not event.external_guild_id.isdecimal()
                or not self._is_guild_allowed(int(event.external_guild_id))):
                return None
            if event.starter is None:
                # Do not invent thread.id as a starter or fall back to the
                # first unrelated history message. Later replay may succeed.
                return None
            if self._reply_notice_adapter is None:
                raise RuntimeError('Discord adapter is not bound for forum starters')
            starter = await self._reply_notice_adapter.fetch_message(event.starter)
            if not isinstance(starter, MessageView) or starter.message != event.starter:
                raise PlatformOperationError('invalid_request', 'unverified forum starter receipt')
            if starter.author is None or starter.author.kind != 'human' or starter.content is None:
                return None
            forum_id = discord_id_to_legacy_int(event.thread, component='parent')
            if self.is_local_community_forum(forum_id):
                return await self.local_community_runtime.handle_normalized_thread_create(
                    event, starter, adapter=self._reply_notice_adapter,
                )
            if self.is_remote_subscription_forum(forum_id):
                return await self.community_runtime.handle_normalized_thread_create(
                    event, starter, adapter=self._reply_notice_adapter,
                )
            return None
        if event.thread.channel.kind not in ('text', 'announcement'):
            return None
        if (event.external_guild_id is None or not event.external_guild_id.isdecimal()
            or not self._is_guild_allowed(int(event.external_guild_id))):
            return None
        if self.personal_feed_conversation_runtime is None:
            return None
        return self.personal_feed_conversation_runtime.adopt_normalized_thread(event)

    def is_personal_feed_thread(self, thread_id: int | None) -> bool:
        """Return whether one Discord thread is durably owned by a personal-feed root."""
        return bool(
            thread_id is not None
            and self.database.personal_feed_conversations.get_thread_by_thread_id(
                thread_id
            )
            is not None
        )

    def handle_personal_feed_thread_create(self, *, thread: object) -> object:
        """Adopt a manually created thread before community routing can capture it."""
        if self.personal_feed_conversation_runtime is None:
            return None
        return self.personal_feed_conversation_runtime.adopt_manual_thread(
            thread=thread
        )

    def is_active_personal_feed_channel(self, user_id: int, channel_id: int) -> bool:
        """Expose the durable current-channel comparison required by later work."""
        return self.personal_feed_service.is_active_personal_feed_channel(
            user_id, channel_id
        )

    def is_local_community_forum(self, forum_channel_id: int | None) -> bool:
        """Return whether one forum is a local-community create source.

        durable delivery widens create routing from host forums to active local
        subscriber forums.  The helper keeps inactive subscribers and unrelated
        forums out of local-community mode while preserving host precedence.
        """
        return (
            resolve_local_community_source_for_forum(self.database, forum_channel_id)
            is not None
        )

    def is_remote_subscription_forum(self, forum_channel_id: int | None) -> bool:
        """Return whether the given forum channel is subscribed to a remote community."""
        if forum_channel_id is None:
            return False
        return (
            self.database.remote_subscriptions.get_subscription_by_channel(
                forum_channel_id
            )
            is not None
        )

    def is_local_community_message(self, message_id: int) -> bool:
        """Return whether one Discord message belongs to local-community mode.

        Platform API contract moves Discord ownership to explicit surface rows, so both
        starter and reply detection must resolve through those tables instead
        of checking canonical local-community rows directly.
        """
        return (
            self.database.local_community_surfaces.get_local_community_thread_surface_by_starter_message_id(
                message_id
            )
            is not None
            or self.database.local_community_surfaces.get_local_community_message_surface_by_discord_message_id(
                message_id
            )
            is not None
        )

    async def handle_thread_create(
        self, *, thread: object, starter_message: object
    ) -> object:
        """Route one thread-create event into the owning bridge mode."""
        guild = getattr(thread, "guild", None)
        guild_id = getattr(guild, "id", None) or getattr(thread, "guild_id", None)
        if not self._is_guild_allowed(guild_id):
            return None
        forum_channel_id = getattr(thread, "parent_id", None)
        personal = self.handle_personal_feed_thread_create(thread=thread)
        if personal is not None and getattr(personal, "status", None) != "ignored":
            return personal
        if self.is_local_community_forum(forum_channel_id):
            return await self.local_community_runtime.handle_discord_thread_create(
                thread=thread,
                starter_message=starter_message,
            )
        return await self.community_runtime.handle_discord_thread_create(
            thread=thread,
            starter_message=starter_message,
        )

    async def handle_message(self, *, message: object) -> object:
        """Route one message event into the owning bridge mode."""
        guild = getattr(message, "guild", None)
        guild_id = getattr(guild, "id", None) or getattr(message, "guild_id", None)
        if not self._is_guild_allowed(guild_id):
            return None
        thread = getattr(message, "channel")
        if self.is_personal_feed_thread(getattr(thread, "id", None)):
            if self.personal_feed_conversation_runtime is None:
                return None
            return await self.personal_feed_conversation_runtime.publish_local_reply(
                message=message
            )
        forum_channel_id = getattr(thread, "parent_id", None)
        personal = self.handle_personal_feed_thread_create(thread=thread)
        if personal is not None and getattr(personal, "status", None) != "ignored":
            return personal
        if self.is_local_community_forum(forum_channel_id):
            return await self.local_community_runtime.handle_discord_message(
                message=message,
            )
        return await self.community_runtime.handle_discord_message(
            message=message,
        )

    async def handle_message_edit(
        self,
        *,
        message_id: int,
        new_content: str,
        author_display_name: str,
        mutation_context: DiscordMutationContext,
        edited_timestamp=None,
        guild_id: int | None = None,
    ) -> None:
        """Route one raw Discord edit event into the owning bridge mode."""
        if guild_id is not None and not self._is_guild_allowed(guild_id):
            return
        mapping = (
            self.database.message_mappings.get_message_mapping_by_discord_message_id(
                message_id
            )
        )
        if mapping is not None and mapping.publication_context == "personal_feed":
            outcome = await mutation_context.user_actor_delivery_service.submit_update(
                object_id=mapping.object_id,
                title=None,
                body_markdown=new_content,
                edited_timestamp=edited_timestamp,
            )
            if outcome == "accepted":
                mutation_context.user_actor_delivery_wake.wake()
            return
        if self.is_local_community_message(message_id):
            await self.local_community_runtime.handle_discord_message_edit(
                message_id=message_id,
                new_content=new_content,
                author_display_name=author_display_name,
                mutation_context=mutation_context,
                edited_timestamp=edited_timestamp,
            )
            return
        await self.community_runtime.handle_discord_message_edit(
            message_id=message_id,
            new_content=new_content,
            author_display_name=author_display_name,
            mutation_context=mutation_context,
            edited_timestamp=edited_timestamp,
        )

    async def handle_message_delete(
        self,
        *,
        message_id: int,
        mutation_context: DiscordMutationContext,
        guild_id: int | None = None,
    ) -> None:
        """Route one raw Discord delete event into the owning bridge mode."""
        if guild_id is not None and not self._is_guild_allowed(guild_id):
            return
        mapping = (
            self.database.message_mappings.get_message_mapping_by_discord_message_id(
                message_id
            )
        )
        if mapping is not None and mapping.publication_context == "personal_feed":
            outcome = await mutation_context.user_actor_delivery_service.submit_delete(
                object_id=mapping.object_id
            )
            if outcome == "accepted":
                mutation_context.user_actor_delivery_wake.wake()
            return
        if self.is_local_community_message(message_id):
            await self.local_community_runtime.handle_discord_message_delete(
                message_id=message_id,
                mutation_context=mutation_context,
            )
            return
        await self.community_runtime.handle_discord_message_delete(
            message_id=message_id,
            mutation_context=mutation_context,
        )
