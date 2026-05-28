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


"""Same-instance local subscriber persistence."""


class LocalSubscriberRepository(BaseRepository):
    """Persist the local subscribers domain."""

    def create_local_subscriber(
            self,
            *,
            local_community_id: int,
            discord_guild_id: int | None,
            discord_channel_id: int,
            initiated_by_discord_user_id: str | None,
            status: str = "active",
        ) -> LocalSubscriber:
            """Persist one same-instance local subscriber forum row."""
            with self.session() as session:
                row = LocalSubscriber(
                    local_community_id=local_community_id,
                    discord_guild_id=discord_guild_id,
                    discord_channel_id=discord_channel_id,
                    initiated_by_discord_user_id=initiated_by_discord_user_id,
                    status=status,
                )
                session.add(row)
                session.flush()
                return row

    def get_local_subscriber(
            self,
            *,
            local_community_id: int,
            discord_channel_id: int,
        ) -> LocalSubscriber | None:
            """Load one local-subscriber row by community and channel id."""
            with self.session() as session:
                return session.scalar(
                    select(LocalSubscriber).where(
                        LocalSubscriber.local_community_id == local_community_id,
                        LocalSubscriber.discord_channel_id == discord_channel_id,
                    )
                )

    def get_local_subscriber_by_channel(self, discord_channel_id: int) -> LocalSubscriber | None:
            """Load one local-subscriber row by its Discord forum channel."""
            with self.session() as session:
                return session.scalar(
                    select(LocalSubscriber).where(LocalSubscriber.discord_channel_id == discord_channel_id)
                )

    def list_local_subscribers(self, local_community_id: int) -> list[LocalSubscriber]:
            """Load local subscribers for one community in stable creation order."""
            with self.session() as session:
                return list(
                    session.scalars(
                        select(LocalSubscriber)
                        .where(LocalSubscriber.local_community_id == local_community_id)
                        .order_by(LocalSubscriber.created_at, LocalSubscriber.id)
                    )
                )

    def list_local_subscribers_by_guild(self, discord_guild_id: int) -> list[LocalSubscriber]:
            """Load local subscribers scoped to one Discord guild."""
            with self.session() as session:
                return list(
                    session.scalars(
                        select(LocalSubscriber)
                        .where(LocalSubscriber.discord_guild_id == discord_guild_id)
                        .order_by(LocalSubscriber.created_at, LocalSubscriber.id)
                    )
                )

    def delete_local_subscriber(self, discord_channel_id: int) -> bool:
            """Delete one local-subscriber row by Discord forum channel id."""
            with self.session() as session:
                row = session.scalar(
                    select(LocalSubscriber).where(LocalSubscriber.discord_channel_id == discord_channel_id)
                )
                if row is None:
                    return False
                session.delete(row)
                session.flush()
                return True

    def list_all_local_subscribers(self, *, status: str | None = None) -> list[LocalSubscriber]:
            """Load local-subscriber rows, optionally filtered by active status."""
            # The dashboard uses active rows only so public placement reflects
            # current fanout targets instead of inactive historical rows.
            with self.session() as session:
                query = select(LocalSubscriber)
                if status is not None:
                    query = query.where(LocalSubscriber.status == status)
                return list(
                    session.scalars(
                        query.order_by(LocalSubscriber.created_at, LocalSubscriber.id)
                    )
                )

    def count_local_subscribers(self, local_community_id: int) -> int:
            """Return how many local subscriber forum rows exist for one community."""
            with self.session() as session:
                return len(
                    list(
                        session.scalars(
                            select(LocalSubscriber.id).where(
                                LocalSubscriber.local_community_id == local_community_id
                            )
                        )
                    )
                )
