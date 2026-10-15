from __future__ import annotations

from .platform_surfaces import PlatformSurfaceRepository, discord_copy_query

from sqlalchemy import select

from ...models import (
    MessageMapping,
)
from .base import BaseRepository

"""ActivityPub activity/object to Discord message mapping persistence."""


class MessageMappingRepository(BaseRepository):
    """Persist the message mappings domain."""

    def create_message_mapping(
        self,
        *,
        source_platform: str,
        source_id: str,
        activity_id: str,
        object_id: str,
        actor_url: str,
        community_actor_url: str,
        discord_channel_id: int | None,
        discord_message_id: int | None,
    ) -> MessageMapping:
        """Create the generic dedup record used by later AP publish flows."""
        with self.session() as session:
            mapping = MessageMapping(
                **(
                    PlatformSurfaceRepository.discord_source_location(
                        session, discord_channel_id
                    )
                    if source_platform == "discord"
                    else {}
                ),
                source_platform=source_platform,
                source_id=source_id,
                activity_id=activity_id,
                object_id=object_id,
                actor_url=actor_url,
                community_actor_url=community_actor_url,
                discord_channel_id=discord_channel_id,
                discord_message_id=discord_message_id,
            )
            session.add(mapping)
            session.flush()
            return mapping

    def get_message_mapping_by_source(
        self, source_platform: str, source_id: str
    ) -> MessageMapping | None:
        """Load one dedup mapping by its immutable source identity."""
        with self.session() as session:
            query = (
                discord_copy_query(MessageMapping, source=True)
                if source_platform == "discord"
                else select(MessageMapping).where(
                    MessageMapping.source_connector_id.is_(None)
                )
            )
            return session.scalar(
                query.where(
                    MessageMapping.source_platform == source_platform,
                    MessageMapping.source_id == source_id,
                )
            )

    def get_message_mapping_by_activity_id(
        self, activity_id: str
    ) -> MessageMapping | None:
        """Load a generic mapping row by ActivityPub activity ID."""
        with self.session() as session:
            return session.scalar(
                select(MessageMapping).where(MessageMapping.activity_id == activity_id)
            )

    def get_message_mapping_by_object_id(self, object_id: str) -> MessageMapping | None:
        """Load a generic mapping row by ActivityPub object ID."""
        with self.session() as session:
            return session.scalar(
                select(MessageMapping).where(MessageMapping.object_id == object_id)
            )

    def get_message_mapping_by_discord_message_id(
        self, discord_message_id: int
    ) -> MessageMapping | None:
        """Load a generic mapping row by Discord message ID."""
        with self.session() as session:
            return session.scalar(
                discord_copy_query(MessageMapping, source=True).where(
                    MessageMapping.discord_message_id == discord_message_id
                )
            )
