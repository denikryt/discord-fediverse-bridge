"""Discord-authored local-community create orchestration.

This module owns host and local-subscriber Discord create paths that publish
canonical ActivityPub objects and repair local Discord surfaces on duplicates.
The runtime facade still decides whether an event belongs to local-community
mode before delegating here.
"""

from __future__ import annotations

from ..content_publish_service import ContentPublishService
from ..db import Database
from ..discord_legacy_projection import discord_id_to_legacy_int
from ..platform_types import (
    MessageCreated, MessageView, PlatformOperationError, ThreadCreated,
    validate_message_reply,
)
from ..runtime_ports import PlatformDeliveryPort
from ..content_sync.outbound_publish import (
    build_discord_comment_body, build_discord_post_title,
)
from .discord_fanout import LocalCommunityDiscordFanout
from .participant_routing import ResolvedLocalCommunitySource
from .delivery_mapping import (
    get_local_community_message_for_discord_message,
    get_local_community_thread_for_discord_thread,
    get_local_community_thread_surface_for_discord_thread,
)
from .discord_surface_fanout import LocalCommunitySurfaceFanout
from .reply_mapping import (
    resolve_outbound_reply_context,
    resolve_outbound_reply_context_for_surface,
)
from .results import LocalCommunityRuntimeResult


class LocalCommunityDiscordCreateHandler:
    """Handle Discord-authored local-community post and comment creates."""

    def __init__(
        self,
        *,
        database: Database,
        content_publish_service: ContentPublishService,
        surface_fanout: LocalCommunitySurfaceFanout,
    ) -> None:
        """Store the narrow create-path dependencies used by moved runtime code."""
        self.database = database
        self.content_publish_service = content_publish_service
        self.surface_fanout = surface_fanout

    async def handle_host_thread_create(
        self,
        *,
        thread: object,
        starter_message: object,
        local_community: object,
    ) -> LocalCommunityRuntimeResult:
        """Handle the existing host-forum thread create path unchanged."""
        existing_thread = get_local_community_thread_for_discord_thread(
            self.database, getattr(thread, "id")
        )
        if existing_thread is not None:
            await self.surface_fanout.fanout_thread_to_local_subscribers(
                local_community=local_community,
                thread_row=existing_thread,
                title=getattr(thread, "name", "Untitled thread"),
                content=getattr(starter_message, "content", ""),
                author_display_name=self.content_publish_service.canonical_author_name_for_discord(
                    getattr(starter_message, "author")
                ),
                source_forum_channel_id=getattr(thread, "parent_id"),
            )
            return LocalCommunityRuntimeResult(
                status="ignored", reason="duplicate_thread"
            )

        publish_result = (
            await self.content_publish_service.publish_local_thread_starter(
                thread=thread,
                starter_message=starter_message,
                community_actor_url=getattr(local_community, "actor_url"),
            )
        )
        if publish_result.status != "published":
            return LocalCommunityRuntimeResult(
                status=publish_result.status, reason=publish_result.reason
            )

        thread_row = (
            self.database.local_community_content.create_local_community_thread(
                local_community_id=getattr(local_community, "id"),
                discord_thread_id=getattr(thread, "id"),
                discord_starter_message_id=getattr(starter_message, "id"),
                ap_activity_id=publish_result.activity_id,
                ap_object_id=publish_result.object_id,
                direction="discord_to_ap",
                origin_kind="discord_local",
            )
        )
        await self.surface_fanout.fanout_thread_to_local_subscribers(
            local_community=local_community,
            thread_row=thread_row,
            title=getattr(thread, "name", "Untitled thread"),
            content=getattr(starter_message, "content", ""),
            author_display_name=self.content_publish_service.canonical_author_name_for_discord(
                getattr(starter_message, "author")
            ),
            source_forum_channel_id=getattr(thread, "parent_id"),
        )
        return LocalCommunityRuntimeResult(
            status="published",
            reason="published",
            activity_id=publish_result.activity_id,
            object_id=publish_result.object_id,
        )

    async def handle_local_subscriber_thread_create(
        self,
        *,
        thread: object,
        starter_message: object,
        local_community: object,
        local_subscriber: object | None,
    ) -> LocalCommunityRuntimeResult:
        """Handle a Stage 4 local-subscriber forum thread as canonical content."""
        if local_subscriber is None:
            return LocalCommunityRuntimeResult(
                status="ignored", reason="not_local_subscriber"
            )
        source_surface = get_local_community_thread_surface_for_discord_thread(
            self.database, getattr(thread, "id")
        )
        if source_surface is not None:
            thread_row = self.database.local_community_surfaces.get_local_community_thread_for_surface(
                source_surface.id
            )
            if thread_row is not None:
                await self.surface_fanout.fanout_thread_to_local_participants(
                    local_community=local_community,
                    thread_row=thread_row,
                    title=getattr(thread, "name", "Untitled thread"),
                    content=getattr(starter_message, "content", ""),
                    author_display_name=self.content_publish_service.canonical_author_name_for_discord(
                        getattr(starter_message, "author")
                    ),
                    source_forum_channel_id=getattr(thread, "parent_id"),
                    include_host=True,
                )
            return LocalCommunityRuntimeResult(
                status="ignored", reason="duplicate_thread"
            )

        publish_result = (
            await self.content_publish_service.publish_local_thread_starter(
                thread=thread,
                starter_message=starter_message,
                community_actor_url=getattr(local_community, "actor_url"),
            )
        )
        if publish_result.status != "published":
            return LocalCommunityRuntimeResult(
                status=publish_result.status, reason=publish_result.reason
            )

        thread_row = self.database.local_community_content.create_local_community_thread_canonical(
            local_community_id=getattr(local_community, "id"),
            ap_activity_id=publish_result.activity_id,
            ap_object_id=publish_result.object_id,
            direction="discord_to_ap",
            origin_kind="discord_local_subscriber",
        )
        self.database.local_community_surfaces.create_local_community_thread_surface(
            local_community_thread_id=getattr(thread_row, "id"),
            discord_forum_channel_id=getattr(thread, "parent_id"),
            discord_thread_id=getattr(thread, "id"),
            discord_starter_message_id=getattr(starter_message, "id"),
            role="local_subscriber",
            local_subscriber_id=getattr(local_subscriber, "id"),
        )
        await self.surface_fanout.fanout_thread_to_local_participants(
            local_community=local_community,
            thread_row=thread_row,
            title=getattr(thread, "name", "Untitled thread"),
            content=getattr(starter_message, "content", ""),
            author_display_name=self.content_publish_service.canonical_author_name_for_discord(
                getattr(starter_message, "author")
            ),
            source_forum_channel_id=getattr(thread, "parent_id"),
            include_host=True,
        )
        return LocalCommunityRuntimeResult(
            status="published",
            reason="published",
            activity_id=publish_result.activity_id,
            object_id=publish_result.object_id,
        )

    async def handle_host_message(
        self, *, message: object, local_community: object
    ) -> LocalCommunityRuntimeResult:
        """Handle the existing host-forum Discord reply path unchanged."""
        thread = getattr(message, "channel")
        existing_message = get_local_community_message_for_discord_message(
            self.database, getattr(message, "id")
        )
        if existing_message is not None:
            thread_row = (
                self.database.local_community_content.get_local_community_thread_by_id(
                    getattr(existing_message, "local_community_thread_id")
                )
            )
            if thread_row is not None:
                await self.surface_fanout.fanout_message_to_local_subscribers(
                    local_community=local_community,
                    thread_row=thread_row,
                    message_row=existing_message,
                    content=getattr(message, "content", ""),
                    author_display_name=self.content_publish_service.canonical_author_name_for_discord(
                        getattr(message, "author")
                    ),
                    source_forum_channel_id=getattr(thread, "parent_id"),
                )
            return LocalCommunityRuntimeResult(
                status="ignored", reason="duplicate_message"
            )

        thread_row = get_local_community_thread_for_discord_thread(
            self.database, getattr(thread, "id")
        )
        if thread_row is None:
            return LocalCommunityRuntimeResult(
                status="ignored", reason="no_thread_context"
            )
        thread_surface = get_local_community_thread_surface_for_discord_thread(
            self.database, getattr(thread, "id")
        )
        if thread_surface is None:
            return LocalCommunityRuntimeResult(
                status="ignored", reason="no_thread_surface"
            )
        if getattr(thread_surface, "discord_starter_message_id") == getattr(
            message, "id"
        ):
            return LocalCommunityRuntimeResult(
                status="ignored", reason="starter_message_already_handled"
            )

        reply_context = resolve_outbound_reply_context(
            database=self.database, thread_row=thread_row, message=message
        )
        publish_result = (
            await self.content_publish_service.publish_local_thread_message(
                message=message,
                community_actor_url=getattr(local_community, "actor_url"),
                parent_object_id=reply_context.parent_ap_object_id,
            )
        )
        if publish_result.status != "published":
            return LocalCommunityRuntimeResult(
                status=publish_result.status, reason=publish_result.reason
            )

        message_row = (
            self.database.local_community_content.create_local_community_message(
                local_community_thread_id=getattr(thread_row, "id"),
                discord_message_id=getattr(message, "id"),
                ap_activity_id=publish_result.activity_id,
                ap_object_id=publish_result.object_id,
                parent_ap_object_id=reply_context.parent_ap_object_id,
                parent_discord_message_id=reply_context.parent_discord_message_id,
                direction="discord_to_ap",
            )
        )
        await self.surface_fanout.fanout_message_to_local_subscribers(
            local_community=local_community,
            thread_row=thread_row,
            message_row=message_row,
            content=getattr(message, "content", ""),
            author_display_name=self.content_publish_service.canonical_author_name_for_discord(
                getattr(message, "author")
            ),
            source_forum_channel_id=getattr(thread, "parent_id"),
        )
        return LocalCommunityRuntimeResult(
            status="published",
            reason="published",
            activity_id=publish_result.activity_id,
            object_id=publish_result.object_id,
        )

    async def handle_local_subscriber_message(
        self,
        *,
        message: object,
        local_community: object,
        local_subscriber: object | None,
    ) -> LocalCommunityRuntimeResult:
        """Handle a Stage 4 local-subscriber Discord reply as canonical content."""
        if local_subscriber is None:
            return LocalCommunityRuntimeResult(
                status="ignored", reason="not_local_subscriber"
            )
        thread = getattr(message, "channel")
        existing_message = get_local_community_message_for_discord_message(
            self.database, getattr(message, "id")
        )
        if existing_message is not None:
            thread_row = (
                self.database.local_community_content.get_local_community_thread_by_id(
                    getattr(existing_message, "local_community_thread_id")
                )
            )
            if thread_row is not None:
                await self.surface_fanout.fanout_message_to_local_participants(
                    local_community=local_community,
                    thread_row=thread_row,
                    message_row=existing_message,
                    content=getattr(message, "content", ""),
                    author_display_name=self.content_publish_service.canonical_author_name_for_discord(
                        getattr(message, "author")
                    ),
                    source_forum_channel_id=getattr(thread, "parent_id"),
                    include_host=True,
                )
            return LocalCommunityRuntimeResult(
                status="ignored", reason="duplicate_message"
            )

        thread_surface = get_local_community_thread_surface_for_discord_thread(
            self.database, getattr(thread, "id")
        )
        if (
            thread_surface is None
            or getattr(thread_surface, "role") != "local_subscriber"
        ):
            return LocalCommunityRuntimeResult(
                status="ignored", reason="no_thread_surface"
            )
        if getattr(thread_surface, "discord_starter_message_id") == getattr(
            message, "id"
        ):
            return LocalCommunityRuntimeResult(
                status="ignored", reason="starter_message_already_handled"
            )
        thread_row = self.database.local_community_surfaces.get_local_community_thread_for_surface(
            getattr(thread_surface, "id")
        )
        if thread_row is None:
            return LocalCommunityRuntimeResult(
                status="ignored", reason="no_thread_context"
            )

        reply_context = resolve_outbound_reply_context_for_surface(
            database=self.database,
            thread_row=thread_row,
            source_thread_surface=thread_surface,
            message=message,
        )
        publish_result = (
            await self.content_publish_service.publish_local_thread_message(
                message=message,
                community_actor_url=getattr(local_community, "actor_url"),
                parent_object_id=reply_context.parent_ap_object_id,
            )
        )
        if publish_result.status != "published":
            return LocalCommunityRuntimeResult(
                status=publish_result.status, reason=publish_result.reason
            )

        message_row = self.database.local_community_content.create_local_community_message_canonical(
            local_community_thread_id=getattr(thread_row, "id"),
            ap_activity_id=publish_result.activity_id,
            ap_object_id=publish_result.object_id,
            parent_ap_object_id=reply_context.parent_ap_object_id,
            direction="discord_to_ap",
        )
        self.database.local_community_surfaces.create_local_community_message_surface(
            local_community_message_id=getattr(message_row, "id"),
            local_community_thread_surface_id=getattr(thread_surface, "id"),
            discord_forum_channel_id=getattr(thread, "parent_id"),
            discord_message_id=getattr(message, "id"),
            parent_discord_message_id=reply_context.parent_discord_message_id,
            role="local_subscriber",
            local_subscriber_id=getattr(local_subscriber, "id"),
        )
        await self.surface_fanout.fanout_message_to_local_participants(
            local_community=local_community,
            thread_row=thread_row,
            message_row=message_row,
            content=getattr(message, "content", ""),
            author_display_name=self.content_publish_service.canonical_author_name_for_discord(
                getattr(message, "author")
            ),
            source_forum_channel_id=getattr(thread, "parent_id"),
            include_host=True,
        )
        return LocalCommunityRuntimeResult(
            status="published",
            reason="published",
            activity_id=publish_result.activity_id,
            object_id=publish_result.object_id,
        )

    async def handle_normalized_thread_create(
        self, *, event: ThreadCreated, starter: MessageView,
        source: ResolvedLocalCommunitySource, adapter: PlatformDeliveryPort,
        fanout: LocalCommunityDiscordFanout,
    ) -> LocalCommunityRuntimeResult:
        """Persist a typed local starter and mirror it without SDK-shaped fields.

        Original source receipts belong to host or subscriber surfaces; remote
        copies cannot become a second canonical publication on Gateway replay.
        """
        thread_id = discord_id_to_legacy_int(event.thread)
        forum_id = discord_id_to_legacy_int(event.thread, component='parent')
        starter_id = discord_id_to_legacy_int(starter.message)
        existing_surface = get_local_community_thread_surface_for_discord_thread(
            self.database, thread_id
        )
        if existing_surface is not None:
            if (existing_surface.discord_forum_channel_id != forum_id
                or existing_surface.discord_starter_message_id != starter_id):
                raise PlatformOperationError('invalid_request', 'conflicting physical starter receipt')
            canonical = self.database.local_community_surfaces.get_local_community_thread_for_surface(
                existing_surface.id
            )
            if canonical is None or canonical.local_community_id != source.local_community.id:
                raise PlatformOperationError('invalid_request', 'foreign canonical thread surface')
            published = self.database.activitypub_objects.get_published_activity_object_by_object_id(
                canonical.ap_object_id
            )
            # Only the first, verified publishing source may repair unfinished
            # copies. A subscriber mirror has the same role as a subscriber
            # source; role alone cannot prove who authored the AP object.
            original = (
                published is not None
                and published.activity_id == canonical.ap_activity_id
                and published.discord_channel_id == forum_id
                and published.discord_message_id == starter_id
                and published.title == build_discord_post_title(thread_name=event.name)
                and starter.author is not None and starter.author.kind == 'human'
                and starter.author.external_actor_id is not None
                and starter.content.format == 'plain' and not starter.content.attachments
                and published.actor_url == getattr(
                    self.database.users.get_user_by_discord_user_id(starter.author.external_actor_id),
                    'actor_url', None,
                )
                and published.body_markdown == build_discord_comment_body(
                    author_name=starter.author.display_name,
                    content=starter.content.body,
                    bridge_prefix=self.content_publish_service.bridge_prefix,
                )
            )
            if original:
                await fanout.fanout_normalized_thread(
                    adapter=adapter, local_community=source.local_community,
                    thread_row=canonical, title=event.name,
                    content=starter.content.body,
                    author_display_name=starter.author.display_name,
                    source_forum_channel_id=forum_id,
                    include_host=source.source_kind == 'local_subscriber',
                )
            return LocalCommunityRuntimeResult('ignored', 'duplicate_thread')

        published = await self.content_publish_service.publish_normalized_post_to_community(
            event=event, starter=starter,
            community_actor_url=source.local_community.actor_url,
            publish_call=self.content_publish_service.fedify_gateway.publish_local_community_content,
            target_inbox_urls=self.content_publish_service._allowed_local_follower_inboxes(
                source.local_community.actor_url
            ),
            notice_adapter=adapter,
        )
        if published.status != 'published':
            return LocalCommunityRuntimeResult(published.status, published.reason)
        if source.source_kind == 'host_forum':
            canonical = self.database.local_community_content.create_local_community_thread(
                local_community_id=source.local_community.id,
                discord_thread_id=thread_id, discord_starter_message_id=starter_id,
                ap_activity_id=published.activity_id, ap_object_id=published.object_id,
                direction='discord_to_ap', origin_kind='discord_local',
            )
        else:
            canonical = self.database.local_community_content.create_local_community_thread_canonical(
                local_community_id=source.local_community.id,
                ap_activity_id=published.activity_id, ap_object_id=published.object_id,
                direction='discord_to_ap', origin_kind='discord_local_subscriber',
            )
            self.database.local_community_surfaces.create_local_community_thread_surface(
                local_community_thread_id=canonical.id, discord_forum_channel_id=forum_id,
                discord_thread_id=thread_id, discord_starter_message_id=starter_id,
                role='local_subscriber', local_subscriber_id=source.local_subscriber.id,
            )
        await fanout.fanout_normalized_thread(
            adapter=adapter, local_community=source.local_community,
            thread_row=canonical, title=event.name, content=starter.content.body,
            author_display_name=starter.author.display_name if starter.author else None,
            source_forum_channel_id=forum_id,
            include_host=source.source_kind == 'local_subscriber',
        )
        return LocalCommunityRuntimeResult(
            'published', 'published', published.activity_id, published.object_id,
        )

    async def handle_normalized_message_create(
        self, *, event: MessageCreated, source: ResolvedLocalCommunitySource,
        adapter: PlatformDeliveryPort, fanout: LocalCommunityDiscordFanout,
    ) -> LocalCommunityRuntimeResult:
        """Persist and distribute one local reply with source-local parent identity.

        Never resolve a mapped parent from another physical thread, even if a
        foreign Discord ID happens to be present in the same SQLite database.
        """
        thread_id = discord_id_to_legacy_int(event.thread_context)
        forum_id = discord_id_to_legacy_int(event.thread_context, component='parent')
        message_id = discord_id_to_legacy_int(event.message)
        thread_surface = get_local_community_thread_surface_for_discord_thread(
            self.database, thread_id
        )
        if thread_surface is None:
            return LocalCommunityRuntimeResult('ignored', 'no_thread_surface')
        if thread_surface.discord_forum_channel_id != forum_id:
            raise PlatformOperationError('invalid_request', 'source thread/forum mismatch')
        canonical = self.database.local_community_surfaces.get_local_community_thread_for_surface(
            thread_surface.id
        )
        if canonical is None or canonical.local_community_id != source.local_community.id:
            raise PlatformOperationError('invalid_request', 'source canonical community mismatch')
        if source.source_kind == 'host_forum':
            if thread_surface.role != 'host':
                return LocalCommunityRuntimeResult('ignored', 'not_source_surface')
        elif (thread_surface.role != 'local_subscriber'
              or thread_surface.local_subscriber_id != source.local_subscriber.id):
            return LocalCommunityRuntimeResult('ignored', 'not_source_surface')
        if message_id == thread_surface.discord_starter_message_id:
            return LocalCommunityRuntimeResult('ignored', 'starter_message_already_handled')
        validate_message_reply(event.thread_context, event.reply_to)
        existing_surface = self.database.local_community_surfaces.get_local_community_message_surface_by_discord_message_id(
            message_id
        )
        if existing_surface is not None:
            if existing_surface.local_community_thread_surface_id != thread_surface.id:
                raise PlatformOperationError('invalid_request', 'foreign physical comment receipt')
            existing = self.database.local_community_surfaces.get_local_community_message_for_surface(
                existing_surface.id
            )
            if existing is None or existing.local_community_thread_id != canonical.id:
                raise PlatformOperationError('invalid_request', 'foreign canonical comment receipt')
            published = self.database.activitypub_objects.get_published_activity_object_by_object_id(
                existing.ap_object_id
            )
            # A mirrored comment in another forum is not the canonical source,
            # even when it shares the same local-subscriber physical role.
            if (published is not None and published.activity_id == existing.ap_activity_id
                and event.author.kind == 'human' and event.author.external_actor_id is not None
                and event.content.format == 'plain' and not event.content.attachments
                and published.actor_url == getattr(
                    self.database.users.get_user_by_discord_user_id(event.author.external_actor_id),
                    'actor_url', None,
                )
                and published.discord_channel_id == forum_id
                and published.discord_message_id == message_id
                and published.body_markdown == build_discord_comment_body(
                    author_name=event.author.display_name,
                    content=event.content.body,
                    bridge_prefix=self.content_publish_service.bridge_prefix,
                )):
                await fanout.fanout_normalized_message(
                    adapter=adapter, local_community=source.local_community,
                    thread_row=canonical, message_row=existing,
                    content=event.content.body, author_display_name=event.author.display_name,
                    source_forum_channel_id=forum_id,
                    include_host=source.source_kind == 'local_subscriber',
                )
            return LocalCommunityRuntimeResult('ignored', 'duplicate_message')

        # Root replies retain the selected source surface's actual starter;
        # known nested parents must match *this* physical thread surface.
        parent_ap = canonical.ap_object_id
        parent_discord = thread_surface.discord_starter_message_id
        if event.reply_to is not None:
            parent_id = discord_id_to_legacy_int(event.reply_to)
            if parent_id != parent_discord:
                known_starter = self.database.local_community_surfaces.get_local_community_thread_surface_by_starter_message_id(
                    parent_id
                )
                if known_starter is not None:
                    # A known starter from another thread cannot be treated
                    # as an unknown reference and flattened onto this root.
                    raise PlatformOperationError('invalid_request', 'foreign starter reply parent')
                parent_surface = self.database.local_community_surfaces.get_local_community_message_surface_by_discord_message_id(
                    parent_id
                )
                if parent_surface is not None:
                    if parent_surface.local_community_thread_surface_id != thread_surface.id:
                        raise PlatformOperationError('invalid_request', 'foreign reply parent surface')
                    parent_message = self.database.local_community_surfaces.get_local_community_message_for_surface(
                        parent_surface.id
                    )
                    if parent_message is None or parent_message.local_community_thread_id != canonical.id:
                        raise PlatformOperationError('invalid_request', 'foreign canonical reply parent')
                    parent_ap = parent_message.ap_object_id
                    parent_discord = parent_id
                # An unmapped Discord reference falls back to the canonical
                # root, matching the legacy local-community comment contract.
        published = await self.content_publish_service.publish_normalized_comment_to_community(
            event=event, community_actor_url=source.local_community.actor_url,
            parent_object_id=parent_ap,
            publish_call=self.content_publish_service.fedify_gateway.publish_local_community_content,
            target_inbox_urls=self.content_publish_service._allowed_local_follower_inboxes(
                source.local_community.actor_url
            ),
            notice_adapter=adapter,
        )
        if published.status != 'published':
            return LocalCommunityRuntimeResult(published.status, published.reason)
        if source.source_kind == 'host_forum':
            message_row = self.database.local_community_content.create_local_community_message(
                local_community_thread_id=canonical.id,
                discord_message_id=message_id,
                ap_activity_id=published.activity_id, ap_object_id=published.object_id,
                parent_ap_object_id=parent_ap,
                parent_discord_message_id=parent_discord,
                direction='discord_to_ap',
            )
        else:
            message_row = self.database.local_community_content.create_local_community_message_canonical(
                local_community_thread_id=canonical.id,
                ap_activity_id=published.activity_id, ap_object_id=published.object_id,
                parent_ap_object_id=parent_ap, direction='discord_to_ap',
            )
            self.database.local_community_surfaces.create_local_community_message_surface(
                local_community_message_id=message_row.id,
                local_community_thread_surface_id=thread_surface.id,
                discord_forum_channel_id=forum_id, discord_message_id=message_id,
                parent_discord_message_id=parent_discord,
                role='local_subscriber', local_subscriber_id=source.local_subscriber.id,
            )
        await fanout.fanout_normalized_message(
            adapter=adapter, local_community=source.local_community,
            thread_row=canonical, message_row=message_row,
            content=event.content.body, author_display_name=event.author.display_name,
            source_forum_channel_id=forum_id,
            include_host=source.source_kind == 'local_subscriber',
        )
        return LocalCommunityRuntimeResult(
            'published', 'published', published.activity_id, published.object_id,
        )
