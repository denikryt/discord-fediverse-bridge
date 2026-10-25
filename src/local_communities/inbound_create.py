"""Remote ActivityPub create handling for local communities.

This module owns inbound post/comment mirroring into Discord, including the
generic ActivityPub message mapping write that must happen only after Discord
send succeeds.  Federation relay ordering and duplicate semantics are preserved
from the original runtime path.
"""

from __future__ import annotations

from collections.abc import Callable
import json
from typing import TYPE_CHECKING

from ..content_sync.platform_fanout import PlatformFanoutCoordinator
from ..platform_types import (
    ChannelRef, ContentDocument, DeliveryPresentation, MessageRef, ThreadRef,
)
from ..runtime_ports import PlatformAdapterPort
from .discord_fanout import LocalCommunityDiscordFanout
from ..db import Database
from ..inbound_activity_outcomes import InboundActivityOutcome
from ..runtime_ports import DiscordDeliveryClientPort
from .delivery_mapping import get_local_community_thread_for_ap_object
from .discord_surface_fanout import LocalCommunitySurfaceFanout
from .federation_fanout import LocalCommunityFederationFanout
from .rendering import format_inbound_comment_body, format_inbound_post_body

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
        """Mirror a verified AP post only via the durable typed fanout."""
        if self._adapter is None:
            raise RuntimeError('Local Community fanout requires a bound platform adapter')
        await self._typed_fanout().fanout_normalized_thread(
            adapter=self._adapter, local_community=local_community,
            thread_row=thread_row, title=title, content=content,
            author_display_name=None, source_forum_channel_id=None,
            include_host=False,
        )

    async def _fanout_comment(self, *, local_community: object, thread_row: object,
                              message_row: object, content: str) -> None:
        """Mirror AP replies using the same typed per-destination parent policy."""
        if self._adapter is None:
            raise RuntimeError('Local Community fanout requires a bound platform adapter')
        await self._typed_fanout().fanout_normalized_message(
            adapter=self._adapter, local_community=local_community,
            thread_row=thread_row, message_row=message_row, content=content,
            author_display_name=None, source_forum_channel_id=None,
            include_host=False,
        )

    @staticmethod
    def _inbound_snapshot(event: object, *, kind: str) -> str:
        """Retain complete normalized inbound AP Create content before any POST."""
        obj = event.object
        payload = {
            'schemaVersion': 1, 'kind': kind, 'apObjectId': obj.ap_id,
            'authorActorId': event.actor_id,
            'authorName': getattr(obj, 'author_name', None) or 'remote',
            'bodyMarkdown': getattr(obj, 'body_markdown', None) or '',
            'title': getattr(obj, 'title', None),
            'parentApObjectId': getattr(obj, 'parent_ap_id', None),
            'postApObjectId': getattr(obj, 'post_ap_id', None),
            'communityActorId': event.community_actor_id,
            'url': getattr(obj, 'url', None) or '',
            'publishedAt': (getattr(obj, 'published_at', None).isoformat()
                            if getattr(obj, 'published_at', None) is not None else None),
        }
        return json.dumps(payload, sort_keys=True, separators=(',', ':'),
                          ensure_ascii=False, allow_nan=False)

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
            if (self._adapter is not None and
                self.database.local_community_surfaces.get_host_local_community_thread_surface(
                    existing.id) is None):
                return _HandlerResult(
                    status='skipped', outcome=InboundActivityOutcome.IGNORED_ALREADY_APPLIED,
                    detail='canonical post awaits verified host receipt',
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
            raise RuntimeError('Local AP post Create requires a bound platform adapter')
        forum = self._host_forum(local_community)
        if forum is None:
            return _HandlerResult(
                status='skipped', outcome=InboundActivityOutcome.IGNORED_UNMAPPED_CONTEXT,
                detail='local community host is not an allowed Discord forum',
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
            # Canonical identity and content precede even a failed physical
            # Create. An unknown POST can now be located through its intent
            # after process death instead of being re-posted on a replay.
            thread_row = self.database.local_community_content.admit_inbound_post(
                local_community_id=local_community.id,
                ap_activity_id=event.delivery_id, ap_object_id=event.object.ap_id,
                current_content_json=self._inbound_snapshot(event, kind='post'),
            )
            confirmed = await PlatformFanoutCoordinator.from_adapter(
                self.database, self._adapter).stage_and_deliver(
                    domain='local', kind='post', canonical_id=thread_row.id,
                    forum=forum, operation='forum_thread', destination=forum,
                    content=ContentDocument(format_inbound_post_body(event)),
                    presentation=DeliveryPresentation(), title=thread_title,
                )
            host = self.database.local_community_surfaces.get_host_local_community_thread_surface(
                thread_row.id)
            if not confirmed or host is None:
                return _HandlerResult(
                    status='processed', outcome=InboundActivityOutcome.APPLIED,
                    detail='canonical post retained; physical host receipt pending',
                )
            if not self._origin_still_accepted(local_community, event):
                return _HandlerResult(
                    status='skipped', outcome=InboundActivityOutcome.IGNORED_ACTOR_NOT_SUBSCRIBER,
                    detail='remote subscriber revoked during physical delivery',
                )
            await self._fanout_post(
                local_community=local_community, thread_row=thread_row,
                title=thread_title, content=format_inbound_post_body(event),
            )
            await self.federation_fanout.relay_create(
                event=event, local_community=local_community, object_kind='post',
            )
            return _HandlerResult(
                status='processed', outcome=InboundActivityOutcome.APPLIED,
                detail='remote post created thread',
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
            if existing_thread is None or host_thread is None:
                return _HandlerResult(
                    status='skipped', outcome=InboundActivityOutcome.IGNORED_UNMAPPED_CONTEXT,
                    detail='canonical parent thread has no verified host surface',
                )
            if host_surface is None:
                # A duplicate AP Create cannot cause a second host POST just
                # because the first durable intent is blocked or unresolved.
                return _HandlerResult(
                    status='skipped', outcome=InboundActivityOutcome.IGNORED_ALREADY_APPLIED,
                    detail='canonical comment awaits host receipt',
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
        if thread_row.deleted_at is not None:
            # Do not retain the inbound child source before checking the
            # authoritative parent tombstone: this would poison later dedup.
            return _HandlerResult(
                status='skipped', outcome=InboundActivityOutcome.IGNORED_UNMAPPED_CONTEXT,
                detail='comment parent post is canonically deleted',
            )
        if self._adapter is None:
            raise RuntimeError('Local AP comment Create requires a bound platform adapter')
        forum = self._host_forum(local_community)
        if forum is None:
            return _HandlerResult(
                status='skipped', outcome=InboundActivityOutcome.IGNORED_UNMAPPED_CONTEXT,
                detail='local community host is not an allowed Discord forum',
            )

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
            blocked_parent_id: int | None = None
            parent_ap_id = getattr(event.object, 'parent_ap_id')
            if parent_ap_id is None or parent_ap_id == thread_row.ap_object_id:
                parent_discord_message_id = host_thread_surface.discord_starter_message_id
            else:
                parent_row = self.database.local_community_content.get_local_community_message_by_ap_object_id(
                    parent_ap_id
                )
                if (parent_row is None or parent_row.deleted_at is not None
                    or parent_row.local_community_thread_id != thread_row.id):
                    return _HandlerResult(
                        status="skipped", outcome=InboundActivityOutcome.IGNORED_UNMAPPED_CONTEXT,
                        detail="nested reply parent is absent or belongs to another thread",
                    )
                parent_surface = self.database.local_community_surfaces.get_local_community_message_surface(
                    local_community_message_id=parent_row.id,
                    local_community_thread_surface_id=host_thread_surface.id,
                )
                if parent_surface is None:
                    # A real known AP parent with no destination receipt is a
                    # blocked dependency, *not* a flat Discord reply.
                    blocked_parent_id = parent_row.id
                    parent_discord_message_id = None
                elif parent_surface.role != 'host':
                    return _HandlerResult(
                        status='skipped', outcome=InboundActivityOutcome.IGNORED_UNMAPPED_CONTEXT,
                        detail='nested parent surface has wrong role',
                    )
                else:
                    parent_discord_message_id = parent_surface.discord_message_id
            if blocked_parent_id is None and (
                parent_discord_message_id is None or parent_discord_message_id <= 0
            ):
                return _HandlerResult(
                    status="skipped", outcome=InboundActivityOutcome.IGNORED_UNMAPPED_CONTEXT,
                    detail="host parent physical message is unresolved",
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
            # A confirmed retained AP source and canonical parent are required
            # even for the first delivery; source and physical copy are not
            # interchangeable author identities.
            message_row = self.database.local_community_content.admit_inbound_comment(
                local_community_thread_id=thread_row.id,
                ap_activity_id=event.delivery_id, ap_object_id=event.object.ap_id,
                parent_ap_object_id=event.object.parent_ap_id,
                current_content_json=self._inbound_snapshot(event, kind='comment'),
            )
            if blocked_parent_id is not None:
                placement = self.database.platform_surfaces.resolve_claim(
                    forum.connector_key, forum.external_channel_id)
                if (placement is None or placement.local_community_id != local_community.id):
                    raise ValueError('nested target host no longer owns the forum')
                self.database.platform_delivery_intents.reserve(
                    domain='local', kind='comment', canonical_id=message_row.id,
                    forum=forum, channel_surface_id=placement.id,
                    owner_kind='local_host', owner_id=local_community.id,
                    blocked_parent_canonical_id=blocked_parent_id,
                )
                return _HandlerResult(
                    status='processed', outcome=InboundActivityOutcome.APPLIED,
                    detail='nested physical reply blocked on verified parent receipt',
                )
            destination = ThreadRef(forum, str(host_thread_surface.discord_thread_id))
            reply = MessageRef('discord-primary', destination.external_thread_id,
                               str(parent_discord_message_id))
            confirmed = await PlatformFanoutCoordinator.from_adapter(
                self.database, self._adapter).stage_and_deliver(
                    domain='local', kind='comment', canonical_id=message_row.id,
                    forum=forum, operation='message', destination=destination,
                    content=ContentDocument(format_inbound_comment_body(event)),
                    presentation=DeliveryPresentation(), reply_to=reply,
                )
            host_copy = self.database.local_community_surfaces.get_host_local_community_message_surface(
                message_row.id)
            if not confirmed or host_copy is None:
                return _HandlerResult(
                    status='processed', outcome=InboundActivityOutcome.APPLIED,
                    detail='canonical comment retained; physical host receipt pending',
                )
            self._persist_inbound_activitypub_message_mapping(
                event=event,
                discord_thread_id=host_thread_surface.discord_thread_id,
                discord_message_id=host_copy.discord_message_id,
            )
            if not self._origin_still_accepted(local_community, event):
                return _HandlerResult(
                    status='skipped', outcome=InboundActivityOutcome.IGNORED_ACTOR_NOT_SUBSCRIBER,
                    detail='remote subscriber revoked during physical reply',
                )
            await self._fanout_comment(
                local_community=local_community, thread_row=thread_row,
                message_row=message_row, content=format_inbound_comment_body(event),
            )
            await self.federation_fanout.relay_create(
                event=event, local_community=local_community, object_kind='comment',
            )
            return _HandlerResult(
                status='processed', outcome=InboundActivityOutcome.APPLIED,
                detail='remote comment created message',
            )
    def _duplicate_post_conflict(self, event: object, community: object, existing: object) -> str | None:
        """Validate immutable AP post/source identity before replaying side effects.

        Historical posts without a trustworthy persisted source must be
        skipped: accepting a replay as proof would let a forged author relay.
        """
        if (existing.deleted_at is not None
            or existing.local_community_id != community.id
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
            # MULTIPLATFORM_DISCORD_ADAPTER_TEMP_UNVERIFIED_AP_DUPLICATE: A legacy mapped AP object without independent persisted actor/payload provenance cannot safely replay Discord fan-out or ActivityPub relay; local subscriber participation must reconcile original proof before enabling recovery.
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
        if (thread is None or thread.deleted_at is not None
            or existing.deleted_at is not None
            or thread.local_community_id != community.id
            or thread.ap_object_id != event.object.post_ap_id
            or existing.ap_activity_id != event.delivery_id
            or existing.parent_ap_object_id != event.object.parent_ap_id
            or existing.direction != "ap_to_discord"):
            return "mapping_conflict"
        # Do not insert a new source row while allegedly verifying a duplicate.
        previous = self.database.personal_feed_conversations.get_inbound_object(event.object.ap_id)
        if previous is None or previous.source_activity_json is None:
            # MULTIPLATFORM_DISCORD_ADAPTER_TEMP_UNVERIFIED_AP_DUPLICATE: A legacy mapped AP object without independent persisted actor/payload provenance cannot safely replay Discord fan-out or ActivityPub relay; local subscriber participation must reconcile original proof before enabling recovery.
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

        # An independent local-community Discord copy can have a different
        # physical ID from an existing generic AP receipt. Keep that first
        # canonical lookup immutable while persisting the community-specific
        # copy in local_community_messages. Never adopt a different AP identity.
        existing = self.database.message_mappings.get_message_mapping_by_object_id(object_id)
        if existing is not None and (
            existing.source_platform, existing.source_id,
            existing.activity_id, existing.actor_url, existing.community_actor_url,
        ) == (
            "activitypub", object_id, activity_id,
            getattr(event, "actor_id"), getattr(event, "community_actor_id"),
        ):
            return
        # Repository insertion remains transactionally strict for an AP key
        # reused with incompatible provenance or a different ActivityPub ID.
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
