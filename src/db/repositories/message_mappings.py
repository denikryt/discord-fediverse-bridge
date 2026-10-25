from __future__ import annotations

from .platform_surfaces import PlatformSurfaceRepository, discord_copy_query

from sqlalchemy import select

from ...models import (
    MessageMapping,
    PlatformConnector,
)
from .base import BaseRepository
from ...platform_types import MessageRef

"""ActivityPub activity/object to Discord message mapping persistence."""


class MessageMappingRepository(BaseRepository):
    """Persist the message mappings domain."""

    def __init__(self, session_provider, immediate_session_provider) -> None:
        """Use SQLite writer reservation for independently retried mapping receipts."""
        super().__init__(session_provider)
        self._immediate_session_provider = immediate_session_provider

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
        source_message: MessageRef | None = None,
    ) -> MessageMapping:
        """Create a source mapping from exact typed evidence when it is supplied.

        The legacy Discord projection remains accepted for old unrelated
        Personal Feed paths; community neutral ingress supplies MessageRef.
        """
        if source_message is not None and (
            source_message.external_message_id != source_id
            or source_platform not in ('discord', 'fluxer')
        ):
            raise ValueError('Source identity differs from exact MessageRef')
        with self._immediate_session_provider() as session:
            if source_message is not None:
                connector = session.scalar(select(PlatformConnector).where(
                    PlatformConnector.connector_key == source_message.connector_key,
                    PlatformConnector.platform == source_platform,
                ))
                if connector is None:
                    raise ValueError('Unknown or mismatched source connector')
            # Both AP unique keys must identify the exact same persisted
            # object. A preliminary handler lookup is not atomic with INSERT.
            by_object = session.scalar(select(MessageMapping).where(
                MessageMapping.object_id == object_id))
            by_activity = session.scalar(select(MessageMapping).where(
                MessageMapping.activity_id == activity_id))
            if by_object is not None or by_activity is not None:
                if (by_object is None or by_activity is None or
                    by_object.id != by_activity.id):
                    raise ValueError('generic AP mapping identity conflict')
                if (source_message is not None and (
                    by_object.source_connector_id != connector.id
                    or by_object.source_external_container_id != source_message.external_container_id
                )):
                    raise ValueError('mapping immediate source container conflict')
                if (by_object.source_platform, by_object.source_id,
                    by_object.activity_id, by_object.object_id, by_object.actor_url,
                    by_object.community_actor_url, by_object.discord_channel_id,
                    by_object.discord_message_id) != (
                    source_platform, source_id, activity_id, object_id, actor_url,
                    community_actor_url, discord_channel_id, discord_message_id):
                    raise ValueError('generic AP mapping immutable receipt conflict')
                return by_object
            # The typed source identity must include the immediate physical
            # container, not the forum on which its thread was created.
            placement = (
                PlatformSurfaceRepository.discord_source_location(
                    session, discord_channel_id
                ) if source_platform == "discord" else {}
            )
            if source_message is not None:
                placement['source_connector_id'] = connector.id
                placement['source_external_container_id'] = source_message.external_container_id
            mapping = MessageMapping(
                **placement,
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

    def get_by_source_ref(self, source: MessageRef) -> MessageMapping | None:
        """Resolve only an exact connector, immediate container and message ID.

        The caller must already have authenticated the source physical event;
        the returned row is identity evidence, not authorization to mutate it.
        """
        if not isinstance(source, MessageRef):
            raise TypeError('typed MessageRef is required')
        with self.session() as session:
            return session.scalar(
                select(MessageMapping)
                .join(PlatformConnector, MessageMapping.source_connector_id == PlatformConnector.id)
                .where(
                    PlatformConnector.connector_key == source.connector_key,
                    MessageMapping.source_platform == PlatformConnector.platform,
                    MessageMapping.source_external_container_id == source.external_container_id,
                    MessageMapping.source_id == source.external_message_id,
                )
            )

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
            rows = list(session.scalars(
                query.where(
                    MessageMapping.source_platform == source_platform,
                    MessageMapping.source_id == source_id,
                ).limit(2)
            ))
            # Old unscoped lookup is restricted to rows without known complete
            # container identity. Typed rows require an explicit MessageRef.
            if len(rows) > 1 or any(r.source_external_container_id is not None for r in rows):
                raise ValueError('Unscoped source lookup is ambiguous or requires MessageRef')
            return rows[0] if rows else None

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
            rows = list(session.scalars(
                discord_copy_query(MessageMapping, source=True).where(
                    MessageMapping.discord_message_id == discord_message_id
                ).limit(2)
            ))
            if len(rows) > 1 or any(r.source_external_container_id is not None for r in rows):
                raise ValueError('Discord numeric-only lookup requires exact MessageRef')
            return rows[0] if rows else None
