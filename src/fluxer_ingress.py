"""Fluxer-only normalized Gateway sink with a fail-closed canonical publication gate.

This handler does not normalize native SDK events or bind Fluxer forums to
Discord communities. Physical ownership is checked from live SQLite evidence.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select

from .models import (
    ChannelSurface, CommunityMessageGroupDelivery, CommunityThreadGroupDelivery,
    LocalCommunityMessageSurface, LocalCommunityThreadSurface, PlatformConnector,
)
from .platform_types import (
    ChannelRef, MessageCreated, MessageDeleted, MessageRef, MessageUpdated,
    PlatformInboundEvent, ThreadCreated, ThreadDeleted, ThreadUpdated,
)

logger = logging.getLogger(__name__)


class FluxerInboundHandler:
    """Check one Fluxer connector's forum ownership and gate publication to #13.

    One admission lifecycle is shared by private HTTP and all accepted sink
    operations; shutdown joins those tasks before the runtime closes SQLite.
    """

    def __init__(self, database, connector_key: str) -> None:
        """Bind one persisted physical connector without deriving an AP owner."""
        if not isinstance(connector_key, str) or not connector_key:
            raise ValueError("Fluxer ingress requires a connector key")
        self.database = database
        self.connector_key = connector_key
        self._accepting = True
        self._inflight: set[asyncio.Task] = set()

    @property
    def accepting(self) -> bool:
        """Report the shared ingress admission state without repository access."""
        return self._accepting

    async def accept_event(self, event: PlatformInboundEvent) -> None:
        """Admit one HTTP request through the same task ownership as the sink."""
        if not self._accepting:
            raise RuntimeError("Fluxer ingress is quiescing")
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("Fluxer ingress requires an asyncio task")
        self._inflight.add(task)
        try:
            await self.handle_platform_event(event)
        finally:
            self._inflight.discard(task)

    async def quiesce(self) -> None:
        """Refuse late intake and join already accepted SQLite sink operations."""
        self._accepting = False
        current = asyncio.current_task()
        tasks = tuple(task for task in self._inflight if task is not current)
        if tasks:
            # Accepted messages remain admitted until their own sink call
            # returns. Do not cancel a DB owner while its result is unknown.
            await asyncio.gather(*tasks)

    async def handle_platform_event(self, event: PlatformInboundEvent) -> None:
        """Observe six normalized event kinds without publishing canonical AP."""
        if not isinstance(event, (
            ThreadCreated, ThreadUpdated, ThreadDeleted,
            MessageCreated, MessageUpdated, MessageDeleted,
        )):
            raise ValueError("unsupported Fluxer normalized event")
        if event.connector_key != self.connector_key:
            raise ValueError("wrong Fluxer connector")
        if not self._claimed_forum(event):
            # ID-only edits/deletes are valid DTOs, but a physical identifier
            # cannot supply missing forum ownership by itself.
            logger.info("Fluxer event ignored: unproven forum ownership")
            return
        if isinstance(event, (ThreadUpdated, ThreadDeleted)):
            logger.info("Fluxer thread metadata observed without canonical publication")
            return
        if isinstance(event, MessageCreated) and (
            event.author.is_bridge_owned or event.author.kind in {"bot", "webhook", "unknown"}
        ):
            logger.info("Fluxer non-human/echo message ignored")
            return
        if isinstance(event, ThreadCreated) and event.newly_created is False:
            logger.info("Fluxer existing thread observed without new source publication")
            return
        # Claimed physical observations still are NOT a canonical source: #13
        # must bind the owning community and prove connector-aware human author.
        logger.info("Fluxer canonical publication blocked: community_binding_not_active")
        # MULTIPLATFORM_FLUXER_COMMUNITY_BINDING_PENDING: Fluxer forum events are verified transport observations, not canonical publications; issue #13 must establish connector-aware community ownership and authenticated Fluxer author identity before removing this fail-closed gate.
        return

    def _claimed_forum(self, event: PlatformInboundEvent) -> bool:
        """Resolve proven connector/forum claims, including stored ID-only copies."""
        if isinstance(event, (ThreadCreated, ThreadUpdated, ThreadDeleted)):
            parent = event.thread.channel
        else:
            parent = event.parent_channel
            if parent is None and event.thread_context is not None:
                parent = event.thread_context.channel
        if parent is not None:
            placement = self.database.platform_surfaces.resolve_claim(
                self.connector_key, parent.external_channel_id,
            )
            return bool(
                placement is not None and placement.platform == "fluxer"
                and placement.kind == "forum" and placement.connector_key == self.connector_key
                and (parent.external_guild_id is None
                     or parent.external_guild_id == placement.external_guild_id)
                and (event.external_guild_id is None
                     or event.external_guild_id == placement.external_guild_id)
            )
        if not isinstance(event, (MessageUpdated, MessageDeleted)):
            return False
        # A cache-less mutation may carry *only* a message ref. Match exact
        # persisted physical copy/starter with its connector and immediate
        # container; a same-numbered Discord/foreign copy never grants a claim.
        return self._stored_copy_claim(event.message, event.external_guild_id)

    def _stored_copy_claim(self, message: MessageRef, external_guild_id: str | None) -> bool:
        """Find an exact persisted forum copy; never use message IDs globally."""
        with self.database.session() as session:
            criteria = (
                PlatformConnector.connector_key == self.connector_key,
                PlatformConnector.platform == "fluxer",
                ChannelSurface.kind == "forum",
                ChannelSurface.role.is_not(None),
            )
            if external_guild_id is not None:
                # ID-only events may still carry a claimed guild: never ignore
                # a contradiction to the durable physical placement.
                criteria += (ChannelSurface.external_guild_id == external_guild_id,)
            # The post surface stores the independently verified starter ID.
            for model in (LocalCommunityThreadSurface, CommunityThreadGroupDelivery):
                query = (
                    select(model.id)
                    .join(ChannelSurface, model.channel_surface_id == ChannelSurface.id)
                    .join(PlatformConnector, model.connector_id == PlatformConnector.id)
                    .where(*criteria,
                           ChannelSurface.connector_id == PlatformConnector.id,
                           model.external_thread_id == message.external_container_id,
                           model.external_starter_message_id == message.external_message_id)
                )
                if session.scalar(query) is not None:
                    return True
            for model in (LocalCommunityMessageSurface, CommunityMessageGroupDelivery):
                query = (
                    select(model.id)
                    .join(ChannelSurface, model.channel_surface_id == ChannelSurface.id)
                    .join(PlatformConnector, model.connector_id == PlatformConnector.id)
                    .where(*criteria,
                           ChannelSurface.connector_id == PlatformConnector.id,
                           model.external_message_id == message.external_message_id)
                )
                if model is LocalCommunityMessageSurface:
                    query = query.join(
                        LocalCommunityThreadSurface,
                        model.local_community_thread_surface_id == LocalCommunityThreadSurface.id,
                    ).where(LocalCommunityThreadSurface.external_thread_id == message.external_container_id)
                else:
                    query = query.where(model.external_thread_id == message.external_container_id)
                if session.scalar(query) is not None:
                    return True
        return False
