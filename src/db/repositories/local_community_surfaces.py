from __future__ import annotations

from datetime import datetime

from sqlalchemy import select

from ...models import (
    LocalCommunity,
    LocalSubscriber,
    LocalCommunityMessage,
    LocalCommunityMessageSurface,
    LocalCommunityRelayDelivery,
    LocalCommunityRelaySourceActivity,
    LocalCommunityThread,
    LocalCommunityThreadSurface,
    RemoteSubscriber,
    utcnow,
)
from .base import BaseRepository


"""Discord surface persistence for local-community content."""


class LocalCommunitySurfaceRepository(BaseRepository):
    """Persist the local community surfaces domain."""

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
            with self.session() as session:
                surface = LocalCommunityThreadSurface(
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
                    select(LocalCommunityThreadSurface).where(
                        LocalCommunityThreadSurface.local_community_thread_id
                        == local_community_thread_id,
                        LocalCommunityThreadSurface.discord_forum_channel_id
                        == discord_forum_channel_id,
                    )
                )

    def get_local_community_thread_surface_by_discord_thread_id(
            self, discord_thread_id: int
        ) -> LocalCommunityThreadSurface | None:
            """Load the thread surface row for one Discord thread id."""
            with self.session() as session:
                return session.scalar(
                    select(LocalCommunityThreadSurface).where(
                        LocalCommunityThreadSurface.discord_thread_id == discord_thread_id
                    )
                )

    def get_local_community_thread_surface_by_starter_message_id(
            self, discord_starter_message_id: int
        ) -> LocalCommunityThreadSurface | None:
            """Load the thread surface row for one Discord starter message id."""
            with self.session() as session:
                return session.scalar(
                    select(LocalCommunityThreadSurface).where(
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
                        select(LocalCommunityThreadSurface)
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
                    select(LocalCommunityThreadSurface).where(
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
            with self.session() as session:
                surface = LocalCommunityMessageSurface(
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
                    select(LocalCommunityMessageSurface).where(
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
                    select(LocalCommunityMessageSurface).where(
                        LocalCommunityMessageSurface.discord_message_id == discord_message_id
                    )
                )

    def list_local_community_message_surfaces(
            self, local_community_message_id: int
        ) -> list[LocalCommunityMessageSurface]:
            """List every Discord message surface for one canonical comment."""
            with self.session() as session:
                return list(
                    session.scalars(
                        select(LocalCommunityMessageSurface)
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
                    select(LocalCommunityMessageSurface).where(
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
                return session.get(LocalCommunityThreadSurface, local_community_thread_surface_id)

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
                return session.get(LocalCommunityMessage, surface.local_community_message_id)
