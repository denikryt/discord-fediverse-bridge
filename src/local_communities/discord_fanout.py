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
from ..content_sync.inbound_references import build_message_reference
from ..db import Database

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


class MissingParentSurface:
    """Sentinel used when a nested reply cannot be represented on a target."""


MISSING_PARENT_SURFACE = MissingParentSurface()


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

    Stage 5 mutation fanout is best-effort per persisted surface.  The source
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

    def __init__(self, *, database: Database, bot: object, policy_service: BridgePolicyService) -> None:
        """Initialise the fanout helper with persistence and Discord boundaries."""
        self.database = database
        self.bot = bot
        self.policy_service = policy_service

    def _forum_guild_id(self, forum_channel_id: int) -> int | None:
        """Resolve a target forum's owning guild from persisted routing state."""
        subscriber = self.database.local_subscribers.get_local_subscriber_by_channel(forum_channel_id)
        if subscriber is not None:
            return _valid_discord_guild_id(getattr(subscriber, "discord_guild_id", None))
        community = self.database.local_communities.get_local_community_by_forum_channel_id(forum_channel_id)
        if community is None:
            return None
        return _valid_discord_guild_id(getattr(community, "discord_guild_id", None))

    def _surface_is_allowed(self, forum_channel_id: int) -> bool:
        """Fail closed before mutating a persisted Discord surface."""
        try:
            guild_id = self._forum_guild_id(forum_channel_id)
            if guild_id is None:
                logger.warning(
                    "Skipping local Discord surface with missing or invalid routing metadata forum_channel_id=%s",
                    forum_channel_id,
                )
                return False
            return self.policy_service.snapshot().is_discord_guild_allowed(guild_id)
        except Exception:
            logger.exception(
                "Failed to validate local Discord surface routing metadata forum_channel_id=%s",
                forum_channel_id,
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
            return self.policy_service.snapshot().is_discord_guild_allowed(guild_id)
        except Exception:
            logger.exception(
                "Failed to evaluate local Discord fanout policy forum_channel_id=%s role=%s",
                target.discord_forum_channel_id,
                target.role,
            )
            return False

    async def fanout_thread_to_local_subscribers(
        self,
        *,
        local_community: object,
        thread_row: object,
        title: str,
        content: str,
        author_display_name: str | None,
        source_forum_channel_id: int | None,
    ) -> LocalDiscordFanoutSummary:
        """Create missing local-subscriber thread surfaces for one post."""
        return await self.fanout_thread(
            local_community=local_community,
            thread_row=thread_row,
            title=title,
            content=content,
            author_display_name=author_display_name,
            source_forum_channel_id=source_forum_channel_id,
            include_host=False,
        )

    async def fanout_thread(
        self,
        *,
        local_community: object,
        thread_row: object,
        title: str,
        content: str,
        author_display_name: str | None,
        source_forum_channel_id: int | None,
        include_host: bool,
    ) -> LocalDiscordFanoutSummary:
        """Create missing Discord thread surfaces for the selected local targets.

        Surface rows are the idempotency boundary.  Replayed source events can
        call this method again; existing target surfaces are skipped, while a
        failed target without a surface can be retried safely.
        """
        summary = LocalDiscordFanoutSummary()
        for target in self._select_targets(
            local_community=local_community,
            include_host=include_host,
            source_forum_channel_id=source_forum_channel_id,
        ):
            if not self._target_is_allowed(target):
                continue
            existing = self.database.local_community_surfaces.get_local_community_thread_surface(
                local_community_thread_id=getattr(thread_row, "id"),
                discord_forum_channel_id=target.discord_forum_channel_id,
            )
            if existing is not None:
                summary.skipped_existing += 1
                continue
            summary.attempted += 1
            try:
                forum = await self.bot.fetch_forum_channel(target.discord_forum_channel_id)
                created = await forum.create_thread(
                    name=title,
                    content=_format_local_mirror_body(
                        author_display_name=author_display_name,
                        content=content,
                    ),
                )
                created_thread, starter_message = self._unpack_created_thread(created)
                self.database.local_community_surfaces.create_local_community_thread_surface(
                    local_community_thread_id=getattr(thread_row, "id"),
                    discord_forum_channel_id=target.discord_forum_channel_id,
                    discord_thread_id=getattr(created_thread, "id"),
                    discord_starter_message_id=getattr(starter_message, "id"),
                    role=target.role,
                    local_subscriber_id=target.local_subscriber_id,
                )
                summary.delivered += 1
            except Exception:
                summary.failed += 1
                logger.exception(
                    "Failed to create local thread surface community=%s target_forum=%s role=%s",
                    getattr(local_community, "id"),
                    target.discord_forum_channel_id,
                    target.role,
                )
        return summary

    async def fanout_message_to_local_subscribers(
        self,
        *,
        local_community: object,
        thread_row: object,
        message_row: object,
        content: str,
        author_display_name: str | None,
        source_forum_channel_id: int | None,
    ) -> LocalDiscordFanoutSummary:
        """Create missing local-subscriber message surfaces for one comment."""
        return await self.fanout_message(
            local_community=local_community,
            thread_row=thread_row,
            message_row=message_row,
            content=content,
            author_display_name=author_display_name,
            source_forum_channel_id=source_forum_channel_id,
            include_host=False,
        )

    async def fanout_message(
        self,
        *,
        local_community: object,
        thread_row: object,
        message_row: object,
        content: str,
        author_display_name: str | None,
        source_forum_channel_id: int | None,
        include_host: bool,
    ) -> LocalDiscordFanoutSummary:
        """Create missing Discord message surfaces for the selected targets.

        Parent Discord message ids are resolved per target surface.  Missing
        nested parent surfaces skip that target instead of flattening the reply
        tree to the root starter message.
        """
        summary = LocalDiscordFanoutSummary()
        for target in self._select_targets(
            local_community=local_community,
            include_host=include_host,
            source_forum_channel_id=source_forum_channel_id,
        ):
            if not self._target_is_allowed(target):
                continue
            target_thread_surface = self.database.local_community_surfaces.get_local_community_thread_surface(
                local_community_thread_id=getattr(thread_row, "id"),
                discord_forum_channel_id=target.discord_forum_channel_id,
            )
            if target_thread_surface is None:
                summary.skipped_missing_thread_surface += 1
                continue
            existing = self.database.local_community_surfaces.get_local_community_message_surface(
                local_community_message_id=getattr(message_row, "id"),
                local_community_thread_surface_id=getattr(target_thread_surface, "id"),
            )
            if existing is not None:
                summary.skipped_existing += 1
                continue
            parent_message_id = self.resolve_parent_message_id(
                thread_row=thread_row,
                message_row=message_row,
                target_thread_surface=target_thread_surface,
            )
            if parent_message_id is MISSING_PARENT_SURFACE:
                summary.skipped_missing_parent_surface += 1
                continue
            summary.attempted += 1
            try:
                discord_thread = await self.bot.get_thread_by_id(
                    getattr(target_thread_surface, "discord_thread_id")
                )
                send_kwargs: dict[str, object] = {}
                if parent_message_id is not None:
                    send_kwargs["reference"] = build_message_reference(
                        discord_thread=discord_thread,
                        message_id=parent_message_id,
                    )
                created_message = await discord_thread.send(
                    _format_local_mirror_body(
                        author_display_name=author_display_name,
                        content=content,
                    ),
                    **send_kwargs,
                )
                self.database.local_community_surfaces.create_local_community_message_surface(
                    local_community_message_id=getattr(message_row, "id"),
                    local_community_thread_surface_id=getattr(target_thread_surface, "id"),
                    discord_forum_channel_id=target.discord_forum_channel_id,
                    discord_message_id=getattr(created_message, "id"),
                    parent_discord_message_id=parent_message_id,
                    role=target.role,
                    local_subscriber_id=target.local_subscriber_id,
                )
                summary.delivered += 1
            except Exception:
                summary.failed += 1
                logger.exception(
                    "Failed to create local message surface community=%s target_forum=%s role=%s",
                    getattr(local_community, "id"),
                    target.discord_forum_channel_id,
                    target.role,
                )
        return summary

    async def fanout_thread_starter_edit(
        self,
        *,
        thread_row: object,
        source_surface_id: int | None,
        new_content: str,
    ) -> LocalDiscordMutationFanoutSummary:
        """Edit starter messages for all selected surfaces of one post.

        The persisted surface rows are the authority for target selection.
        Missing or failing Discord targets are isolated so one broken copy cannot
        prevent the remaining local surfaces from receiving the mutation.
        """
        summary = LocalDiscordMutationFanoutSummary()
        for surface in self.database.local_community_surfaces.list_local_community_thread_surfaces(getattr(thread_row, "id")):
            if not self._surface_is_allowed(getattr(surface, "discord_forum_channel_id")):
                continue
            if source_surface_id is not None and getattr(surface, "id") == source_surface_id:
                summary.skipped_source += 1
                continue
            summary.attempted += 1
            try:
                await edit_discord_message(
                    bot=self.bot,
                    discord_thread_id=getattr(surface, "discord_thread_id"),
                    discord_message_id=getattr(surface, "discord_starter_message_id"),
                    new_content=new_content,
                    preserve_header=True,
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
    ) -> LocalDiscordMutationFanoutSummary:
        """Edit message surfaces for all selected copies of one comment."""
        summary = LocalDiscordMutationFanoutSummary()
        for surface in self.database.local_community_surfaces.list_local_community_message_surfaces(getattr(message_row, "id")):
            thread_surface = self.database.local_community_surfaces.get_local_community_thread_surface_by_id(
                getattr(surface, "local_community_thread_surface_id")
            )
            if thread_surface is None or not self._surface_is_allowed(getattr(thread_surface, "discord_forum_channel_id")):
                summary.skipped_missing_thread += 1
                continue
            if source_surface_id is not None and getattr(surface, "id") == source_surface_id:
                summary.skipped_source += 1
                continue
            summary.attempted += 1
            try:
                await edit_discord_message(
                    bot=self.bot,
                    discord_thread_id=getattr(thread_surface, "discord_thread_id"),
                    discord_message_id=getattr(surface, "discord_message_id"),
                    new_content=new_content,
                    preserve_header=True,
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
    ) -> LocalDiscordMutationFanoutSummary:
        """Mark starter messages deleted for all selected surfaces of one post."""
        summary = LocalDiscordMutationFanoutSummary()
        for surface in self.database.local_community_surfaces.list_local_community_thread_surfaces(getattr(thread_row, "id")):
            if not self._surface_is_allowed(getattr(surface, "discord_forum_channel_id")):
                continue
            if source_surface_id is not None and getattr(surface, "id") == source_surface_id:
                summary.skipped_source += 1
                continue
            summary.attempted += 1
            try:
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
    ) -> LocalDiscordMutationFanoutSummary:
        """Mark message surfaces deleted for all selected copies of one comment."""
        summary = LocalDiscordMutationFanoutSummary()
        for surface in self.database.local_community_surfaces.list_local_community_message_surfaces(getattr(message_row, "id")):
            thread_surface = self.database.local_community_surfaces.get_local_community_thread_surface_by_id(
                getattr(surface, "local_community_thread_surface_id")
            )
            if thread_surface is None or not self._surface_is_allowed(getattr(thread_surface, "discord_forum_channel_id")):
                summary.skipped_missing_thread += 1
                continue
            if source_surface_id is not None and getattr(surface, "id") == source_surface_id:
                summary.skipped_source += 1
                continue
            summary.attempted += 1
            try:
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

    def resolve_parent_message_id(
        self,
        *,
        thread_row: object,
        message_row: object,
        target_thread_surface: object,
    ) -> int | MissingParentSurface | None:
        """Resolve the parent Discord message id inside one target thread surface."""
        parent_ap_object_id = getattr(message_row, "parent_ap_object_id", None)
        if parent_ap_object_id is None or parent_ap_object_id == getattr(thread_row, "ap_object_id"):
            return getattr(target_thread_surface, "discord_starter_message_id")
        parent_message = self.database.local_community_content.get_local_community_message_by_ap_object_id(parent_ap_object_id)
        if parent_message is None:
            return MISSING_PARENT_SURFACE
        parent_surface = self.database.local_community_surfaces.get_local_community_message_surface(
            local_community_message_id=getattr(parent_message, "id"),
            local_community_thread_surface_id=getattr(target_thread_surface, "id"),
        )
        if parent_surface is None:
            return MISSING_PARENT_SURFACE
        return getattr(parent_surface, "discord_message_id")

    def _select_targets(
        self,
        *,
        local_community: object,
        include_host: bool,
        source_forum_channel_id: int | None,
    ) -> list[LocalDiscordFanoutTarget]:
        """Select host and/or active local-subscriber fanout targets."""
        targets: list[LocalDiscordFanoutTarget] = []
        host_forum_id = getattr(local_community, "discord_forum_channel_id")
        if include_host and source_forum_channel_id != host_forum_id:
            targets.append(
                LocalDiscordFanoutTarget(
                    role="host",
                    discord_forum_channel_id=host_forum_id,
                    discord_guild_id=getattr(local_community, "discord_guild_id", None),
                    local_subscriber_id=None,
                )
            )
        for subscriber in self.database.local_subscribers.list_local_subscribers(getattr(local_community, "id")):
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

    @staticmethod
    def _unpack_created_thread(created: object) -> tuple[object, object]:
        """Normalize both Discord `create_thread()` result shapes used by fakes."""
        if isinstance(created, tuple):
            return created[0], created[1]
        return getattr(created, "thread"), getattr(created, "message")
