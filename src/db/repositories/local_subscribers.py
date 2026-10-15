"""Same-instance local subscriber persistence and connector-neutral claims."""

from __future__ import annotations


from sqlalchemy import select

from ...models import (
    LocalSubscriber,
)
from .base import BaseRepository
from .platform_surfaces import PlatformSurfaceRepository, discord_claim_query
from ...models import ChannelSurface


class LocalSubscriberRepository(BaseRepository):
    """Persist the local subscribers domain."""

    def __init__(self, session_provider, writer_provider):
        """Keep read sessions separate from writer-reserved mutation transactions."""
        super().__init__(session_provider)
        self._writer_provider = writer_provider

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
        # MULTIPLATFORM_STAGE01_TEMP: Discord channel-ID wrapper is scoped to the primary Discord connector.
        with self._writer_provider() as session:
            surface = PlatformSurfaceRepository.discord_channel(
                session, discord_channel_id, discord_guild_id, "forum"
            )
            return self.add_local_subscriber(
                session,
                surface.id,
                local_community_id=local_community_id,
                initiated_by_discord_user_id=initiated_by_discord_user_id,
                status=status,
            )

    def add_local_subscriber(self, session, surface_id, **fields):
        """Insert neutral subscriber state and claim within the caller's transaction."""
        from ...models import PlatformConnector

        surface = session.get(ChannelSurface, surface_id)
        if surface is None:
            raise ValueError("Missing physical channel surface")
        connector = session.get(PlatformConnector, surface.connector_id)
        discord = (
            connector.connector_key == "discord-primary"
            and connector.platform == "discord"
        )
        # Derive compatibility projections from physical identity, never from an
        # untrusted caller; other connectors carry no fabricated Discord IDs.
        row = LocalSubscriber(
            discord_channel_id=int(surface.external_channel_id) if discord else None,
            discord_guild_id=(
                int(surface.external_guild_id)
                if discord and surface.external_guild_id is not None
                else None
            ),
            **fields,
        )
        session.add(row)
        session.flush()
        PlatformSurfaceRepository.claim_local_subscriber(session, surface.id, row.id)
        return row

    def get_local_subscriber(
        self,
        *,
        local_community_id: int,
        discord_channel_id: int,
    ) -> LocalSubscriber | None:
        """Load one local-subscriber row by community and channel id."""
        # MULTIPLATFORM_STAGE01_TEMP: Discord channel-ID wrapper is scoped to the primary Discord connector.
        with self.session() as session:
            return session.scalar(
                discord_claim_query(LocalSubscriber, "local_subscriber_id").where(
                    LocalSubscriber.local_community_id == local_community_id,
                    LocalSubscriber.discord_channel_id == discord_channel_id,
                )
            )

    def get_local_subscriber_by_channel(
        self, discord_channel_id: int
    ) -> LocalSubscriber | None:
        """Load one local-subscriber row by its Discord forum channel."""
        # MULTIPLATFORM_STAGE01_TEMP: Discord channel-ID wrapper is scoped to the primary Discord connector.
        with self.session() as session:
            return session.scalar(
                discord_claim_query(LocalSubscriber, "local_subscriber_id").where(
                    LocalSubscriber.discord_channel_id == discord_channel_id
                )
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

    def list_local_subscribers_by_guild(
        self, discord_guild_id: int
    ) -> list[LocalSubscriber]:
        """Load local subscribers scoped to one Discord guild."""
        # MULTIPLATFORM_STAGE01_TEMP: Discord channel-ID wrapper is scoped to the primary Discord connector.
        return self.list_discord_subscribers(guild_id=discord_guild_id)

    def delete_local_subscriber(self, discord_channel_id: int) -> bool:
        """Delete one local-subscriber row by Discord forum channel id."""
        # MULTIPLATFORM_STAGE01_TEMP: Discord channel-ID wrapper is scoped to the primary Discord connector.
        with self._writer_provider() as session:
            row = session.scalar(
                discord_claim_query(LocalSubscriber, "local_subscriber_id").where(
                    LocalSubscriber.discord_channel_id == discord_channel_id
                )
            )
            if row is None:
                return False
            surface = session.scalar(
                select(ChannelSurface).where(
                    ChannelSurface.local_subscriber_id == row.id
                )
            )
            if surface is None:
                raise ValueError("Missing current typed channel claim")
            PlatformSurfaceRepository.release_claim(
                session,
                surface.id,
                owner_column="local_subscriber_id",
                expected_owner_id=row.id,
            )
            session.delete(row)
            session.flush()
            return True

    def list_all_local_subscribers(
        self, *, status: str | None = None
    ) -> list[LocalSubscriber]:
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

    def list_discord_subscribers(
        self, *, status=None, guild_id=None, community_id=None
    ):
        """Read primary Discord claims without changing global membership semantics."""
        with self.session() as session:
            query = discord_claim_query(LocalSubscriber, "local_subscriber_id")
            if status is not None:
                query = query.where(LocalSubscriber.status == status)
            if guild_id is not None:
                query = query.where(LocalSubscriber.discord_guild_id == guild_id)
            if community_id is not None:
                query = query.where(LocalSubscriber.local_community_id == community_id)
            return list(
                session.scalars(
                    query.order_by(LocalSubscriber.created_at, LocalSubscriber.id)
                )
            )

    def list_discord_active_subscribers(self, community_id):
        """Select eligible local fan-out targets only from active Discord claims."""
        return self.list_discord_subscribers(status="active", community_id=community_id)

    def count_discord_local_subscribers(self, community_id):
        """Count current Discord participants for the existing dashboard projection."""
        return len(self.list_discord_subscribers(community_id=community_id))
