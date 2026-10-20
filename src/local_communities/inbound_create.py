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
from ..discord_legacy_projection import discord_id_to_legacy_int
from ..platform_types import (ChannelRef, ContentDocument, CreateAttempt,
                              DeliveryPresentation, MessageRef, PlatformOperationError, ThreadRef,
                              validate_created_receipt)
from ..runtime_ports import PlatformAdapterPort
from .discord_fanout import LocalCommunityDiscordFanout
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
        self._adapter: PlatformAdapterPort | None = None

    def bind_discord_adapter(self, adapter: PlatformAdapterPort) -> None:
        """Use the registered connector for production AP-originated physical delivery."""
        self._adapter = adapter

    def _host_forum(self, local_community: object) -> ChannelRef | None:
        """Authorize current host placement and guild before an outbound Discord POST.

        Old persisted surfaces and untrusted AP payloads cannot authorize writes.
        """
        placement = self.database.platform_surfaces.get_discord_host_placement(local_community.id)
        if placement is None or not placement.external_guild_id:
            return None
        try:
            guild_id = int(placement.external_guild_id)
            channel_id = int(placement.external_channel_id)
            if guild_id <= 0 or channel_id <= 0 or not self.surface_fanout.policy_service.is_discord_guild_allowed(guild_id):
                return None
        except (TypeError, ValueError, AttributeError, RuntimeError):
            return None
        return ChannelRef('discord-primary', str(channel_id), 'forum', str(guild_id))

    def _host_unchanged(self, local_community: object, original: ChannelRef) -> bool:
        """Recheck host authority after a potentially slow physical API call."""
        current = self._host_forum(local_community)
        return (current is not None
                and current.external_channel_id == original.external_channel_id
                and current.external_guild_id == original.external_guild_id)

    def _origin_still_accepted(self, local_community: object, event: object) -> bool:
        """Prevent later sibling delivery or AP relay after a concurrent Undo(Follow)."""
        current = self.database.remote_subscribers.get_remote_subscriber(
            local_community_id=local_community.id, remote_actor_id=event.actor_id,
        )
        return current is not None and current.status == 'accepted'

    def _typed_fanout(self) -> LocalCommunityDiscordFanout:
        """Construct the existing physical fanout with no access to Discord SDK."""
        return LocalCommunityDiscordFanout(
            database=self.database, bot=None, policy_service=self.surface_fanout.policy_service,
        )

    async def _fanout_post(self, *, local_community: object, thread_row: object,
                           title: str, content: str) -> None:
        """Mirror a verified AP post to active subscribers through the selected transport."""
        if self._adapter is None:
            await self.surface_fanout.fanout_thread_to_local_subscribers(
                local_community=local_community, thread_row=thread_row, title=title,
                content=content, author_display_name=None, source_forum_channel_id=None,
            )
        else:
            await self._typed_fanout().fanout_normalized_thread(
                adapter=self._adapter, local_community=local_community, thread_row=thread_row,
                title=title, content=content, author_display_name=None,
                source_forum_channel_id=None, include_host=False,
            )

    async def _fanout_comment(self, *, local_community: object, thread_row: object,
                              message_row: object, content: str) -> None:
        """Mirror a verified AP reply using a target-specific parent receipt."""
        if self._adapter is None:
            await self.surface_fanout.fanout_message_to_local_subscribers(
                local_community=local_community, thread_row=thread_row,
                message_row=message_row, content=content,
                author_display_name=None, source_forum_channel_id=None,
            )
        else:
            await self._typed_fanout().fanout_normalized_message(
                adapter=self._adapter, local_community=local_community, thread_row=thread_row,
                message_row=message_row, content=content, author_display_name=None,
                source_forum_channel_id=None, include_host=False,
            )

    async def handle_post(self, event: object, runtime: object) -> HandlerResult:
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
        # A previously mapped object is not an authorization bypass. The
        # subscription and provenance checks must precede every side effect.
        subscriber = self.database.remote_subscribers.get_remote_subscriber(
            local_community_id=getattr(local_community, "id"),
            remote_actor_id=getattr(event, "actor_id"),
        )
        if subscriber is None or subscriber.status != "accepted":
            return _HandlerResult(
                status="skipped",
                outcome=InboundActivityOutcome.IGNORED_ACTOR_NOT_SUBSCRIBER,
                detail="remote actor is not an accepted remote subscriber",
            )
        existing = get_local_community_thread_for_ap_object(
            self.database, getattr(getattr(event, "object"), "ap_id")
        )
        if existing is not None:
            conflict = self._duplicate_post_conflict(event, local_community, existing)
            if conflict is not None:
                return _HandlerResult(
                    status="skipped",
                    outcome=InboundActivityOutcome.IGNORED_UNMAPPED_CONTEXT,
                    detail=conflict,
                )
            await self._fanout_post(
                local_community=local_community, thread_row=existing,
                title=getattr(event.object, "title", None) or "Untitled remote post",
                content=format_inbound_post_body(event),
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
        placement = self.database.platform_surfaces.get_discord_host_placement(
            getattr(local_community, "id")
        )
        if placement is None:
            return _HandlerResult(
                status="skipped",
                outcome=InboundActivityOutcome.IGNORED_UNMAPPED_CONTEXT,
                detail="local community has no Discord host placement",
            )
        if self._adapter is None:
            bot = self._discord_client_provider()
            if bot is None:
                raise RuntimeError("LocalCommunityRuntime requires bot for inbound Discord delivery")
        else:
            forum = self._host_forum(local_community)
            if forum is None:
                return _HandlerResult(
                    status="skipped", outcome=InboundActivityOutcome.IGNORED_UNMAPPED_CONTEXT,
                    detail="local community host is not an allowed Discord forum",
                )

        # Persist canonical source identity before a Discord POST: a process
        # crash after the POST must not leave a post with no origin snapshot.
        if not isinstance(getattr(event, "source_activity_json", None), dict):
            return _HandlerResult(
                status="skipped",
                outcome=InboundActivityOutcome.IGNORED_UNMAPPED_CONTEXT,
                detail="unverified_source",
            )
        source_id = getattr(event, "source_activity_id", None) or event.delivery_id
        self.database.local_community_relay.get_or_create_local_community_relay_source_activity(
            local_community_id=local_community.id,
            object_kind="post",
            operation="create",
            source_object_ap_id=event.object.ap_id,
            source_activity_id=source_id,
            source_announce_id=getattr(event, "source_announce_id", None),
            origin_remote_actor_id=event.actor_id,
            source_activity_json=event.source_activity_json,
        )
        thread_title = getattr(event.object, "title", None) or "Untitled remote post"
        if self._adapter is not None:
            # A physical receipt is evidence only when its destination matches
            # the current host placement, including known guild metadata.
            attempt = CreateAttempt(
                f"local-community-inbound-post:{local_community.id}:{event.object.ap_id}",
                'discord-primary', 'forum_thread', forum,
            )
            presentation = DeliveryPresentation()
            receipt = await self._adapter.create_forum_thread(
                forum, ContentDocument(format_inbound_post_body(event)),
                title=thread_title, presentation=presentation, attempt=attempt,
            )
            try:
                # Only a request-matched bot receipt can be committed. The
                # DTO also proves the starter belongs to this physical thread.
                validate_created_receipt(attempt, receipt, presentation=presentation)
                # Shared validation intentionally permits unknown refinable
                # channel metadata. A *host* receipt cannot: it must positively
                # verify this forum's kind and guild before SQLite adoption.
                if (receipt.thread.channel.kind != 'forum'
                    or receipt.thread.channel.external_guild_id != forum.external_guild_id):
                    raise PlatformOperationError('invalid_request', 'unverified host forum metadata')
            except (PlatformOperationError, ValueError, AttributeError, TypeError) as exc:
                # The POST may already have succeeded; a false receipt must
                # never authorize either SQLite mapping or a blind retry.
                raise PlatformOperationError('unknown_delivery', 'inbound host forum receipt mismatch') from exc
            discord_thread_id = discord_id_to_legacy_int(receipt.thread)
            starter_message_id = discord_id_to_legacy_int(receipt.starter)
            if not self._host_unchanged(local_community, forum):
                raise PlatformOperationError('unknown_delivery', 'local community host changed during Create')
        else:
            await bot.wait_until_bridge_ready()
            forum_channel = await bot.fetch_forum_channel(int(placement.external_channel_id))
            created = await forum_channel.create_thread(
                name=thread_title, content=format_inbound_post_body(event),
            )
            created_thread, starter_message = unpack_created_thread(created)
            discord_thread_id = getattr(created_thread, 'id')
            starter_message_id = getattr(starter_message, 'id')
        thread_row = (
            self.database.local_community_content.create_local_community_thread(
                local_community_id=getattr(local_community, "id"),
                discord_thread_id=discord_thread_id,
                discord_starter_message_id=starter_message_id,
                ap_activity_id=getattr(event, "delivery_id"),
                ap_object_id=getattr(getattr(event, "object"), "ap_id"),
                direction="ap_to_discord",
                origin_kind="remote_follower",
            )
        )
        if not self._origin_still_accepted(local_community, event):
            # Preserve the verified physical receipt, but suppress *new* side
            # effects if the accepted subscription disappeared during HTTP.
            return _HandlerResult(status="skipped", outcome=InboundActivityOutcome.IGNORED_ACTOR_NOT_SUBSCRIBER,
                                  detail="remote subscriber was revoked during delivery")
        await self._fanout_post(
            local_community=local_community, thread_row=thread_row,
            title=thread_title, content=format_inbound_post_body(event),
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

    async def handle_comment(self, event: object, runtime: object) -> HandlerResult:
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
        # An accepted Follow is required for replays too, not just new posts.
        subscriber = self.database.remote_subscribers.get_remote_subscriber(
            local_community_id=getattr(local_community, "id"),
            remote_actor_id=getattr(event, "actor_id"),
        )
        if subscriber is None or subscriber.status != "accepted":
            return _HandlerResult(
                status="skipped",
                outcome=InboundActivityOutcome.IGNORED_ACTOR_NOT_SUBSCRIBER,
                detail="remote actor is not an accepted remote subscriber",
            )
        existing_message = self.database.local_community_content.get_local_community_message_by_ap_object_id(
            getattr(getattr(event, "object"), "ap_id")
        )
        if existing_message is not None:
            conflict = self._duplicate_comment_conflict(event, local_community, existing_message)
            if conflict is not None:
                return _HandlerResult(
                    status="skipped",
                    outcome=InboundActivityOutcome.IGNORED_UNMAPPED_CONTEXT,
                    detail=conflict,
                )
            existing_thread = (
                self.database.local_community_content.get_local_community_thread_by_id(
                    getattr(existing_message, "local_community_thread_id")
                )
            )
            host_surface = self.database.local_community_surfaces.get_host_local_community_message_surface(
                existing_message.id
            )
            host_thread = self.database.local_community_surfaces.get_host_local_community_thread_surface(
                existing_message.local_community_thread_id
            )
            if existing_thread is None or host_surface is None or host_thread is None:
                return _HandlerResult(
                    status="skipped",
                    outcome=InboundActivityOutcome.IGNORED_UNMAPPED_CONTEXT,
                    detail="mapping_conflict",
                )
            # A crash between the canonical receipt and generic mapping must
            # be recoverable, but only from the persisted *host* SDK receipt.
            # The repository rejects reusing a generic AP key for another ID.
            try:
                self._persist_inbound_activitypub_message_mapping(
                    event=event,
                    discord_thread_id=host_thread.discord_thread_id,
                    discord_message_id=host_surface.discord_message_id,
                )
            except ValueError:
                return _HandlerResult(
                    status="skipped",
                    outcome=InboundActivityOutcome.IGNORED_UNMAPPED_CONTEXT,
                    detail="mapping_conflict",
                )
            await self._fanout_comment(
                local_community=local_community, thread_row=existing_thread,
                message_row=existing_message, content=format_inbound_comment_body(event),
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

        thread_row = self.database.local_community_content.get_local_community_thread_by_ap_object_id(
            getattr(getattr(event, "object"), "post_ap_id")
        )
        if thread_row is None:
            return _HandlerResult(
                status="skipped",
                outcome=InboundActivityOutcome.IGNORED_UNMAPPED_CONTEXT,
                detail="comment parent post is not mapped",
            )
        if self._adapter is None:
            bot = self._discord_client_provider()
            if bot is None:
                raise RuntimeError("LocalCommunityRuntime requires bot for inbound Discord delivery")
        else:
            forum = self._host_forum(local_community)
            if forum is None:
                return _HandlerResult(status="skipped", outcome=InboundActivityOutcome.IGNORED_UNMAPPED_CONTEXT,
                                      detail="local community host is not an allowed Discord forum")

        # Authorize the owning canonical thread and actual host placement
        # before retaining any durable AP comment. An invalid nested parent
        # must not poison idempotent re-admission when its mapping appears.
        if thread_row.local_community_id != local_community.id:
            return _HandlerResult(
                status="skipped", outcome=InboundActivityOutcome.IGNORED_UNMAPPED_CONTEXT,
                detail="comment post belongs to another local community",
            )
        host_thread_surface = self.database.local_community_surfaces.get_host_local_community_thread_surface(
            getattr(thread_row, "id")
        )
        if host_thread_surface is None:
            return _HandlerResult(
                status="skipped", outcome=InboundActivityOutcome.IGNORED_UNMAPPED_CONTEXT,
                detail="comment thread host surface not mapped",
            )
        if self._adapter is not None:
            if host_thread_surface.discord_forum_channel_id != int(forum.external_channel_id):
                return _HandlerResult(
                    status="skipped", outcome=InboundActivityOutcome.IGNORED_UNMAPPED_CONTEXT,
                    detail="host surface no longer matches placement",
                )
            parent_ap_id = getattr(event.object, 'parent_ap_id')
            if parent_ap_id is None or parent_ap_id == thread_row.ap_object_id:
                parent_discord_message_id = host_thread_surface.discord_starter_message_id
            else:
                parent_row = self.database.local_community_content.get_local_community_message_by_ap_object_id(
                    parent_ap_id
                )
                if parent_row is None or parent_row.local_community_thread_id != thread_row.id:
                    return _HandlerResult(
                        status="skipped", outcome=InboundActivityOutcome.IGNORED_UNMAPPED_CONTEXT,
                        detail="nested reply parent is absent or belongs to another thread",
                    )
                parent_surface = self.database.local_community_surfaces.get_local_community_message_surface(
                    local_community_message_id=parent_row.id,
                    local_community_thread_surface_id=host_thread_surface.id,
                )
                if parent_surface is None or parent_surface.role != 'host':
                    return _HandlerResult(
                        status="skipped", outcome=InboundActivityOutcome.IGNORED_UNMAPPED_CONTEXT,
                        detail="nested reply parent has no verified host surface",
                    )
                parent_discord_message_id = parent_surface.discord_message_id
            if parent_discord_message_id is None or parent_discord_message_id <= 0:
                return _HandlerResult(
                    status="skipped", outcome=InboundActivityOutcome.IGNORED_UNMAPPED_CONTEXT,
                    detail="host parent physical message is unresolved",
                )
        else:
            # Historical direct-call tests still use the SDK projection;
            # Stage 03 cleanup will migrate these consumers to the adapter.
            parent_discord_message_id = resolve_inbound_reply_target(
                database=self.database,
                parent_ap_object_id=getattr(event.object, 'parent_ap_id'),
                thread_row=thread_row,
            )

        admission = self.database.personal_feed_conversations.retain_community_create(
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
        if admission.status != "created":
            return _HandlerResult(
                status="skipped",
                outcome=InboundActivityOutcome.IGNORED_UNMAPPED_CONTEXT,
                detail=admission.status,
            )

        if self._adapter is not None:
            destination = ThreadRef(forum, str(host_thread_surface.discord_thread_id))
            reply = (MessageRef('discord-primary', destination.external_thread_id,
                                str(parent_discord_message_id))
                     if parent_discord_message_id is not None else None)
            attempt = CreateAttempt(
                f"local-community-inbound-comment:{local_community.id}:{event.object.ap_id}",
                'discord-primary', 'message', destination,
            )
            presentation = DeliveryPresentation()
            receipt = await self._adapter.send_message(
                destination, ContentDocument(format_inbound_comment_body(event)),
                reply_to=reply, presentation=presentation, attempt=attempt,
            )
            try:
                validate_created_receipt(attempt, receipt, presentation=presentation)
            except (PlatformOperationError, ValueError, AttributeError, TypeError) as exc:
                # A successful but unverified response may have sent already.
                raise PlatformOperationError('unknown_delivery', 'inbound host message receipt mismatch') from exc
            discord_message_id = discord_id_to_legacy_int(receipt.message)
            if not self._host_unchanged(local_community, forum):
                raise PlatformOperationError('unknown_delivery', 'local community host changed during reply')
            discord_thread_id = host_thread_surface.discord_thread_id
        else:
            discord_thread = await bot.get_thread_by_id(host_thread_surface.discord_thread_id)
            send_kwargs: dict[str, object] = {}
            if parent_discord_message_id is not None:
                send_kwargs['reference'] = build_message_reference(
                    discord_thread=discord_thread, message_id=parent_discord_message_id,
                )
            created_message = await discord_thread.send(
                format_inbound_comment_body(event), **send_kwargs,
            )
            discord_message_id = getattr(created_message, 'id')
            discord_thread_id = getattr(discord_thread, 'id')
        message_row = (
            self.database.local_community_content.create_local_community_message(
                local_community_thread_id=getattr(thread_row, "id"),
                discord_message_id=discord_message_id,
                ap_activity_id=getattr(event, "delivery_id"),
                ap_object_id=getattr(getattr(event, "object"), "ap_id"),
                parent_ap_object_id=getattr(getattr(event, "object"), "parent_ap_id"),
                parent_discord_message_id=parent_discord_message_id,
                direction="ap_to_discord",
            )
        )
        self._persist_inbound_activitypub_message_mapping(
            event=event,
            discord_thread_id=discord_thread_id,
            discord_message_id=discord_message_id,
        )
        if not self._origin_still_accepted(local_community, event):
            # A confirmed host message remains mapped, but revoked authors do
            # not authorize further subscriber copies or federation relay.
            return _HandlerResult(status="skipped", outcome=InboundActivityOutcome.IGNORED_ACTOR_NOT_SUBSCRIBER,
                                  detail="remote subscriber was revoked during delivery")
        await self._fanout_comment(
            local_community=local_community, thread_row=thread_row,
            message_row=message_row, content=format_inbound_comment_body(event),
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

    def _duplicate_post_conflict(self, event: object, community: object, existing: object) -> str | None:
        """Validate immutable AP post/source identity before replaying side effects.

        Historical posts without a trustworthy persisted source must be
        skipped: accepting a replay as proof would let a forged author relay.
        """
        if (existing.local_community_id != community.id
            or existing.ap_activity_id != event.delivery_id
            or existing.direction != "ap_to_discord"
            or existing.origin_kind != "remote_follower"):
            return "mapping_conflict"
        source_id = getattr(event, "source_activity_id", None) or event.delivery_id
        source = self.database.local_community_relay.get_local_community_relay_source_activity(
            local_community_id=community.id,
            operation="create",
            source_object_ap_id=event.object.ap_id,
            source_activity_id=source_id,
        )
        if source is None or not isinstance(getattr(event, "source_activity_json", None), dict):
            # MULTIPLATFORM_STAGE03_TEMP_UNVERIFIED_AP_DUPLICATE: A legacy mapped AP object without independent persisted actor/payload provenance cannot safely replay Discord fan-out or ActivityPub relay; Stage 04 must reconcile original proof before enabling recovery.
            return "unverified_duplicate"
        if (source.object_kind != "post"
            or source.origin_remote_actor_id != event.actor_id
            or source.source_announce_id != getattr(event, "source_announce_id", None)
            or source.source_activity_json != event.source_activity_json):
            return "mapping_conflict"
        return None

    def _duplicate_comment_conflict(self, event: object, community: object, existing: object) -> str | None:
        """Require canonical message and previously retained source to match exactly."""
        thread = self.database.local_community_content.get_local_community_thread_by_id(
            existing.local_community_thread_id
        )
        if (thread is None or thread.local_community_id != community.id
            or thread.ap_object_id != event.object.post_ap_id
            or existing.ap_activity_id != event.delivery_id
            or existing.parent_ap_object_id != event.object.parent_ap_id
            or existing.direction != "ap_to_discord"):
            return "mapping_conflict"
        # Do not insert a new source row while allegedly verifying a duplicate.
        previous = self.database.personal_feed_conversations.get_inbound_object(event.object.ap_id)
        if previous is None or previous.source_activity_json is None:
            # MULTIPLATFORM_STAGE03_TEMP_UNVERIFIED_AP_DUPLICATE: A legacy mapped AP object without independent persisted actor/payload provenance cannot safely replay Discord fan-out or ActivityPub relay; Stage 04 must reconcile original proof before enabling recovery.
            return "unverified_duplicate"
        admission = self.database.personal_feed_conversations.retain_community_create(
            activity_id=getattr(event, "source_activity_id", None) or event.delivery_id,
            object_id=event.object.ap_id,
            root_object_id=event.object.post_ap_id,
            parent_object_id=event.object.parent_ap_id,
            actor_id=event.actor_id,
            actor_name=getattr(event.object, "author_name", None),
            content_markdown=getattr(event.object, "body_markdown", None) or "",
            published_at=getattr(event.object, "published_at", None),
            source_activity=getattr(event, "source_activity_json", None),
        )
        return None if admission.status == "duplicate" else "mapping_conflict"

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

        # Repository insertion is transactionally idempotent; an AP key reused
        # with a different physical Discord receipt must fail, never disappear.
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
