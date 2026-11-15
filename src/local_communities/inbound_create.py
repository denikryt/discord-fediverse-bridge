"""Remote ActivityPub create handling for local communities.

This module owns inbound post/comment mirroring into Discord, including the
generic ActivityPub message mapping write that must happen only after Discord
send succeeds.  Federation relay ordering and duplicate semantics are preserved
from the original runtime path.
"""

from __future__ import annotations

from collections.abc import Callable
import logging
from typing import TYPE_CHECKING

from ..content_sync.inbound_references import build_message_reference
from ..db import Database
from ..bridge_policy import BridgePolicyService
from .actor_authorization import ActorFetch, authorize_inbound_source
from ..db.repositories.local_community_content import HostIdentityConflict
from ..inbound_activity_outcomes import InboundActivityOutcome
from ..runtime_ports import DiscordDeliveryClientPort
from .discord_surface_fanout import LocalCommunitySurfaceFanout
from .federation_fanout import LocalCommunityFederationFanout
from .rendering import (
    format_inbound_comment_body,
    format_inbound_post_body,
    unpack_created_thread,
)
from .reply_mapping import resolve_inbound_reply_target

logger = logging.getLogger(__name__)

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
        bridge_policy_service: BridgePolicyService,
        actor_fetcher: ActorFetch | None = None,
    ) -> None:
        """Store only dependencies needed for inbound Discord mirror creation."""
        self.database = database
        self.surface_fanout = surface_fanout
        self.federation_fanout = federation_fanout
        self._discord_client_provider = discord_client_provider
        self.bridge_policy_service = bridge_policy_service
        self.actor_fetcher = actor_fetcher

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
        # Authorize the specific verified tenant before duplicate/parent reads
        # or any host, mapping or relay side effect. A Service relationship
        # never grants rights based solely on a Person's hostname.
        authorized = await authorize_inbound_source(
            event=event,
            local_community_id=getattr(local_community, "id"),
            database=self.database,
            policy=self.bridge_policy_service,
            fetch_actor=self.actor_fetcher,
        )
        if authorized is None:
            return _HandlerResult(
                status="skipped",
                outcome=InboundActivityOutcome.IGNORED_ACTOR_NOT_SUBSCRIBER,
                detail="remote actor has no verified accepted subscriber relationship",
            )
        object_payload = getattr(event, "object")
        tenant_id = getattr(local_community, "id")
        first_title = getattr(object_payload, "title", None) or "Untitled remote post"
        first_content = format_inbound_post_body(event)
        # The SQLite writer lock protects INSERT + CAS claim before any Discord
        # REST request. Freeze the first rendered body under that lock as well:
        # a later Announce may retry missing mirrors, but never with a new body.
        try:
            reservation = self.database.local_community_content.reserve_inbound_thread(
                local_community_id=tenant_id,
                ap_activity_id=getattr(event, "delivery_id"),
                ap_object_id=getattr(object_payload, "ap_id"),
                source_owner_actor_id=authorized.author_actor_id,
                origin_subscriber_actor_id=authorized.subscriber_actor_id,
                inbound_create_snapshot={"title": first_title, "content": first_content},
            )
        except HostIdentityConflict:
            return _HandlerResult(
                status="skipped",
                outcome=InboundActivityOutcome.IGNORED_ALREADY_APPLIED,
                detail="conflicting original Group post owner or origin",
            )
        thread_row = reservation.canonical
        # Authoritative projection comes from the initial successful SQLite
        # reservation, not the current signed duplicate's mutable object.
        original = thread_row.inbound_create_snapshot_json
        title, content = original["title"], original["content"]
        if reservation.attempt_id is None:
            # Only a confirmed host surface can be mirrored or relayed. A
            # concurrent sending or unknown attempt must never be retried.
            if reservation.state == "delivered":
                await self.surface_fanout.fanout_thread_to_local_subscribers(
                    local_community=local_community, thread_row=thread_row,
                    title=title, content=content,
                    author_display_name=None, source_forum_channel_id=None,
                )
                await self.federation_fanout.relay_create(
                    event=event, local_community=local_community,
                    object_kind="post",
                    origin_subscriber_actor_id=thread_row.origin_subscriber_actor_id,
                    source_owner_actor_id=thread_row.source_owner_actor_id,
                )
            return _HandlerResult(
                status="skipped",
                outcome=InboundActivityOutcome.IGNORED_ALREADY_APPLIED,
                detail=f"Group post host attempt {reservation.state}",
            )
        bot = self._discord_client_provider()
        if bot is None:
            self.database.local_community_content.finish_inbound_host_attempt(
                canonical_id=thread_row.id, attempt_id=reservation.attempt_id,
                kind="post", state="failed",
            )
            raise RuntimeError("LocalCommunityRuntime requires bot for inbound Discord delivery")

        # Failures before create_thread are provably unsent and retryable. Once
        # the method is invoked an exception may mean the response was lost
        # after Discord committed; keep unknown without a blind resend.
        try:
            await bot.wait_until_bridge_ready()
            forum_channel = await bot.fetch_forum_channel(
                getattr(local_community, "discord_forum_channel_id")
            )
        except Exception:
            self.database.local_community_content.finish_inbound_host_attempt(
                canonical_id=thread_row.id, attempt_id=reservation.attempt_id,
                kind="post", state="failed",
            )
            raise
        try:
            created = await forum_channel.create_thread(name=title, content=content, suppress_embeds=True)
            created_thread, starter_message = unpack_created_thread(created)
        except Exception:
            self.database.local_community_content.finish_inbound_host_attempt(
                canonical_id=thread_row.id, attempt_id=reservation.attempt_id,
                kind="post", state="unknown",
            )
            logger.warning("Ambiguous local Group host post delivery tenant=%s canonical=%s", tenant_id, thread_row.id)
            return _HandlerResult(
                status="skipped", outcome=InboundActivityOutcome.PROCESSING_FAILED,
                detail="host Discord Create outcome unknown; no automatic resend",
            )
        self.database.local_community_content.finish_inbound_host_attempt(
            canonical_id=thread_row.id, attempt_id=reservation.attempt_id,
            kind="post", state="delivered",
            discord_thread_id=getattr(created_thread, "id"),
            discord_message_id=getattr(starter_message, "id"),
        )
        # This post's canonical placement and host surface are now durable.
        await self.surface_fanout.fanout_thread_to_local_subscribers(
            local_community=local_community, thread_row=thread_row,
            title=title, content=content,
            author_display_name=None, source_forum_channel_id=None,
        )
        await self.federation_fanout.relay_create(
            event=event, local_community=local_community, object_kind="post",
            origin_subscriber_actor_id=thread_row.origin_subscriber_actor_id,
            source_owner_actor_id=thread_row.source_owner_actor_id,
        )
        return _HandlerResult(
            status="processed", outcome=InboundActivityOutcome.APPLIED,
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
        # Authorize the specific verified tenant before duplicate/parent reads
        # or any host, mapping or relay side effect. A Service relationship
        # never grants rights based solely on a Person's hostname.
        authorized = await authorize_inbound_source(
            event=event,
            local_community_id=getattr(local_community, "id"),
            database=self.database,
            policy=self.bridge_policy_service,
            fetch_actor=self.actor_fetcher,
        )
        if authorized is None:
            return _HandlerResult(
                status="skipped",
                outcome=InboundActivityOutcome.IGNORED_ACTOR_NOT_SUBSCRIBER,
                detail="remote actor has no verified accepted subscriber relationship",
            )
        obj = getattr(event, "object")
        tenant_id = getattr(local_community, "id")
        first_content = format_inbound_comment_body(event)
        thread_row = self.database.local_community_content.get_local_community_thread_by_ap_object_id(
            getattr(obj, "post_ap_id"), local_community_id=tenant_id,
        )
        if thread_row is None:
            return _HandlerResult(
                status="skipped", outcome=InboundActivityOutcome.IGNORED_UNMAPPED_CONTEXT,
                detail="comment parent post is not mapped in target Group",
            )
        host_thread_surface = self.database.local_community_surfaces.get_host_local_community_thread_surface(
            getattr(thread_row, "id")
        )
        if host_thread_surface is None:
            return _HandlerResult(
                status="skipped", outcome=InboundActivityOutcome.IGNORED_UNMAPPED_CONTEXT,
                detail="comment thread host surface not mapped",
            )
        # The nested parent must be confirmed in this exact root, not merely
        # present under the same object ID in a neighboring local community.
        try:
            reservation = self.database.local_community_content.reserve_inbound_message(
                local_community_id=tenant_id,
                local_community_thread_id=getattr(thread_row, "id"),
                ap_activity_id=getattr(event, "delivery_id"),
                ap_object_id=getattr(obj, "ap_id"),
                parent_ap_object_id=getattr(obj, "parent_ap_id"),
                source_owner_actor_id=authorized.author_actor_id,
                origin_subscriber_actor_id=authorized.subscriber_actor_id,
                inbound_create_snapshot={"content": first_content},
            )
        except HostIdentityConflict:
            return _HandlerResult(
                status="skipped", outcome=InboundActivityOutcome.IGNORED_UNMAPPED_CONTEXT,
                detail="conflicting Group reply identity or foreign parent",
            )
        message_row = reservation.canonical
        # Replays may restore an unconfirmed sibling, but cannot use a newer
        # Create body to contradict the existing host message.
        content = message_row.inbound_create_snapshot_json["content"]
        if reservation.attempt_id is None:
            if reservation.state == "delivered":
                await self.surface_fanout.fanout_message_to_local_subscribers(
                    local_community=local_community, thread_row=thread_row,
                    message_row=message_row, content=content,
                    author_display_name=None, source_forum_channel_id=None,
                )
                await self.federation_fanout.relay_create(
                    event=event, local_community=local_community,
                    object_kind="comment",
                    origin_subscriber_actor_id=message_row.origin_subscriber_actor_id,
                    source_owner_actor_id=message_row.source_owner_actor_id,
                )
            return _HandlerResult(
                status="skipped", outcome=InboundActivityOutcome.IGNORED_ALREADY_APPLIED,
                detail=f"Group reply host attempt {reservation.state}",
            )
        bot = self._discord_client_provider()
        if bot is None:
            self.database.local_community_content.finish_inbound_host_attempt(
                canonical_id=message_row.id, attempt_id=reservation.attempt_id,
                kind="comment", state="failed",
            )
            raise RuntimeError("LocalCommunityRuntime requires bot for inbound Discord delivery")
        try:
            discord_thread = await bot.get_thread_by_id(
                getattr(host_thread_surface, "discord_thread_id")
            )
            parent_discord_message_id = resolve_inbound_reply_target(
                database=self.database,
                parent_ap_object_id=getattr(obj, "parent_ap_id"),
                thread_row=thread_row,
            )
            if parent_discord_message_id is None:
                raise HostIdentityConflict("confirmed parent Discord surface missing")
            send_kwargs = {
                "reference": build_message_reference(
                    discord_thread=discord_thread, message_id=parent_discord_message_id,
                )
            }
        except Exception:
            self.database.local_community_content.finish_inbound_host_attempt(
                canonical_id=message_row.id, attempt_id=reservation.attempt_id,
                kind="comment", state="failed",
            )
            raise
        try:
            created_message = await discord_thread.send(content, suppress_embeds=True, **send_kwargs)
        except Exception:
            self.database.local_community_content.finish_inbound_host_attempt(
                canonical_id=message_row.id, attempt_id=reservation.attempt_id,
                kind="comment", state="unknown",
            )
            logger.warning("Ambiguous local Group host reply delivery tenant=%s canonical=%s", tenant_id, message_row.id)
            return _HandlerResult(
                status="skipped", outcome=InboundActivityOutcome.PROCESSING_FAILED,
                detail="host Discord reply outcome unknown; no automatic resend",
            )
        self.database.local_community_content.finish_inbound_host_attempt(
            canonical_id=message_row.id, attempt_id=reservation.attempt_id,
            kind="comment", state="delivered",
            discord_message_id=getattr(created_message, "id"),
            parent_discord_message_id=parent_discord_message_id,
        )
        # The per-Group host surface is committed; a global AP reference is
        # optional compatibility data and cannot select a second tenant's host.
        self.database.personal_feed_conversations.retain_community_create(
            activity_id=getattr(event, "source_activity_id", None) or getattr(event, "delivery_id"),
            object_id=getattr(obj, "ap_id"),
            root_object_id=getattr(obj, "post_ap_id"),
            parent_object_id=getattr(obj, "parent_ap_id"),
            actor_id=getattr(event, "actor_id"),
            actor_name=getattr(obj, "author_name", None),
            content_markdown=getattr(obj, "body_markdown", None) or "",
            published_at=getattr(obj, "published_at", None),
            source_activity=getattr(event, "source_activity_json", None),
        )
        self._persist_inbound_activitypub_message_mapping(
            event=event, discord_thread_id=getattr(discord_thread, "id"),
            discord_message_id=getattr(created_message, "id"),
        )
        await self.surface_fanout.fanout_message_to_local_subscribers(
            local_community=local_community, thread_row=thread_row,
            message_row=message_row, content=content,
            author_display_name=None, source_forum_channel_id=None,
        )
        await self.federation_fanout.relay_create(
            event=event, local_community=local_community, object_kind="comment",
            origin_subscriber_actor_id=message_row.origin_subscriber_actor_id,
            source_owner_actor_id=message_row.source_owner_actor_id,
        )
        return _HandlerResult(
            status="processed", outcome=InboundActivityOutcome.APPLIED,
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
