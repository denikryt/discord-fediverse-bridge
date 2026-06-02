"""Local community identity persistence."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...local_community_lifecycle import VALID_LOCAL_COMMUNITY_STATUSES
from ...management_audit import community_metadata_diff, community_status_diff
from ...models import LocalCommunity
from .base import BaseRepository


@dataclass(slots=True)
class LocalCommunitySettingsUpdate:
    """Describe one community-settings mutation and the deltas it produced.

    Repositories compute these deltas while the original row values are still
    available, but audit action naming remains outside this persistence layer.
    Empty dictionaries mean the submitted values did not change that slice.
    """

    community: LocalCommunity
    metadata_before: dict[str, object]
    metadata_after: dict[str, object]
    status_before: dict[str, object]
    status_after: dict[str, object]


class LocalCommunityRepository(BaseRepository):
    """Persist the local communities domain without owning command policy."""

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
        """Create one Discord-backed local community in a self-owned transaction."""
        with self.session() as session:
            return self.add_local_community(
                session,
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

    def add_local_community(
        self,
        session: Session,
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
        """Add one local-community row to a caller-owned transaction.

        ManagementActions uses this helper so the identity row and audit row are
        flushed through the same SQLAlchemy session. Other callers can keep using
        the public self-transactional wrapper above.
        """
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

    def get_local_community_by_forum_channel_id(self, discord_forum_channel_id: int) -> LocalCommunity | None:
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
            return session.scalar(select(LocalCommunity).where(LocalCommunity.actor_url == actor_url))

    def get_local_community_by_slug(self, slug: str) -> LocalCommunity | None:
        """Load the local community for one stable slug."""
        with self.session() as session:
            return session.scalar(select(LocalCommunity).where(LocalCommunity.slug == slug))

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

    def list_active_local_communities_by_guild(self, *, discord_guild_id: int) -> list[LocalCommunity]:
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

    def list_manageable_local_communities_by_guild(self, *, discord_guild_id: int) -> list[LocalCommunity]:
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
        """Update editable metadata while preserving lifecycle status."""
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
        """Update editable metadata and lifecycle status in one transaction."""
        with self.session() as session:
            update = self.set_local_community_settings(
                session,
                local_community_id=local_community_id,
                display_name=display_name,
                summary=summary,
                status=status,
            )
            return None if update is None else update.community

    def set_local_community_settings(
        self,
        session: Session,
        *,
        local_community_id: int,
        display_name: str,
        summary: str | None,
        status: str,
    ) -> LocalCommunitySettingsUpdate | None:
        """Set metadata/status and return changed-field deltas.

        The repository is responsible for comparing the persisted row with the
        requested values because it has the pre-mutation state loaded. It does
        not decide which audit actions those deltas become.
        """
        if status not in VALID_LOCAL_COMMUNITY_STATUSES:
            raise ValueError("Local community status must be active or disabled")
        community = session.get(LocalCommunity, local_community_id)
        if community is None:
            return None
        metadata_before, metadata_after = community_metadata_diff(
            old_display_name=community.display_name,
            old_summary=community.summary,
            new_display_name=display_name,
            new_summary=summary,
        )
        status_before, status_after = community_status_diff(
            old_status=community.status,
            new_status=status,
        )
        community.display_name = display_name
        community.summary = summary
        community.status = status
        session.flush()
        return LocalCommunitySettingsUpdate(
            community=community,
            metadata_before=metadata_before,
            metadata_after=metadata_after,
            status_before=status_before,
            status_after=status_after,
        )
