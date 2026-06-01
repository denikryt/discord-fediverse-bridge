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
from ...local_community_lifecycle import VALID_LOCAL_COMMUNITY_STATUSES


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
            summary: str | None,
            created_by_discord_user_id: str,
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
            The creator id is copied from the command operation unchanged so
            management permissions can compare Discord user ids as strings.
            """
            with self.session() as session:
                community = LocalCommunity(
                    discord_guild_id=discord_guild_id,
                    discord_forum_channel_id=discord_forum_channel_id,
                    slug=slug,
                    display_name=display_name,
                    summary=summary,
                    created_by_discord_user_id=created_by_discord_user_id,
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

    def list_active_local_communities_by_guild(
        self,
        *,
        discord_guild_id: int,
    ) -> list[LocalCommunity]:
            """Return active local communities for one Discord guild by slug."""
            with self.session() as session:
                return list(
                    session.scalars(
                        select(LocalCommunity)
                        .where(
                            LocalCommunity.discord_guild_id == discord_guild_id,
                            LocalCommunity.status == "active",
                        )
                        .order_by(LocalCommunity.slug, LocalCommunity.id)
                    )
                )

    def list_active_local_communities_owned_by_user_in_guild(
        self,
        *,
        discord_guild_id: int,
        created_by_discord_user_id: str,
    ) -> list[LocalCommunity]:
            """Return active guild communities owned by one Discord user id."""
            with self.session() as session:
                return list(
                    session.scalars(
                        select(LocalCommunity)
                        .where(
                            LocalCommunity.discord_guild_id == discord_guild_id,
                            LocalCommunity.created_by_discord_user_id == created_by_discord_user_id,
                            LocalCommunity.status == "active",
                        )
                        .order_by(LocalCommunity.slug, LocalCommunity.id)
                    )
                )

    def list_active_local_communities(self) -> list[LocalCommunity]:
            """Return every active local community across guilds by slug."""
            with self.session() as session:
                return list(
                    session.scalars(
                        select(LocalCommunity)
                        .where(LocalCommunity.status == "active")
                        .order_by(LocalCommunity.slug, LocalCommunity.id)
                    )
                )


    def list_manageable_local_communities_by_guild(
        self,
        *,
        discord_guild_id: int,
    ) -> list[LocalCommunity]:
            """Return active and disabled local communities for one guild by slug."""
            with self.session() as session:
                return list(
                    session.scalars(
                        select(LocalCommunity)
                        .where(
                            LocalCommunity.discord_guild_id == discord_guild_id,
                            LocalCommunity.status.in_(["active", "disabled"]),
                        )
                        .order_by(LocalCommunity.slug, LocalCommunity.id)
                    )
                )

    def list_manageable_local_communities_owned_by_user_in_guild(
        self,
        *,
        discord_guild_id: int,
        created_by_discord_user_id: str,
    ) -> list[LocalCommunity]:
            """Return owned active and disabled guild communities for management UI."""
            with self.session() as session:
                return list(
                    session.scalars(
                        select(LocalCommunity)
                        .where(
                            LocalCommunity.discord_guild_id == discord_guild_id,
                            LocalCommunity.created_by_discord_user_id == created_by_discord_user_id,
                            LocalCommunity.status.in_(["active", "disabled"]),
                        )
                        .order_by(LocalCommunity.slug, LocalCommunity.id)
                    )
                )

    def list_manageable_local_communities(self) -> list[LocalCommunity]:
            """Return every active or disabled local community across guilds by slug."""
            with self.session() as session:
                return list(
                    session.scalars(
                        select(LocalCommunity)
                        .where(LocalCommunity.status.in_(["active", "disabled"]))
                        .order_by(LocalCommunity.slug, LocalCommunity.id)
                    )
                )

    def update_local_community_metadata(
        self,
        *,
        local_community_id: int,
        display_name: str,
        summary: str | None,
    ) -> LocalCommunity | None:
            """Update editable metadata for one local community row.

            This compatibility method intentionally preserves lifecycle status;
            status-aware edit flows use `update_local_community_settings`.
            """
            with self.session() as session:
                community = session.get(LocalCommunity, local_community_id)
                if community is None:
                    return None
                community.display_name = display_name
                community.summary = summary
                session.flush()
                return community

    def update_local_community_settings(
        self,
        *,
        local_community_id: int,
        display_name: str,
        summary: str | None,
        status: str,
    ) -> LocalCommunity | None:
            """Update editable metadata and lifecycle status for one community.

            Slug, actor URLs, Discord binding, and ownership remain stable
            identity fields. Callers must validate status before this repository
            method persists it.
            """
            with self.session() as session:
                if status not in VALID_LOCAL_COMMUNITY_STATUSES:
                    raise ValueError("Local community status must be active or disabled")
                community = session.get(LocalCommunity, local_community_id)
                if community is None:
                    return None
                community.display_name = display_name
                community.summary = summary
                community.status = status
                session.flush()
                return community
