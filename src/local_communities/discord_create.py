"""Discord-authored local-community create orchestration.

This module owns host and local-subscriber Discord create paths that publish
canonical ActivityPub objects and repair local Discord surfaces on duplicates.
The runtime facade still decides whether an event belongs to local-community
mode before delegating here.
"""

from __future__ import annotations

from ..content_publish_service import ContentPublishService
from ..db import Database
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
