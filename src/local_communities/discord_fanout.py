"""Local Discord fanout for bridge-owned local communities.

This module owns Stage 3 local-subscriber copy creation. It deliberately only
creates Discord surfaces for already-canonical community activities; it never
publishes ActivityPub, never chooses remote subscribers, and never treats local
subscriber forums as source forums.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from ..content_sync.inbound_references import build_message_reference
from ..db import Database

logger = logging.getLogger(__name__)


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


class LocalCommunityDiscordFanout:
    """Create local-subscriber Discord surfaces for canonical community activity."""

    def __init__(self, *, database: Database, bot: object) -> None:
        """Initialise the fanout helper with persistence and Discord boundaries."""
        self.database = database
        self.bot = bot

    async def fanout_thread_to_local_subscribers(
        self,
        *,
        local_community: object,
        thread_row: object,
        title: str,
        content: str,
        source_forum_channel_id: int | None,
    ) -> LocalDiscordFanoutSummary:
        """Create missing local-subscriber thread surfaces for one post.

        Surface rows are the idempotency boundary. A replayed source event can
        call this method again; existing target surfaces are skipped, while a
        target that failed earlier can be retried because it has no surface row.
        """
        summary = LocalDiscordFanoutSummary()
        subscribers = self.database.list_local_subscribers(
            getattr(local_community, "id")
        )
        for subscriber in subscribers:
            if getattr(subscriber, "status", "active") != "active":
                continue
            target_forum_id = getattr(subscriber, "discord_channel_id")
            if source_forum_channel_id == target_forum_id:
                continue
            existing = self.database.get_local_community_thread_surface(
                local_community_thread_id=getattr(thread_row, "id"),
                discord_forum_channel_id=target_forum_id,
            )
            if existing is not None:
                summary.skipped_existing += 1
                continue
            summary.attempted += 1
            try:
                forum = await self.bot.fetch_forum_channel(target_forum_id)
                created = await forum.create_thread(name=title, content=content)
                created_thread, starter_message = self._unpack_created_thread(created)
                self.database.create_local_community_thread_surface(
                    local_community_thread_id=getattr(thread_row, "id"),
                    discord_forum_channel_id=target_forum_id,
                    discord_thread_id=getattr(created_thread, "id"),
                    discord_starter_message_id=getattr(starter_message, "id"),
                    role="local_subscriber",
                    local_subscriber_id=getattr(subscriber, "id"),
                )
                summary.delivered += 1
            except Exception:
                summary.failed += 1
                logger.exception(
                    "Failed to create local-subscriber thread surface community=%s subscriber=%s",
                    getattr(local_community, "id"),
                    getattr(subscriber, "id", None),
                )
        return summary

    async def fanout_message_to_local_subscribers(
        self,
        *,
        local_community: object,
        thread_row: object,
        message_row: object,
        content: str,
        source_forum_channel_id: int | None,
    ) -> LocalDiscordFanoutSummary:
        """Create missing local-subscriber message surfaces for one comment.

        Parent Discord message ids are resolved per target surface. Missing
        nested parent surfaces skip that target instead of flattening the reply
        tree to the root starter message.
        """
        summary = LocalDiscordFanoutSummary()
        subscribers = self.database.list_local_subscribers(
            getattr(local_community, "id")
        )
        for subscriber in subscribers:
            if getattr(subscriber, "status", "active") != "active":
                continue
            target_forum_id = getattr(subscriber, "discord_channel_id")
            if source_forum_channel_id == target_forum_id:
                continue
            target_thread_surface = self.database.get_local_community_thread_surface(
                local_community_thread_id=getattr(thread_row, "id"),
                discord_forum_channel_id=target_forum_id,
            )
            if target_thread_surface is None:
                summary.skipped_missing_thread_surface += 1
                continue
            existing = self.database.get_local_community_message_surface(
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
                created_message = await discord_thread.send(content, **send_kwargs)
                self.database.create_local_community_message_surface(
                    local_community_message_id=getattr(message_row, "id"),
                    local_community_thread_surface_id=getattr(target_thread_surface, "id"),
                    discord_forum_channel_id=target_forum_id,
                    discord_message_id=getattr(created_message, "id"),
                    parent_discord_message_id=parent_message_id,
                    role="local_subscriber",
                    local_subscriber_id=getattr(subscriber, "id"),
                )
                summary.delivered += 1
            except Exception:
                summary.failed += 1
                logger.exception(
                    "Failed to create local-subscriber message surface community=%s subscriber=%s",
                    getattr(local_community, "id"),
                    getattr(subscriber, "id", None),
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
        parent_message = self.database.get_local_community_message_by_ap_object_id(
            parent_ap_object_id
        )
        if parent_message is None:
            return MISSING_PARENT_SURFACE
        parent_surface = self.database.get_local_community_message_surface(
            local_community_message_id=getattr(parent_message, "id"),
            local_community_thread_surface_id=getattr(target_thread_surface, "id"),
        )
        if parent_surface is None:
            return MISSING_PARENT_SURFACE
        return getattr(parent_surface, "discord_message_id")

    @staticmethod
    def _unpack_created_thread(created: object) -> tuple[object, object]:
        """Normalize both Discord `create_thread()` result shapes used by fakes."""
        if isinstance(created, tuple):
            return created[0], created[1]
        return getattr(created, "thread"), getattr(created, "message")
