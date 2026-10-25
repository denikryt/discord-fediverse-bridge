"""Local Discord fanout for bridge-owned local communities.

This module owns concrete Discord surface creation for canonical local-community
activity.  It never publishes ActivityPub and never selects remote subscribers;
its only responsibility is making sure the host forum and active local
subscriber forums have the expected Discord thread/message surfaces.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..bridge_policy import BridgePolicyService
from ..content_sync.edit_delete import (
    edit_discord_message,
    mark_discord_message_deleted,
)
from ..content_sync.platform_fanout import PlatformFanoutCoordinator
from ..formatting import apply_edit_to_discord_message
from ..db import Database
from ..platform_types import ChannelRef, ContentDocument, DeliveryPresentation, MessageRef, PlatformOperationError, ThreadRef
from ..runtime_ports import PlatformAdapterPort, PlatformDeliveryPort

logger = logging.getLogger(__name__)


def _valid_discord_guild_id(value: object) -> int | None:
    """Return a valid positive Discord guild id, otherwise ``None``."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _format_local_mirror_body(*, author_display_name: str | None, content: str) -> str:
    """Render one Discord-authored local-community copy with a stable header.

    Local-community Discord-to-Discord fanout should keep the same first-line
    attribution contract as the shared mirror mode so sibling copies show who
    authored the content and later edits can preserve that header.
    """
    normalized_content = (content or "").strip()
    normalized_author = (author_display_name or "").strip()
    if not normalized_author:
        return normalized_content
    if normalized_content:
        return f"`{normalized_author}`\n\n{normalized_content}"
    return f"`{normalized_author}`"


@dataclass(slots=True)
class LocalDiscordFanoutSummary:
    """Summarise local Discord fanout attempts for one canonical activity."""

    attempted: int = 0
    delivered: int = 0
    skipped_existing: int = 0
    skipped_missing_thread_surface: int = 0
    skipped_missing_parent_surface: int = 0
    failed: int = 0


@dataclass(slots=True)
class LocalDiscordMutationFanoutSummary:
    """Report local Discord edit/delete attempts across persisted surfaces.

    participant mutation support mutation fanout is best-effort per persisted surface.  The source
    surface is skipped for Discord-originated mutations, while remote-originated
    mutations target every local surface because no local Discord source exists.
    """

    attempted: int = 0
    applied: int = 0
    failed: int = 0
    skipped_source: int = 0
    skipped_missing_thread: int = 0


@dataclass(slots=True)
class LocalDiscordFanoutTarget:
    """Describe one Discord forum target for local-community surface fanout."""

    role: str
    discord_forum_channel_id: int
    discord_guild_id: int | None
    local_subscriber_id: int | None


class LocalCommunityDiscordFanout:
    """Create Discord surfaces for canonical local-community activity."""

    def __init__(
        self, *, database: Database, bot: object, policy_service: BridgePolicyService
    ) -> None:
        """Initialise the fanout helper with persistence and Discord boundaries."""
        self.database = database
        self.bot = bot
        self.policy_service = policy_service

    def _forum_guild_id(self, forum_channel_id: int) -> int | None:
        """Resolve a target forum's owning guild from persisted routing state."""
        subscriber = self.database.local_subscribers.get_local_subscriber_by_channel(
            forum_channel_id
        )
        if subscriber is not None:
            return _valid_discord_guild_id(
                getattr(subscriber, "discord_guild_id", None)
            )
        community = (
            self.database.local_communities.get_local_community_by_forum_channel_id(
                forum_channel_id
            )
        )
        if community is None:
            return None
        placement = self.database.platform_surfaces.get_discord_host_placement(
            community.id
        )
        return (
            _valid_discord_guild_id(int(placement.external_guild_id))
            if placement is not None and placement.external_guild_id is not None
            else None
        )

    def _surface_is_allowed(self, surface: object, community_id: int) -> bool:
        """Authorize a persisted physical copy against its current owner and guild.

        An old receipt is not a subscription: Undo or rebinding a forum must
        prevent later mutations even if the copy remains in SQLite.
        """
        forum_id = getattr(surface, "discord_forum_channel_id", None)
        role = getattr(surface, "role", None)
        if not isinstance(forum_id, int) or forum_id <= 0:
            return False
        try:
            # Subscriber and host ownership checks must also fail closed when
            # their repositories are temporarily unavailable. Checking only
            # the guild lookup in a try/except leaves this admission exposed.
            if role == "host":
                placement = self.database.platform_surfaces.get_discord_host_placement(
                    community_id
                )
                if (placement is None
                    or placement.external_channel_id != str(forum_id)
                    or getattr(surface, "local_subscriber_id", None) is not None):
                    return False
            elif role == "local_subscriber":
                subscriber = self.database.local_subscribers.get_local_subscriber_by_channel(
                    forum_id
                )
                if (subscriber is None or subscriber.status != "active"
                    or subscriber.local_community_id != community_id
                    or subscriber.id != getattr(surface, "local_subscriber_id", None)):
                    return False
            else:
                return False
            guild_id = self._forum_guild_id(forum_id)
            if guild_id is None:
                return False
            return self.policy_service.is_discord_guild_allowed(guild_id)
        except Exception:
            logger.exception(
                "Failed to validate local Discord surface ownership forum_channel_id=%s",
                forum_id,
            )
            return False

    def _target_is_allowed(self, target: LocalDiscordFanoutTarget) -> bool:
        """Fail closed before creating one selected local Discord target surface."""
        guild_id = _valid_discord_guild_id(target.discord_guild_id)
        if guild_id is None:
            logger.warning(
                "Skipping local Discord fanout target with invalid guild metadata "
                "forum_channel_id=%s role=%s guild_id=%r",
                target.discord_forum_channel_id,
                target.role,
                target.discord_guild_id,
            )
            return False
        try:
            return self.policy_service.is_discord_guild_allowed(guild_id)
        except Exception:
            logger.exception(
                "Failed to evaluate local Discord fanout policy forum_channel_id=%s role=%s",
                target.discord_forum_channel_id,
                target.role,
            )
            return False

    async def fanout_normalized_thread(
        self,
        *,
        adapter: PlatformDeliveryPort,
        local_community: object,
        thread_row: object,
        title: str,
        content: str,
        author_display_name: str | None,
        source_forum_channel_id: int | None,
        include_host: bool,
    ) -> LocalDiscordFanoutSummary:
        """Deliver typed forum copies to the same host/subscriber targets as legacy fanout.

        A stable per-canonical-target attempt identifies physical delivery, while
        the repository owns permanent copy identity. Never infer a starter ID
        from a thread ID and never persist a receipt from another target.
        """
        if getattr(thread_row, 'local_community_id') != getattr(local_community, 'id'):
            raise PlatformOperationError('invalid_request', 'canonical thread community mismatch')
        summary = LocalDiscordFanoutSummary()
        body = ContentDocument(_format_local_mirror_body(
            author_display_name=author_display_name, content=content,
        ))
        for target in self._select_targets(
            local_community=local_community, include_host=include_host,
            source_forum_channel_id=source_forum_channel_id,
        ):
            if not self._target_is_allowed(target):
                continue
            existing = self.database.local_community_surfaces.get_local_community_thread_surface(
                local_community_thread_id=thread_row.id,
                discord_forum_channel_id=target.discord_forum_channel_id,
            )
            if existing is not None:
                summary.skipped_existing += 1
                continue
            summary.attempted += 1
            try:
                # Target guild is sourced from the active subscription or the
                # independently stored host placement, not the untrusted event.
                forum = ChannelRef(
                    'discord-primary', str(target.discord_forum_channel_id),
                    'forum', str(target.discord_guild_id),
                )
                # The intent and frozen body must commit before the Discord POST;
                # the coordinator atomically records verified thread+starter IDs.
                delivered = await PlatformFanoutCoordinator.from_adapter(
                    self.database,adapter).stage_and_deliver(
                    domain='local',kind='post',canonical_id=thread_row.id,
                    forum=forum,operation='forum_thread',destination=forum,
                    content=body,presentation=DeliveryPresentation(),title=title)
                if not delivered:
                    summary.failed += 1
                    continue
                summary.delivered += 1
            except Exception:
                summary.failed += 1
                logger.exception(
                    'Failed to deliver typed local thread community=%s forum=%s',
                    local_community.id, target.discord_forum_channel_id,
                )
        return summary

    async def fanout_normalized_message(
        self,
        *,
        adapter: PlatformDeliveryPort,
        local_community: object,
        thread_row: object,
        message_row: object,
        content: str,
        author_display_name: str | None,
        source_forum_channel_id: int | None,
        include_host: bool,
    ) -> LocalDiscordFanoutSummary:
        """Send one canonical reply through typed destinations with per-copy parent IDs.

        Missing nested parents suppress only the affected target. The original
        Discord thread/starter pairing is never a fallback for an unresolved
        nested reply, and duplicate physical receipts remain repository-owned.
        """
        if (getattr(thread_row, 'local_community_id') != getattr(local_community, 'id')
            or getattr(message_row, 'local_community_thread_id') != thread_row.id):
            raise PlatformOperationError('invalid_request', 'canonical local reply ownership mismatch')
        summary = LocalDiscordFanoutSummary()
        body = ContentDocument(_format_local_mirror_body(
            author_display_name=author_display_name, content=content,
        ))
        for target in self._select_targets(
            local_community=local_community, include_host=include_host,
            source_forum_channel_id=source_forum_channel_id,
        ):
            if not self._target_is_allowed(target):
                continue
            surface = self.database.local_community_surfaces.get_local_community_thread_surface(
                local_community_thread_id=thread_row.id,
                discord_forum_channel_id=target.discord_forum_channel_id,
            )
            if (surface is None or surface.role != target.role
                or surface.local_subscriber_id != target.local_subscriber_id):
                # A historical thread copy belongs to its original owner.
                # Reassigning this forum cannot transfer reply authority to
                # the new subscriber/host, even when the AP post is the same.
                summary.skipped_missing_thread_surface += 1
                continue
            existing = self.database.local_community_surfaces.get_local_community_message_surface(
                local_community_message_id=message_row.id,
                local_community_thread_surface_id=surface.id,
            )
            if existing is not None:
                summary.skipped_existing += 1
                continue
            try:
                # Typed receipts have no legacy integer Discord projection.
                # Resolve both root and nested parents from the exact
                # connector, forum surface and destination thread instead.
                from sqlalchemy import select
                from ..models import LocalCommunityMessage
                from ..content_sync.parent_mapping import MissingParent, resolve_target_parent

                forum = ChannelRef(
                    'discord-primary', str(target.discord_forum_channel_id),
                    'forum', str(target.discord_guild_id),
                )
                destination = ThreadRef(forum, surface.external_thread_id)
                parent_ap_id = getattr(message_row, 'parent_ap_object_id', None)
                parent_id = None
                with self.database.session() as session:
                    if parent_ap_id not in (None, thread_row.ap_object_id):
                        parent = session.scalar(select(LocalCommunityMessage).where(
                            LocalCommunityMessage.ap_object_id == parent_ap_id,
                            LocalCommunityMessage.local_community_thread_id == thread_row.id,
                            LocalCommunityMessage.deleted_at.is_(None),
                        ))
                        if parent is None:
                            # An unidentified or foreign parent must not be
                            # flattened onto the starter or another thread.
                            raise ValueError('unknown/foreign canonical nested parent')
                        parent_id = parent.id
                    physical_parent = resolve_target_parent(
                        session, domain='local', canonical_thread_id=thread_row.id,
                        target=destination, target_surface_id=surface.channel_surface_id,
                        canonical_parent_message_id=parent_id,
                    )
                coordinator = PlatformFanoutCoordinator.from_adapter(self.database, adapter)
                if isinstance(physical_parent, MissingParent):
                    # Missing *known* parents must leave durable blocked work.
                    # Parent receipt/restart recovery will create the complete
                    # immutable DTO only after proof in this same destination.
                    if coordinator.stage_blocked(
                        domain='local', kind='comment', canonical_id=message_row.id,
                        forum=forum, parent_canonical_id=physical_parent.canonical_parent_id,
                    ) is None:
                        summary.failed += 1
                    else:
                        summary.skipped_missing_parent_surface += 1
                    continue
                summary.attempted += 1
                delivered = await coordinator.stage_and_deliver(
                    domain='local', kind='comment', canonical_id=message_row.id,
                    forum=forum, operation='message', destination=destination,
                    content=body, presentation=DeliveryPresentation(),
                    reply_to=physical_parent,
                )
                if not delivered:
                    summary.failed += 1
                    continue
                summary.delivered += 1
            except Exception:
                summary.failed += 1
                logger.exception(
                    'Failed to deliver typed local reply community=%s forum=%s',
                    local_community.id, target.discord_forum_channel_id,
                )
        return summary


    async def _apply_normalized_mutation(
        self,
        *,
        adapter: PlatformAdapterPort,
        thread_id: int,
        message_id: int,
        new_content: str | None,
    ) -> None:
        """Edit one verified Discord receipt through the connector boundary.

        A remote Update must retain the physical copy's existing attribution
        header. Missing or mismatched reads fail closed instead of guessing an
        author. Delete keeps the historical visible tombstone semantics.
        """
        ref = MessageRef('discord-primary', str(thread_id), str(message_id))
        if new_content is None:
            body = '*deleted by creator*'
        else:
            observed = await adapter.fetch_message(ref)
            if observed.message != ref or observed.content is None:
                raise PlatformOperationError(
                    'unknown_delivery', 'local Discord mutation receipt mismatch',
                )
            body = apply_edit_to_discord_message(observed.content.body, new_content)
        await adapter.edit_message(ref, ContentDocument(body))

    async def fanout_thread_starter_edit(
        self,
        *,
        thread_row: object,
        source_surface_id: int | None,
        new_content: str,
        adapter: PlatformAdapterPort | None = None,
    ) -> LocalDiscordMutationFanoutSummary:
        """Edit starter messages for all selected surfaces of one post.

        The persisted surface rows are the authority for target selection.
        Missing or failing Discord targets are isolated so one broken copy cannot
        prevent the remaining local surfaces from receiving the mutation.
        """
        summary = LocalDiscordMutationFanoutSummary()
        for (
            surface
        ) in self.database.local_community_surfaces.list_local_community_thread_surfaces(
            getattr(thread_row, "id")
        ):
            if not self._surface_is_allowed(
                surface, getattr(thread_row, "local_community_id")
            ):
                continue
            if (
                source_surface_id is not None
                and getattr(surface, "id") == source_surface_id
            ):
                summary.skipped_source += 1
                continue
            summary.attempted += 1
            try:
                if adapter is not None:
                    await self._apply_normalized_mutation(
                        adapter=adapter,
                        thread_id=getattr(surface, "discord_thread_id"),
                        message_id=getattr(surface, "discord_starter_message_id"),
                        new_content=new_content,
                    )
                else:
                    await edit_discord_message(
                        bot=self.bot,
                        discord_thread_id=getattr(surface, "discord_thread_id"),
                        discord_message_id=getattr(surface, "discord_starter_message_id"),
                        new_content=new_content, preserve_header=True,
                    )
                summary.applied += 1
            except Exception:
                summary.failed += 1
                logger.exception(
                    "Failed to edit local thread surface thread_id=%s surface_id=%s",
                    getattr(thread_row, "id"),
                    getattr(surface, "id"),
                )
        return summary

    async def fanout_message_edit(
        self,
        *,
        message_row: object,
        source_surface_id: int | None,
        new_content: str,
        adapter: PlatformAdapterPort | None = None,
    ) -> LocalDiscordMutationFanoutSummary:
        """Edit message surfaces for all selected copies of one comment."""
        summary = LocalDiscordMutationFanoutSummary()
        canonical_thread = self.database.local_community_content.get_local_community_thread_by_id(
            getattr(message_row, "local_community_thread_id")
        )
        if canonical_thread is None:
            return summary
        for (
            surface
        ) in self.database.local_community_surfaces.list_local_community_message_surfaces(
            getattr(message_row, "id")
        ):
            thread_surface = self.database.local_community_surfaces.get_local_community_thread_surface_by_id(
                getattr(surface, "local_community_thread_surface_id")
            )
            if (thread_surface is None
                or getattr(thread_surface, "local_community_thread_id") != canonical_thread.id
                or getattr(surface, "role") != getattr(thread_surface, "role")
                or getattr(surface, "local_subscriber_id", None) != getattr(thread_surface, "local_subscriber_id", None)
                or not self._surface_is_allowed(
                    thread_surface, canonical_thread.local_community_id
                )):
                summary.skipped_missing_thread += 1
                continue
            if (
                source_surface_id is not None
                and getattr(surface, "id") == source_surface_id
            ):
                summary.skipped_source += 1
                continue
            summary.attempted += 1
            try:
                if adapter is not None:
                    await self._apply_normalized_mutation(
                        adapter=adapter,
                        thread_id=getattr(thread_surface, "discord_thread_id"),
                        message_id=getattr(surface, "discord_message_id"),
                        new_content=new_content,
                    )
                else:
                    await edit_discord_message(
                        bot=self.bot,
                        discord_thread_id=getattr(thread_surface, "discord_thread_id"),
                        discord_message_id=getattr(surface, "discord_message_id"),
                        new_content=new_content, preserve_header=True,
                    )
                summary.applied += 1
            except Exception:
                summary.failed += 1
                logger.exception(
                    "Failed to edit local message surface message_id=%s surface_id=%s",
                    getattr(message_row, "id"),
                    getattr(surface, "id"),
                )
        return summary

    async def fanout_thread_starter_delete(
        self,
        *,
        thread_row: object,
        source_surface_id: int | None,
        adapter: PlatformAdapterPort | None = None,
    ) -> LocalDiscordMutationFanoutSummary:
        """Mark starter messages deleted for all selected surfaces of one post."""
        summary = LocalDiscordMutationFanoutSummary()
        for (
            surface
        ) in self.database.local_community_surfaces.list_local_community_thread_surfaces(
            getattr(thread_row, "id")
        ):
            if not self._surface_is_allowed(
                surface, getattr(thread_row, "local_community_id")
            ):
                continue
            if (
                source_surface_id is not None
                and getattr(surface, "id") == source_surface_id
            ):
                summary.skipped_source += 1
                continue
            summary.attempted += 1
            try:
                if adapter is not None:
                    await self._apply_normalized_mutation(
                        adapter=adapter,
                        thread_id=getattr(surface, "discord_thread_id"),
                        message_id=getattr(surface, "discord_starter_message_id"),
                        new_content=None,
                    )
                else:
                    await mark_discord_message_deleted(
                        bot=self.bot,
                        discord_thread_id=getattr(surface, "discord_thread_id"),
                        discord_message_id=getattr(surface, "discord_starter_message_id"),
                    )
                summary.applied += 1
            except Exception:
                summary.failed += 1
                logger.exception(
                    "Failed to delete-mark local thread surface thread_id=%s surface_id=%s",
                    getattr(thread_row, "id"),
                    getattr(surface, "id"),
                )
        return summary

    async def fanout_message_delete(
        self,
        *,
        message_row: object,
        source_surface_id: int | None,
        adapter: PlatformAdapterPort | None = None,
    ) -> LocalDiscordMutationFanoutSummary:
        """Mark message surfaces deleted for all selected copies of one comment."""
        summary = LocalDiscordMutationFanoutSummary()
        canonical_thread = self.database.local_community_content.get_local_community_thread_by_id(
            getattr(message_row, "local_community_thread_id")
        )
        if canonical_thread is None:
            return summary
        for (
            surface
        ) in self.database.local_community_surfaces.list_local_community_message_surfaces(
            getattr(message_row, "id")
        ):
            thread_surface = self.database.local_community_surfaces.get_local_community_thread_surface_by_id(
                getattr(surface, "local_community_thread_surface_id")
            )
            if (thread_surface is None
                or getattr(thread_surface, "local_community_thread_id") != canonical_thread.id
                or getattr(surface, "role") != getattr(thread_surface, "role")
                or getattr(surface, "local_subscriber_id", None) != getattr(thread_surface, "local_subscriber_id", None)
                or not self._surface_is_allowed(
                    thread_surface, canonical_thread.local_community_id
                )):
                summary.skipped_missing_thread += 1
                continue
            if (
                source_surface_id is not None
                and getattr(surface, "id") == source_surface_id
            ):
                summary.skipped_source += 1
                continue
            summary.attempted += 1
            try:
                if adapter is not None:
                    await self._apply_normalized_mutation(
                        adapter=adapter,
                        thread_id=getattr(thread_surface, "discord_thread_id"),
                        message_id=getattr(surface, "discord_message_id"),
                        new_content=None,
                    )
                else:
                    await mark_discord_message_deleted(
                        bot=self.bot,
                        discord_thread_id=getattr(thread_surface, "discord_thread_id"),
                        discord_message_id=getattr(surface, "discord_message_id"),
                    )
                summary.applied += 1
            except Exception:
                summary.failed += 1
                logger.exception(
                    "Failed to delete-mark local message surface message_id=%s surface_id=%s",
                    getattr(message_row, "id"),
                    getattr(surface, "id"),
                )
        return summary


    def _select_targets(
        self,
        *,
        local_community: object,
        include_host: bool,
        source_forum_channel_id: int | None,
    ) -> list[LocalDiscordFanoutTarget]:
        """Select host and/or active local-subscriber fanout targets."""
        targets: list[LocalDiscordFanoutTarget] = []
        placement = self.database.platform_surfaces.get_discord_host_placement(
            getattr(local_community, "id")
        )
        host_forum_id = (
            int(placement.external_channel_id) if placement is not None else None
        )
        if (
            include_host
            and host_forum_id is not None
            and source_forum_channel_id != host_forum_id
        ):
            targets.append(
                LocalDiscordFanoutTarget(
                    role="host",
                    discord_forum_channel_id=host_forum_id,
                    discord_guild_id=(
                        int(placement.external_guild_id)
                        if placement.external_guild_id is not None
                        else None
                    ),
                    local_subscriber_id=None,
                )
            )
        for (
            subscriber
        ) in self.database.local_subscribers.list_discord_active_subscribers(
            getattr(local_community, "id")
        ):
            if getattr(subscriber, "status", "active") != "active":
                continue
            target_forum_id = getattr(subscriber, "discord_channel_id")
            if source_forum_channel_id == target_forum_id:
                continue
            targets.append(
                LocalDiscordFanoutTarget(
                    role="local_subscriber",
                    discord_forum_channel_id=target_forum_id,
                    discord_guild_id=getattr(subscriber, "discord_guild_id", None),
                    local_subscriber_id=getattr(subscriber, "id"),
                )
            )
        return targets
