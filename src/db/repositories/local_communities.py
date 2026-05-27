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


"""Local community identity persistence."""


class LocalCommunityRepository(BaseRepository):
    """Persist the local communities domain."""

    def create_local_community(
            self,
            *,
            discord_guild_id: int,
            discord_forum_channel_id: int,
            slug: str,
            display_name: str,
            summary: str,
            actor_url: str,
            inbox_url: str,
            outbox_url: str,
            followers_url: str,
            public_key_pem: str,
            private_key_pem: str,
            status: str = "active",
        ) -> LocalCommunity:
            """Create one Discord-backed local community row.

            The local-community creation flow persists the actor identity in Python
            so the gateway can read it later without owning any creation policy.
            """
            with self.session() as session:
                community = LocalCommunity(
                    discord_guild_id=discord_guild_id,
                    discord_forum_channel_id=discord_forum_channel_id,
                    slug=slug,
                    display_name=display_name,
                    summary=summary,
                    actor_url=actor_url,
                    inbox_url=inbox_url,
                    outbox_url=outbox_url,
                    followers_url=followers_url,
                    public_key_pem=public_key_pem,
                    private_key_pem=private_key_pem,
                    status=status,
                )
                session.add(community)
                session.flush()
                return community

    def get_local_community_by_forum_channel_id(
            self, discord_forum_channel_id: int
        ) -> LocalCommunity | None:
            """Load the local community bound to one Discord forum channel."""
            with self.session() as session:
                return session.scalar(
                    select(LocalCommunity).where(
                        LocalCommunity.discord_forum_channel_id == discord_forum_channel_id
                    )
                )

    def get_local_community_by_actor_url(self, actor_url: str) -> LocalCommunity | None:
            """Load the local community that owns one actor URL."""
            with self.session() as session:
                return session.scalar(
                    select(LocalCommunity).where(LocalCommunity.actor_url == actor_url)
                )

    def get_local_community_by_slug(self, slug: str) -> LocalCommunity | None:
            """Load the local community for one stable slug."""
            with self.session() as session:
                return session.scalar(
                    select(LocalCommunity).where(LocalCommunity.slug == slug)
                )

    def get_local_community_by_id(self, local_community_id: int) -> LocalCommunity | None:
            """Load one local community by its primary key."""
            with self.session() as session:
                return session.get(LocalCommunity, local_community_id)

    def list_local_communities(self) -> list[LocalCommunity]:
            """Return all local communities in stable creation order."""
            with self.session() as session:
                return list(
                    session.scalars(select(LocalCommunity).order_by(LocalCommunity.created_at, LocalCommunity.id))
                )
