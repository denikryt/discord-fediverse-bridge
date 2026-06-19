"""Remote ActivityPub create handling for local communities.

This module owns inbound post/comment mirroring into Discord, including the
generic ActivityPub message mapping write that must happen only after Discord
send succeeds.  Federation relay ordering and duplicate semantics are preserved
from the original runtime path.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from ..content_sync.inbound_references import build_message_reference
from ..db import Database
from ..inbound_activity_outcomes import InboundActivityOutcome
from ..runtime_ports import DiscordDeliveryClientPort
from .delivery_mapping import get_local_community_thread_for_ap_object
from .discord_surface_fanout import LocalCommunitySurfaceFanout
from .federation_fanout import LocalCommunityFederationFanout
from .rendering import (
    format_inbound_comment_body,
    format_inbound_post_body,
    unpack_created_thread,
)
from .reply_mapping import resolve_inbound_reply_target

if TYPE_CHECKING:
    from ..activitypub_handlers import HandlerResult


class LocalCommunityInboundCreateHandler:
    """Handle remote ActivityPub creates targeting local communities."""

    def __init__(
        self,
        *,
        database: Database,
        surface_fanout: LocalCommunitySurfaceFanout,
        federation_fanout: LocalCommunityFederationFanout,
        discord_client_provider: Callable[[], DiscordDeliveryClientPort | None],
    ) -> None:
        """Store only dependencies needed for inbound Discord mirror creation."""
        self.database = database
        self.surface_fanout = surface_fanout
        self.federation_fanout = federation_fanout
        self._discord_client_provider = discord_client_provider

    async def handle_post(
        self, event: object, runtime: object
    ) -> HandlerResult:
        """Mirror one remote top-level post into a new Discord forum thread."""
        from ..activitypub_handlers import HandlerResult as _HandlerResult

        local_community = (
            self.database.local_communities.get_local_community_by_actor_url(
                getattr(event, "community_actor_id")
            )
        )
        if local_community is None:
            return _HandlerResult(
                status="skipped",
                outcome=InboundActivityOutcome.IGNORED_UNKNOWN_LOCAL_COMMUNITY,
                detail="unknown local community",
            )
        existing = get_local_community_thread_for_ap_object(
            self.database, getattr(getattr(event, "object"), "ap_id")
        )
        if existing is not None:
            await self.surface_fanout.fanout_thread_to_local_subscribers(
                local_community=local_community,
                thread_row=existing,
                title=getattr(getattr(event, "object"), "title", None)
                or "Untitled remote post",
                content=format_inbound_post_body(event),
                author_display_name=None,
                source_forum_channel_id=None,
            )
            await self.federation_fanout.relay_create(
                event=event,
                local_community=local_community,
                object_kind="post",
            )
            return _HandlerResult(
                status="skipped",
                outcome=InboundActivityOutcome.IGNORED_ALREADY_APPLIED,
                detail="post already mapped",
            )
        remote_subscriber = self.database.remote_subscribers.get_remote_subscriber(
            local_community_id=getattr(local_community, "id"),
            remote_actor_id=getattr(event, "actor_id"),
        )
        if (
            remote_subscriber is None
            or getattr(remote_subscriber, "status") != "accepted"
        ):
            return _HandlerResult(
                status="skipped",
                outcome=InboundActivityOutcome.IGNORED_ACTOR_NOT_SUBSCRIBER,
                detail="remote actor is not an accepted remote subscriber",
            )
        bot = self._discord_client_provider()
        if bot is None:
            raise RuntimeError(
                "LocalCommunityRuntime requires bot for inbound Discord delivery"
            )

        await bot.wait_until_bridge_ready()
        forum_channel = await bot.fetch_forum_channel(
            getattr(local_community, "discord_forum_channel_id")
        )
        thread_title = (
            getattr(getattr(event, "object"), "title", None) or "Untitled remote post"
        )
        created = await forum_channel.create_thread(
            name=thread_title,
            content=format_inbound_post_body(event),
        )
        created_thread, starter_message = unpack_created_thread(created)
        thread_row = (
            self.database.local_community_content.create_local_community_thread(
                local_community_id=getattr(local_community, "id"),
                discord_thread_id=getattr(created_thread, "id"),
                discord_starter_message_id=getattr(starter_message, "id"),
                ap_activity_id=getattr(event, "delivery_id"),
                ap_object_id=getattr(getattr(event, "object"), "ap_id"),
                direction="ap_to_discord",
                origin_kind="remote_follower",
            )
        )
        await self.surface_fanout.fanout_thread_to_local_subscribers(
            local_community=local_community,
            thread_row=thread_row,
            title=thread_title,
            content=format_inbound_post_body(event),
            author_display_name=None,
            source_forum_channel_id=None,
        )
        await self.federation_fanout.relay_create(
            event=event,
            local_community=local_community,
            object_kind="post",
        )
        return _HandlerResult(
            status="processed",
            outcome=InboundActivityOutcome.APPLIED,
            detail="remote post created thread",
        )

    async def handle_comment(
        self, event: object, runtime: object
    ) -> HandlerResult:
        """Mirror one remote comment into the mapped Discord thread."""
        from ..activitypub_handlers import HandlerResult as _HandlerResult

        local_community = (
            self.database.local_communities.get_local_community_by_actor_url(
                getattr(event, "community_actor_id")
            )
        )
        if local_community is None:
            return _HandlerResult(
                status="skipped",
                outcome=InboundActivityOutcome.IGNORED_UNKNOWN_LOCAL_COMMUNITY,
                detail="unknown local community",
            )
        existing_message = self.database.local_community_content.get_local_community_message_by_ap_object_id(
            getattr(getattr(event, "object"), "ap_id")
        )
        if existing_message is not None:
            existing_thread = (
                self.database.local_community_content.get_local_community_thread_by_id(
                    getattr(existing_message, "local_community_thread_id")
                )
            )
            if existing_thread is not None:
                await self.surface_fanout.fanout_message_to_local_subscribers(
                    local_community=local_community,
                    thread_row=existing_thread,
                    message_row=existing_message,
                    content=format_inbound_comment_body(event),
                    author_display_name=None,
                    source_forum_channel_id=None,
                )
            await self.federation_fanout.relay_create(
                event=event,
                local_community=local_community,
                object_kind="comment",
            )
            return _HandlerResult(
                status="skipped",
                outcome=InboundActivityOutcome.IGNORED_ALREADY_APPLIED,
                detail="comment already mapped",
            )
        remote_subscriber = self.database.remote_subscribers.get_remote_subscriber(
            local_community_id=getattr(local_community, "id"),
            remote_actor_id=getattr(event, "actor_id"),
        )
        if (
            remote_subscriber is None
            or getattr(remote_subscriber, "status") != "accepted"
        ):
            return _HandlerResult(
                status="skipped",
                outcome=InboundActivityOutcome.IGNORED_ACTOR_NOT_SUBSCRIBER,
                detail="remote actor is not an accepted remote subscriber",
            )

        thread_row = self.database.local_community_content.get_local_community_thread_by_ap_object_id(
            getattr(getattr(event, "object"), "post_ap_id")
        )
        if thread_row is None:
            return _HandlerResult(
                status="skipped",
                outcome=InboundActivityOutcome.IGNORED_UNMAPPED_CONTEXT,
                detail="comment parent post is not mapped",
            )
        bot = self._discord_client_provider()
        if bot is None:
            raise RuntimeError(
                "LocalCommunityRuntime requires bot for inbound Discord delivery"
            )

        self.database.personal_feed_conversations.retain_community_create(
            activity_id=getattr(event, "source_activity_id", None)
            or getattr(event, "delivery_id"),
            object_id=getattr(getattr(event, "object"), "ap_id"),
            root_object_id=getattr(getattr(event, "object"), "post_ap_id"),
            parent_object_id=getattr(getattr(event, "object"), "parent_ap_id"),
            actor_id=getattr(event, "actor_id"),
            actor_name=getattr(getattr(event, "object"), "author_name", None),
            content_markdown=getattr(getattr(event, "object"), "body_markdown", None)
            or "",
            published_at=getattr(getattr(event, "object"), "published_at", None),
            source_activity=getattr(event, "source_activity_json", None),
        )

        host_thread_surface = self.database.local_community_surfaces.get_host_local_community_thread_surface(
            getattr(thread_row, "id")
        )
        if host_thread_surface is None:
            return _HandlerResult(
                status="skipped",
                outcome=InboundActivityOutcome.IGNORED_UNMAPPED_CONTEXT,
                detail="comment thread host surface not mapped",
            )
        discord_thread = await bot.get_thread_by_id(
            getattr(host_thread_surface, "discord_thread_id")
        )
        parent_discord_message_id = resolve_inbound_reply_target(
            database=self.database,
            parent_ap_object_id=getattr(getattr(event, "object"), "parent_ap_id"),
            thread_row=thread_row,
        )
        send_kwargs: dict[str, object] = {}
        if parent_discord_message_id is not None:
            # The shared helper keeps the reference contract identical to the
            # remote-subscription mode and avoids duck-typed reply objects.
            send_kwargs["reference"] = build_message_reference(
                discord_thread=discord_thread,
                message_id=parent_discord_message_id,
            )
        created_message = await discord_thread.send(
            format_inbound_comment_body(event), **send_kwargs
        )
        message_row = (
            self.database.local_community_content.create_local_community_message(
                local_community_thread_id=getattr(thread_row, "id"),
                discord_message_id=getattr(created_message, "id"),
                ap_activity_id=getattr(event, "delivery_id"),
                ap_object_id=getattr(getattr(event, "object"), "ap_id"),
                parent_ap_object_id=getattr(getattr(event, "object"), "parent_ap_id"),
                parent_discord_message_id=parent_discord_message_id,
                direction="ap_to_discord",
            )
        )
        self._persist_inbound_activitypub_message_mapping(
            event=event,
            discord_thread_id=getattr(discord_thread, "id"),
            discord_message_id=getattr(created_message, "id"),
        )
        await self.surface_fanout.fanout_message_to_local_subscribers(
            local_community=local_community,
            thread_row=thread_row,
            message_row=message_row,
            content=format_inbound_comment_body(event),
            author_display_name=None,
            source_forum_channel_id=None,
        )
        await self.federation_fanout.relay_create(
            event=event,
            local_community=local_community,
            object_kind="comment",
        )
        return _HandlerResult(
            status="processed",
            outcome=InboundActivityOutcome.APPLIED,
            detail="remote comment created message",
        )

    def _persist_inbound_activitypub_message_mapping(
        self,
        *,
        event: object,
        discord_thread_id: int,
        discord_message_id: int,
    ) -> None:
        """Persist the generic AP mapping for one mirrored inbound comment.

        Local-community placement is still owned by `local_community_messages`,
        but the gateway resolves later direct replies by reading
        `message_mappings.object_id`.  Persisting this row only after Discord
        send succeeds guarantees the gateway never resolves a parent that Python
        cannot place back into Discord.
        """
        object_payload = getattr(event, "object")
        object_id = getattr(object_payload, "ap_id")
        activity_id = getattr(event, "delivery_id")

        # ActivityPub deliveries are replayable.  Check both unique AP columns
        # before inserting so a duplicate relay cannot turn a successfully
        # mirrored comment into an integrity error.
        if (
            self.database.message_mappings.get_message_mapping_by_object_id(object_id)
            is not None
        ):
            return
        if (
            self.database.message_mappings.get_message_mapping_by_activity_id(
                activity_id
            )
            is not None
        ):
            return

        self.database.message_mappings.create_message_mapping(
            source_platform="activitypub",
            source_id=object_id,
            activity_id=activity_id,
            object_id=object_id,
            actor_url=getattr(event, "actor_id"),
            community_actor_url=getattr(event, "community_actor_id"),
            discord_channel_id=discord_thread_id,
            discord_message_id=discord_message_id,
        )
