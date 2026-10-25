from __future__ import annotations

from .platform_surfaces import PlatformSurfaceRepository, discord_copy_query


from sqlalchemy import select

from ...models import (
    LocalCommunityMessage,
    LocalCommunityMessageSurface,
    LocalCommunityThread,
    LocalCommunityThreadSurface,
    ChannelSurface,
)
from .base import BaseRepository

"""Discord surface persistence for local-community content."""


class LocalCommunitySurfaceRepository(BaseRepository):
    """Persist the local community surfaces domain."""

    def __init__(self, session_provider, immediate_session_provider) -> None:
        """Bind source-independent SQLite writes to the database writer boundary."""
        super().__init__(session_provider)
        self._immediate_session_provider = immediate_session_provider

    def create_local_community_thread_surface(
        self,
        *,
        local_community_thread_id: int,
        discord_forum_channel_id: int,
        discord_thread_id: int,
        discord_starter_message_id: int,
        role: str,
        local_subscriber_id: int | None = None,
    ) -> LocalCommunityThreadSurface:
        """Persist one explicit Discord thread surface for a canonical thread."""
        with self._immediate_session_provider() as session:
            location = PlatformSurfaceRepository.discord_copy_location(
                session, discord_forum_channel_id
            )
            thread = session.get(LocalCommunityThread, local_community_thread_id)
            if thread is None:
                raise ValueError("Missing canonical community thread")
            prior = session.scalar(select(LocalCommunityThreadSurface).where(
                LocalCommunityThreadSurface.local_community_thread_id == local_community_thread_id,
                LocalCommunityThreadSurface.channel_surface_id == location['channel_surface_id'],
            ))
            if prior is not None:
                # The same SDK receipt may be committed by independent handlers
                # after per-key singleflight, but another Discord copy may not.
                if (prior.connector_id, prior.channel_surface_id,
                    prior.external_thread_id, prior.external_starter_message_id,
                    prior.discord_forum_channel_id, prior.discord_thread_id,
                    prior.discord_starter_message_id, prior.role, prior.local_subscriber_id) != (
                    location['connector_id'], location['channel_surface_id'],
                    str(discord_thread_id), str(discord_starter_message_id),
                    discord_forum_channel_id, discord_thread_id,
                    discord_starter_message_id, role, local_subscriber_id):
                    raise ValueError('local community sibling thread receipt conflict')
                return prior
            # A physical thread/starter cannot belong to two canonical posts.
            if session.scalar(select(LocalCommunityThreadSurface).where(
                LocalCommunityThreadSurface.connector_id == location['connector_id'],
                (LocalCommunityThreadSurface.external_thread_id == str(discord_thread_id))
                | (LocalCommunityThreadSurface.external_starter_message_id == str(discord_starter_message_id)),
            )) is not None:
                raise ValueError('local community sibling physical thread identity conflict')
            surface = LocalCommunityThreadSurface(
                **location,
                external_thread_id=str(discord_thread_id),
                external_starter_message_id=str(discord_starter_message_id),
                local_community_thread_id=local_community_thread_id,
                discord_forum_channel_id=discord_forum_channel_id,
                discord_thread_id=discord_thread_id,
                discord_starter_message_id=discord_starter_message_id,
                role=role,
                local_subscriber_id=local_subscriber_id,
            )
            session.add(surface)
            session.flush()
            return surface

    def get_local_community_thread_surface(
        self, *, local_community_thread_id: int, discord_forum_channel_id: int
    ) -> LocalCommunityThreadSurface | None:
        """Return one thread surface for a canonical thread and forum target."""
        with self.session() as session:
            return session.scalar(
                discord_copy_query(LocalCommunityThreadSurface)
                .join(ChannelSurface, ChannelSurface.id == LocalCommunityThreadSurface.channel_surface_id)
                .where(
                    LocalCommunityThreadSurface.local_community_thread_id
                    == local_community_thread_id,
                    ChannelSurface.external_channel_id == str(discord_forum_channel_id),
                    ChannelSurface.connector_id == LocalCommunityThreadSurface.connector_id,
                    ChannelSurface.kind == 'forum',
                )
            )

    def get_local_community_thread_surface_by_discord_thread_id(
        self, discord_thread_id: int
    ) -> LocalCommunityThreadSurface | None:
        """Load the thread surface row for one Discord thread id."""
        with self.session() as session:
            return session.scalar(
                discord_copy_query(LocalCommunityThreadSurface).where(
                    LocalCommunityThreadSurface.discord_thread_id == discord_thread_id
                )
            )

    def get_local_community_thread_surface_by_starter_message_id(
        self, discord_starter_message_id: int
    ) -> LocalCommunityThreadSurface | None:
        """Load the thread surface row for one Discord starter message id."""
        with self.session() as session:
            return session.scalar(
                discord_copy_query(LocalCommunityThreadSurface).where(
                    LocalCommunityThreadSurface.discord_starter_message_id
                    == discord_starter_message_id
                )
            )

    def list_local_community_thread_surfaces(
        self, local_community_thread_id: int
    ) -> list[LocalCommunityThreadSurface]:
        """List every Discord thread surface for one canonical thread."""
        with self.session() as session:
            return list(
                session.scalars(
                    discord_copy_query(LocalCommunityThreadSurface)
                    .where(
                        LocalCommunityThreadSurface.local_community_thread_id
                        == local_community_thread_id
                    )
                    .order_by(
                        LocalCommunityThreadSurface.created_at,
                        LocalCommunityThreadSurface.id,
                    )
                )
            )

    def get_host_local_community_thread_surface(
        self, local_community_thread_id: int
    ) -> LocalCommunityThreadSurface | None:
        """Return the host forum thread surface for one canonical thread."""
        with self.session() as session:
            return session.scalar(
                discord_copy_query(LocalCommunityThreadSurface).where(
                    LocalCommunityThreadSurface.local_community_thread_id
                    == local_community_thread_id,
                    LocalCommunityThreadSurface.role == "host",
                )
            )

    def create_local_community_message_surface(
        self,
        *,
        local_community_message_id: int,
        local_community_thread_surface_id: int,
        discord_forum_channel_id: int,
        discord_message_id: int,
        parent_discord_message_id: int | None,
        role: str,
        local_subscriber_id: int | None = None,
    ) -> LocalCommunityMessageSurface:
        """Persist one explicit Discord message surface for a canonical comment."""
        with self._immediate_session_provider() as session:
            thread_surface = session.get(
                LocalCommunityThreadSurface, local_community_thread_surface_id
            )
            message = session.get(LocalCommunityMessage, local_community_message_id)
            if thread_surface is None or message is None:
                raise ValueError("Missing canonical message or parent thread surface")
            location = PlatformSurfaceRepository.discord_copy_location(
                session, discord_forum_channel_id
            )
            if (
                location["channel_surface_id"] != thread_surface.channel_surface_id
                or location["connector_id"] != thread_surface.connector_id
                or message.local_community_thread_id != thread_surface.local_community_thread_id
            ):
                raise ValueError("Message must belong to the selected canonical thread and physical channel")
            prior = session.scalar(select(LocalCommunityMessageSurface).where(
                LocalCommunityMessageSurface.local_community_message_id == local_community_message_id,
                LocalCommunityMessageSurface.local_community_thread_surface_id == local_community_thread_surface_id,
            ))
            if prior is not None:
                # Refuse changed physical replies or target owners despite a
                # valid canonical AP message; preserve the first verified copy.
                if (prior.connector_id, prior.channel_surface_id,
                    prior.external_message_id, prior.external_parent_message_id,
                    prior.discord_forum_channel_id, prior.discord_message_id,
                    prior.parent_discord_message_id, prior.role, prior.local_subscriber_id) != (
                    location['connector_id'], location['channel_surface_id'],
                    str(discord_message_id),
                    str(parent_discord_message_id) if parent_discord_message_id is not None else None,
                    discord_forum_channel_id, discord_message_id,
                    parent_discord_message_id, role, local_subscriber_id):
                    raise ValueError('local community sibling message receipt conflict')
                return prior
            if session.scalar(select(LocalCommunityMessageSurface).where(
                LocalCommunityMessageSurface.connector_id == location['connector_id'],
                LocalCommunityMessageSurface.external_message_id == str(discord_message_id),
            )) is not None:
                raise ValueError('local community sibling physical message identity conflict')
            surface = LocalCommunityMessageSurface(
                connector_id=thread_surface.connector_id,
                channel_surface_id=thread_surface.channel_surface_id,
                external_message_id=str(discord_message_id),
                external_parent_message_id=(
                    str(parent_discord_message_id)
                    if parent_discord_message_id is not None
                    else None
                ),
                local_community_message_id=local_community_message_id,
                local_community_thread_surface_id=local_community_thread_surface_id,
                discord_forum_channel_id=discord_forum_channel_id,
                discord_message_id=discord_message_id,
                parent_discord_message_id=parent_discord_message_id,
                role=role,
                local_subscriber_id=local_subscriber_id,
            )
            session.add(surface)
            session.flush()
            return surface

    def get_local_community_message_surface(
        self, *, local_community_message_id: int, local_community_thread_surface_id: int
    ) -> LocalCommunityMessageSurface | None:
        """Return one message surface for a canonical comment and thread surface."""
        with self.session() as session:
            return session.scalar(
                discord_copy_query(LocalCommunityMessageSurface).where(
                    LocalCommunityMessageSurface.local_community_message_id
                    == local_community_message_id,
                    LocalCommunityMessageSurface.local_community_thread_surface_id
                    == local_community_thread_surface_id,
                )
            )

    def get_local_community_message_surface_by_discord_message_id(
        self, discord_message_id: int
    ) -> LocalCommunityMessageSurface | None:
        """Load the message surface row for one Discord message id."""
        with self.session() as session:
            return session.scalar(
                discord_copy_query(LocalCommunityMessageSurface).where(
                    LocalCommunityMessageSurface.discord_message_id
                    == discord_message_id
                )
            )

    def list_local_community_message_surfaces(
        self, local_community_message_id: int
    ) -> list[LocalCommunityMessageSurface]:
        """List every Discord message surface for one canonical comment."""
        with self.session() as session:
            return list(
                session.scalars(
                    discord_copy_query(LocalCommunityMessageSurface)
                    .where(
                        LocalCommunityMessageSurface.local_community_message_id
                        == local_community_message_id
                    )
                    .order_by(
                        LocalCommunityMessageSurface.created_at,
                        LocalCommunityMessageSurface.id,
                    )
                )
            )

    def get_host_local_community_message_surface(
        self, local_community_message_id: int
    ) -> LocalCommunityMessageSurface | None:
        """Return the host forum message surface for one canonical comment."""
        with self.session() as session:
            return session.scalar(
                discord_copy_query(LocalCommunityMessageSurface).where(
                    LocalCommunityMessageSurface.local_community_message_id
                    == local_community_message_id,
                    LocalCommunityMessageSurface.role == "host",
                )
            )

    def get_local_community_thread_surface_by_id(
        self, local_community_thread_surface_id: int
    ) -> LocalCommunityThreadSurface | None:
        """Load one local-community thread surface by primary key."""
        with self.session() as session:
            return session.get(
                LocalCommunityThreadSurface, local_community_thread_surface_id
            )

    def get_local_community_thread_for_surface(
        self, local_community_thread_surface_id: int
    ) -> LocalCommunityThread | None:
        """Resolve the canonical thread that owns one Discord thread surface."""
        with self.session() as session:
            surface = session.get(
                LocalCommunityThreadSurface, local_community_thread_surface_id
            )
            if surface is None:
                return None
            return session.get(LocalCommunityThread, surface.local_community_thread_id)

    def get_local_community_message_for_surface(
        self, local_community_message_surface_id: int
    ) -> LocalCommunityMessage | None:
        """Resolve the canonical message that owns one Discord message surface."""
        with self.session() as session:
            surface = session.get(
                LocalCommunityMessageSurface, local_community_message_surface_id
            )
            if surface is None:
                return None
            return session.get(
                LocalCommunityMessage, surface.local_community_message_id
            )

    def get_thread_surface_by_external_id(self, connector_key, external_thread_id):
        """Lookup a historical thread copy using its complete connector identity."""
        from ...models import PlatformConnector

        with self.session() as session:
            return session.scalar(
                select(LocalCommunityThreadSurface)
                .join(
                    PlatformConnector,
                    PlatformConnector.id == LocalCommunityThreadSurface.connector_id,
                )
                .where(
                    PlatformConnector.connector_key == connector_key,
                    LocalCommunityThreadSurface.external_thread_id
                    == external_thread_id,
                )
            )

    def get_message_surface_by_external_id(self, connector_key, external_message_id):
        """Lookup a historical message independently of current channel ownership."""
        from ...models import PlatformConnector

        with self.session() as session:
            return session.scalar(
                select(LocalCommunityMessageSurface)
                .join(
                    PlatformConnector,
                    PlatformConnector.id == LocalCommunityMessageSurface.connector_id,
                )
                .where(
                    PlatformConnector.connector_key == connector_key,
                    LocalCommunityMessageSurface.external_message_id
                    == external_message_id,
                )
            )
