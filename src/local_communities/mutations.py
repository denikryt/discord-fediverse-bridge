"""Edit and delete propagation for local-community content.

This module owns Discord-authored and remote ActivityPub mutation orchestration
for local communities.  It preserves the starter-before-reply lookup order,
policy-filtered remote target selection, and best-effort local Discord fanout
semantics from the original runtime.
"""

from __future__ import annotations

from collections.abc import Callable
import logging
from typing import TYPE_CHECKING

from ..bridge_policy import BridgePolicyService
from ..db import Database
from ..fedify_gateway_client import DeleteContentRequest, FedifyGatewayClient, UpdateContentRequest
from ..inbound_activity_outcomes import InboundActivityOutcome
from ..runtime_ports import DiscordMutationContext, PlatformAdapterPort
from .discord_surface_fanout import LocalCommunitySurfaceFanout
from .federation_fanout import LocalCommunityFederationFanout
from .rendering import format_inbound_comment_body, format_inbound_post_body

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ..activitypub_handlers import HandlerResult


class _NoopWake:
    """Ignore wake calls for legacy gateway-only test fallbacks."""

    def wake(self) -> None:
        """Preserve old direct-gateway tests that had no worker."""


class LocalCommunityMutationHandler:
    """Coordinate local-community edit/delete propagation."""

    def __init__(
        self,
        *,
        database: Database,
        fedify_gateway: FedifyGatewayClient,
        bridge_policy_service: BridgePolicyService,
        surface_fanout: LocalCommunitySurfaceFanout,
        federation_fanout: LocalCommunityFederationFanout,
        is_community_id_disabled: Callable[[int | None], bool],
    ) -> None:
        """Store the narrow dependencies needed by mutation handling."""
        self.database = database
        self.fedify_gateway = fedify_gateway
        self.bridge_policy_service = bridge_policy_service
        self.surface_fanout = surface_fanout
        self.federation_fanout = federation_fanout
        self.is_community_id_disabled = is_community_id_disabled
        self.discord_adapter: PlatformAdapterPort | None = None

    def bind_discord_adapter(self, adapter: PlatformAdapterPort) -> None:
        """Bind the single verified physical transport for Discord-originated mutations."""
        if self.discord_adapter is not None:
            raise RuntimeError('Local Community mutation adapter is already configured')
        self.discord_adapter = adapter

    def _surface_can_mutate(self, surface: object, local_community_id: int) -> bool:
        """Return whether one Discord surface can author canonical mutations.

        Host surfaces are always authoritative.  Local-subscriber surfaces are
        authoritative only while the matching local subscriber row remains
        active, matching local subscriber participation's create-source routing invariant.
        """
        if getattr(surface, "role") != "local_subscriber":
            return True
        local_subscriber = self.database.local_subscribers.get_local_subscriber(
            local_community_id=local_community_id,
            discord_channel_id=getattr(surface, "discord_forum_channel_id"),
        )
        return (
            local_subscriber is not None
            and getattr(local_subscriber, "status") == "active"
            and getattr(local_subscriber, "id")
            == getattr(surface, "local_subscriber_id")
        )

    async def _send_update_request(
        self,
        *,
        mutation_context: DiscordMutationContext,
        published: object,
        new_content: str,
        edited_timestamp,
    ) -> None:
        """Durably admit one local-community edit before gateway I/O."""
        service = mutation_context.user_actor_delivery_service
        if service is None:
            raise RuntimeError('author Update requires durable User Actor delivery')
        if edited_timestamp is None:
            logger.warning(
                "Ignoring durable local-community edit without Discord edited_timestamp object=%s",
                published.object_id,
            )
            return
        try:
            outcome = await service.submit_update(
                object_id=published.object_id,
                title=published.title,
                body_markdown=new_content,
                edited_timestamp=edited_timestamp,
            )
            if outcome == "accepted":
                mutation_context.user_actor_delivery_wake.wake()
        except Exception:
            logger.exception(
                "Failed to reserve local-community edit for AP object %s",
                published.object_id,
            )

    async def _send_delete_request(
        self, *, mutation_context: DiscordMutationContext, published: object
    ) -> None:
        """Durably admit one terminal local-community delete."""
        service = mutation_context.user_actor_delivery_service
        if service is None:
            raise RuntimeError('author Delete requires durable User Actor delivery')
        try:
            outcome = await service.submit_delete(
                object_id=published.object_id,
            )
            if outcome == "accepted":
                mutation_context.user_actor_delivery_wake.wake()
        except Exception:
            logger.exception(
                "Failed to reserve local-community delete for AP object %s",
                published.object_id,
            )

    def _allowed_remote_inboxes(self, community_actor_url: str) -> list[str]:
        """Return accepted remote-subscriber inboxes allowed by current policy."""
        local_community = (
            self.database.local_communities.get_local_community_by_actor_url(
                community_actor_url
            )
        )
        if local_community is None:
            return []
        snapshot = self.bridge_policy_service.snapshot()
        return [
            str(row.remote_inbox_url)
            for row in self.database.remote_subscribers.list_remote_subscribers(
                getattr(local_community, "id"), status="accepted"
            )
            if snapshot.federation_decision(str(row.remote_inbox_url)).allowed
        ]


    def _require_mutation_context(
        self, *, mutation_context: DiscordMutationContext | None, runtime: object | None
    ) -> DiscordMutationContext:
        """Return the narrow mutation context, adapting legacy tests when needed."""
        if mutation_context is not None:
            return mutation_context
        if runtime is None:
            raise RuntimeError("Discord mutation context is not configured")
        # Compatibility is restricted to direct tests that still pass a runtime
        # namespace. Production app.py passes DiscordMutationContext directly.
        return DiscordMutationContext(
            bridge_policy_service=getattr(runtime, "bridge_policy_service", self.bridge_policy_service),
            user_actor_delivery_service=getattr(runtime, "user_actor_delivery_service", None),
            user_actor_delivery_wake=getattr(
                runtime, "user_actor_delivery_worker", _NoopWake()
            ),
            fedify_gateway=getattr(runtime, "fedify_gateway", self.fedify_gateway),
        )

    async def handle_discord_message_edit(
        self,
        *,
        message_id: int,
        new_content: str,
        mutation_context: DiscordMutationContext | None = None,
        runtime: object | None = None,
        author_display_name: str = "",
        edited_timestamp=None,
    ) -> None:
        """Propagate one Discord-authored local-community edit to all participants.

        participant mutation support resolves the edited Discord message through surface rows first.
        That lets host and local-subscriber copies both resolve the same
        canonical AP object, while still excluding the user-edited source copy
        from local Discord fanout.
        """
        del author_display_name
        mutation_context = self._require_mutation_context(
            mutation_context=mutation_context, runtime=runtime
        )

        thread_surface = self.database.local_community_surfaces.get_local_community_thread_surface_by_starter_message_id(
            message_id
        )
        if thread_surface is not None:
            thread_row = self.database.local_community_surfaces.get_local_community_thread_for_surface(
                getattr(thread_surface, "id")
            )
            if thread_row is None or not self._surface_can_mutate(
                thread_surface, getattr(thread_row, "local_community_id")
            ):
                return
            if self.is_community_id_disabled(
                getattr(thread_row, "local_community_id", None)
            ):
                return
            published = self.database.activitypub_objects.get_published_activity_object_by_object_id(
                getattr(thread_row, "ap_object_id")
            )
            if published is None:
                return
            await self._send_update_request(
                mutation_context=mutation_context,
                published=published,
                new_content=new_content,
                edited_timestamp=edited_timestamp,
            )
            await self.surface_fanout.fanout_thread_edit(
                thread_row=thread_row,
                source_surface_id=getattr(thread_surface, "id"),
                new_content=new_content,
                adapter=self.discord_adapter,
            )
            return

        message_surface = self.database.local_community_surfaces.get_local_community_message_surface_by_discord_message_id(
            message_id
        )
        if message_surface is None:
            return
        message_row = self.database.local_community_surfaces.get_local_community_message_for_surface(
            getattr(message_surface, "id")
        )
        if message_row is None:
            return
        thread_row = (
            self.database.local_community_content.get_local_community_thread_by_id(
                getattr(message_row, "local_community_thread_id")
            )
        )
        if thread_row is None or not self._surface_can_mutate(
            message_surface, getattr(thread_row, "local_community_id")
        ):
            return
        if self.is_community_id_disabled(
            getattr(thread_row, "local_community_id", None)
        ):
            return
        published = self.database.activitypub_objects.get_published_activity_object_by_object_id(
            getattr(message_row, "ap_object_id")
        )
        if published is None:
            return
        await self._send_update_request(
            mutation_context=mutation_context,
            published=published,
            new_content=new_content,
            edited_timestamp=edited_timestamp,
        )
        await self.surface_fanout.fanout_message_edit(
            message_row=message_row,
            source_surface_id=getattr(message_surface, "id"),
            new_content=new_content,
            adapter=self.discord_adapter,
        )

    async def handle_discord_message_delete(
        self,
        *,
        message_id: int,
        mutation_context: DiscordMutationContext | None = None,
        runtime: object | None = None,
    ) -> None:
        """Propagate one Discord-authored local-community delete to participants."""
        mutation_context = self._require_mutation_context(
            mutation_context=mutation_context, runtime=runtime
        )
        thread_surface = self.database.local_community_surfaces.get_local_community_thread_surface_by_starter_message_id(
            message_id
        )
        if thread_surface is not None:
            thread_row = self.database.local_community_surfaces.get_local_community_thread_for_surface(
                getattr(thread_surface, "id")
            )
            if thread_row is None or not self._surface_can_mutate(
                thread_surface, getattr(thread_row, "local_community_id")
            ):
                return
            if self.is_community_id_disabled(
                getattr(thread_row, "local_community_id", None)
            ):
                return
            published = self.database.activitypub_objects.get_published_activity_object_by_object_id(
                getattr(thread_row, "ap_object_id")
            )
            if published is None:
                return
            await self._send_delete_request(
                mutation_context=mutation_context, published=published
            )
            await self.surface_fanout.fanout_thread_delete(
                thread_row=thread_row,
                source_surface_id=getattr(thread_surface, "id"),
                adapter=self.discord_adapter,
            )
            return

        message_surface = self.database.local_community_surfaces.get_local_community_message_surface_by_discord_message_id(
            message_id
        )
        if message_surface is None:
            return
        message_row = self.database.local_community_surfaces.get_local_community_message_for_surface(
            getattr(message_surface, "id")
        )
        if message_row is None:
            return
        thread_row = (
            self.database.local_community_content.get_local_community_thread_by_id(
                getattr(message_row, "local_community_thread_id")
            )
        )
        if thread_row is None or not self._surface_can_mutate(
            message_surface, getattr(thread_row, "local_community_id")
        ):
            return
        if self.is_community_id_disabled(
            getattr(thread_row, "local_community_id", None)
        ):
            return
        published = self.database.activitypub_objects.get_published_activity_object_by_object_id(
            getattr(message_row, "ap_object_id")
        )
        if published is None:
            return
        await self._send_delete_request(
            mutation_context=mutation_context, published=published
        )
        await self.surface_fanout.fanout_message_delete(
            message_row=message_row,
            source_surface_id=getattr(message_surface, "id"),
            adapter=self.discord_adapter,
        )

    async def handle_inbound_post_update(
        self, event: object, runtime: object
    ) -> HandlerResult:
        """Edit the starter message for one inbound remote post update."""
        from ..activitypub_handlers import HandlerResult as _HandlerResult

        thread_row = self.database.local_community_content.get_local_community_thread_by_ap_object_id(
            getattr(getattr(event, "object"), "ap_id")
        )
        if thread_row is None:
            return _HandlerResult(
                status="skipped",
                outcome=InboundActivityOutcome.IGNORED_UNMAPPED_CONTEXT,
                detail="post not yet mapped",
            )
        local_community = (
            self.database.local_communities.get_local_community_by_actor_url(
                getattr(event, "community_actor_id")
            )
        )
        # Durable canonical Update admission must precede every physical
        # edit and Group relay, including with zero Discord target surfaces.
        if local_community is None:
            return _HandlerResult(
                'skipped', InboundActivityOutcome.IGNORED_UNMAPPED_CONTEXT,
                'local community Update has no owner',
            )
        from .inbound_create import LocalCommunityInboundCreateHandler
        try:
            first_update = self.database.local_community_content.admit_authenticated_update(
                kind='post', ap_object_id=event.object.ap_id,
                local_community_id=local_community.id, actor_id=event.actor_id,
                new_content_json=LocalCommunityInboundCreateHandler._inbound_snapshot(
                    event, kind='post'),
                updated_at=getattr(event.object, 'updated_at', None),
                source_activity_id=getattr(event, 'source_activity_id', None),
            )
        except ValueError as exc:
            return _HandlerResult(
                'skipped', InboundActivityOutcome.IGNORED_UNMAPPED_CONTEXT, str(exc),
            )
        if not first_update:
            return _HandlerResult(
                'skipped', InboundActivityOutcome.IGNORED_ALREADY_APPLIED,
                'canonical local post Update already admitted',
            )
        mutation_coordinator = getattr(runtime, 'physical_mutation_coordinator', None)
        if mutation_coordinator is not None:
            await mutation_coordinator.drain()
        # Retain canonical work for startup recovery when adapter is absent;
        # do not re-enable the legacy direct Discord edit/delete path.
        if local_community is not None:
            await self.federation_fanout.relay_update_or_delete(
                event=event,
                local_community=local_community,
                object_kind="post",
                operation="update",
            )
        return _HandlerResult(
            status="processed",
            outcome=InboundActivityOutcome.APPLIED,
            detail="post updated",
        )

    async def handle_inbound_post_delete(
        self, event: object, runtime: object
    ) -> HandlerResult:
        """Mark the starter message deleted for one inbound remote post delete."""
        from ..activitypub_handlers import HandlerResult as _HandlerResult

        thread_row = self.database.local_community_content.get_local_community_thread_by_ap_object_id(
            getattr(getattr(event, "object"), "ap_id")
        )
        if thread_row is None:
            return _HandlerResult(
                status="skipped",
                outcome=InboundActivityOutcome.IGNORED_UNMAPPED_CONTEXT,
                detail="post not yet mapped",
            )
        local_community = (
            self.database.local_communities.get_local_community_by_actor_url(
                getattr(event, "community_actor_id")
            )
        )
        if local_community is None:
            return _HandlerResult(
                status='skipped', outcome=InboundActivityOutcome.IGNORED_UNMAPPED_CONTEXT,
                detail='local community Delete does not match owning actor',
            )
        try:
            first_delete = self.database.local_community_content.mark_inbound_deleted_if_live(
                kind='post', ap_object_id=event.object.ap_id,
                local_community_id=local_community.id, actor_id=event.actor_id,
            )
        except ValueError as exc:
            return _HandlerResult(
                status='skipped', outcome=InboundActivityOutcome.IGNORED_UNMAPPED_CONTEXT,
                detail=str(exc),
            )
        if not first_delete:
            return _HandlerResult(
                status='skipped', outcome=InboundActivityOutcome.IGNORED_ALREADY_APPLIED,
                detail='canonical local post already deleted',
            )
        # Physical edit and federation relay follow the durable writer commit.
        mutation_coordinator = getattr(runtime, 'physical_mutation_coordinator', None)
        if mutation_coordinator is not None:
            await mutation_coordinator.drain()
        # Retain canonical work for startup recovery when adapter is absent;
        # do not re-enable the legacy direct Discord edit/delete path.
        if local_community is not None:
            await self.federation_fanout.relay_update_or_delete(
                event=event,
                local_community=local_community,
                object_kind="post",
                operation="delete",
            )
        return _HandlerResult(
            status="processed",
            outcome=InboundActivityOutcome.APPLIED,
            detail="post deleted",
        )

    async def handle_inbound_comment_update(
        self, event: object, runtime: object
    ) -> HandlerResult:
        """Edit the mirrored Discord copy for one inbound remote comment update."""
        from ..activitypub_handlers import HandlerResult as _HandlerResult

        message_row = self.database.local_community_content.get_local_community_message_by_ap_object_id(
            getattr(getattr(event, "object"), "ap_id")
        )
        if message_row is None:
            return _HandlerResult(
                status="skipped",
                outcome=InboundActivityOutcome.IGNORED_UNMAPPED_CONTEXT,
                detail="comment not yet mapped",
            )
        thread_row = (
            self.database.local_community_content.get_local_community_thread_by_id(
                getattr(message_row, "local_community_thread_id")
            )
        )
        if thread_row is None:
            return _HandlerResult(
                status="skipped",
                outcome=InboundActivityOutcome.IGNORED_UNMAPPED_CONTEXT,
                detail="comment thread not mapped",
            )
        local_community = (
            self.database.local_communities.get_local_community_by_actor_url(
                getattr(event, "community_actor_id")
            )
        )
        # Durable canonical Update admission must precede every physical
        # edit and Group relay, including with zero Discord target surfaces.
        if local_community is None:
            return _HandlerResult(
                'skipped', InboundActivityOutcome.IGNORED_UNMAPPED_CONTEXT,
                'local community Update has no owner',
            )
        from .inbound_create import LocalCommunityInboundCreateHandler
        try:
            first_update = self.database.local_community_content.admit_authenticated_update(
                kind='comment', ap_object_id=event.object.ap_id,
                local_community_id=local_community.id, actor_id=event.actor_id,
                new_content_json=LocalCommunityInboundCreateHandler._inbound_snapshot(
                    event, kind='comment'),
                updated_at=getattr(event.object, 'updated_at', None),
                source_activity_id=getattr(event, 'source_activity_id', None),
            )
        except ValueError as exc:
            return _HandlerResult(
                'skipped', InboundActivityOutcome.IGNORED_UNMAPPED_CONTEXT, str(exc),
            )
        if not first_update:
            return _HandlerResult(
                'skipped', InboundActivityOutcome.IGNORED_ALREADY_APPLIED,
                'canonical local comment Update already admitted',
            )
        mutation_coordinator = getattr(runtime, 'physical_mutation_coordinator', None)
        if mutation_coordinator is not None:
            await mutation_coordinator.drain()
        # Retain canonical work for startup recovery when adapter is absent;
        # do not re-enable the legacy direct Discord edit/delete path.
        if local_community is not None:
            await self.federation_fanout.relay_update_or_delete(
                event=event,
                local_community=local_community,
                object_kind="comment",
                operation="update",
            )
        return _HandlerResult(
            status="processed",
            outcome=InboundActivityOutcome.APPLIED,
            detail="comment updated",
        )

    async def handle_inbound_comment_delete(
        self, event: object, runtime: object
    ) -> HandlerResult:
        """Mark the mirrored Discord copy deleted for one inbound remote comment delete."""
        from ..activitypub_handlers import HandlerResult as _HandlerResult

        message_row = self.database.local_community_content.get_local_community_message_by_ap_object_id(
            getattr(getattr(event, "object"), "ap_id")
        )
        if message_row is None:
            return _HandlerResult(
                status="skipped",
                outcome=InboundActivityOutcome.IGNORED_UNMAPPED_CONTEXT,
                detail="comment not yet mapped",
            )
        thread_row = (
            self.database.local_community_content.get_local_community_thread_by_id(
                getattr(message_row, "local_community_thread_id")
            )
        )
        if thread_row is None:
            return _HandlerResult(
                status="skipped",
                outcome=InboundActivityOutcome.IGNORED_UNMAPPED_CONTEXT,
                detail="comment thread not mapped",
            )
        local_community = (
            self.database.local_communities.get_local_community_by_actor_url(
                getattr(event, "community_actor_id")
            )
        )
        if local_community is None:
            return _HandlerResult(
                status='skipped', outcome=InboundActivityOutcome.IGNORED_UNMAPPED_CONTEXT,
                detail='local community Delete does not match owning actor',
            )
        try:
            first_delete = self.database.local_community_content.mark_inbound_deleted_if_live(
                kind='comment', ap_object_id=event.object.ap_id,
                local_community_id=local_community.id, actor_id=event.actor_id,
            )
        except ValueError as exc:
            return _HandlerResult(
                status='skipped', outcome=InboundActivityOutcome.IGNORED_UNMAPPED_CONTEXT,
                detail=str(exc),
            )
        if not first_delete:
            return _HandlerResult(
                status='skipped', outcome=InboundActivityOutcome.IGNORED_ALREADY_APPLIED,
                detail='canonical local comment already deleted',
            )
        # Physical edit and federation relay follow the durable writer commit.
        mutation_coordinator = getattr(runtime, 'physical_mutation_coordinator', None)
        if mutation_coordinator is not None:
            await mutation_coordinator.drain()
        # Retain canonical work for startup recovery when adapter is absent;
        # do not re-enable the legacy direct Discord edit/delete path.
        if local_community is not None:
            await self.federation_fanout.relay_update_or_delete(
                event=event,
                local_community=local_community,
                object_kind="comment",
                operation="delete",
            )
        return _HandlerResult(
            status="processed",
            outcome=InboundActivityOutcome.APPLIED,
            detail="comment deleted",
        )
