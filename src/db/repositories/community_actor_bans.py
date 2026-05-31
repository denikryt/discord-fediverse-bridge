"""Community-scoped remote actor ban persistence for local communities."""

from __future__ import annotations

from sqlalchemy import func, select

from ...models import CommunityActorBan, utcnow
from .base import BaseRepository


class CommunityActorBanRepository(BaseRepository):
    """Persist and query local-only remote actor bans for one local community."""

    def create_active_ban(
        self,
        *,
        local_community_id: int,
        actor_handle: str,
        actor_url: str | None,
        created_by_discord_user_id: str,
        reason: str | None,
    ) -> CommunityActorBan:
        """Create or reactivate one active ban row scoped to one community.

        Duplicate active-ban detection is performed by callers before this
        method so command messages can include the existing reason. If the same
        actor was previously unbanned, this method reuses the inactive row. That
        preserves the current `(community, handle, status)` uniqueness rule and
        prevents repeated ban/unban cycles from colliding on multiple inactive
        rows.
        """
        with self.session() as session:
            inactive = session.scalar(
                select(CommunityActorBan).where(
                    CommunityActorBan.local_community_id == local_community_id,
                    CommunityActorBan.actor_handle == actor_handle,
                    CommunityActorBan.status == "inactive",
                )
            )
            if inactive is not None:
                inactive.status = "active"
                inactive.reason = reason
                inactive.created_by_discord_user_id = created_by_discord_user_id
                if actor_url and not inactive.actor_url:
                    inactive.actor_url = actor_url
                inactive.updated_at = utcnow()
                session.flush()
                return inactive

            ban = CommunityActorBan(
                local_community_id=local_community_id,
                actor_handle=actor_handle,
                actor_url=actor_url,
                status="active",
                created_by_discord_user_id=created_by_discord_user_id,
                reason=reason,
            )
            session.add(ban)
            session.flush()
            return ban

    def get_active_ban_by_handle(
        self,
        *,
        local_community_id: int,
        actor_handle: str,
    ) -> CommunityActorBan | None:
        """Load the active ban for one normalized handle in one community."""
        with self.session() as session:
            return session.scalar(
                select(CommunityActorBan).where(
                    CommunityActorBan.local_community_id == local_community_id,
                    CommunityActorBan.actor_handle == actor_handle,
                    CommunityActorBan.status == "active",
                )
            )

    def get_inactive_ban_by_handle(
        self,
        *,
        local_community_id: int,
        actor_handle: str,
    ) -> CommunityActorBan | None:
        """Load the inactive historical ban row for one community and handle."""
        with self.session() as session:
            return session.scalar(
                select(CommunityActorBan).where(
                    CommunityActorBan.local_community_id == local_community_id,
                    CommunityActorBan.actor_handle == actor_handle,
                    CommunityActorBan.status == "inactive",
                )
            )

    def get_active_ban_by_actor_url(
        self,
        *,
        local_community_id: int,
        actor_url: str,
    ) -> CommunityActorBan | None:
        """Load the active ban matching one concrete ActivityPub actor URL."""
        with self.session() as session:
            return session.scalar(
                select(CommunityActorBan).where(
                    CommunityActorBan.local_community_id == local_community_id,
                    CommunityActorBan.actor_url == actor_url,
                    CommunityActorBan.status == "active",
                )
            )

    def deactivate_active_ban_by_handle(
        self,
        *,
        local_community_id: int,
        actor_handle: str,
    ) -> CommunityActorBan | None:
        """Mark an active ban inactive without deleting moderation history."""
        with self.session() as session:
            ban = session.scalar(
                select(CommunityActorBan).where(
                    CommunityActorBan.local_community_id == local_community_id,
                    CommunityActorBan.actor_handle == actor_handle,
                    CommunityActorBan.status == "active",
                )
            )
            if ban is None:
                return None
            ban.status = "inactive"
            ban.updated_at = utcnow()
            session.flush()
            return ban

    def list_active_bans_for_community(
        self,
        *,
        local_community_id: int,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[CommunityActorBan]:
        """Return active bans for one community ordered newest first."""
        statement = (
            select(CommunityActorBan)
            .where(
                CommunityActorBan.local_community_id == local_community_id,
                CommunityActorBan.status == "active",
            )
            .order_by(CommunityActorBan.created_at.desc(), CommunityActorBan.id.desc())
            .offset(offset)
        )
        if limit is not None:
            statement = statement.limit(limit)
        with self.session() as session:
            return list(session.scalars(statement))

    def count_active_bans_for_community(self, *, local_community_id: int) -> int:
        """Count active bans for one community without loading each row."""
        with self.session() as session:
            return int(
                session.scalar(
                    select(func.count(CommunityActorBan.id)).where(
                        CommunityActorBan.local_community_id == local_community_id,
                        CommunityActorBan.status == "active",
                    )
                )
                or 0
            )

    def find_active_ban_for_actor(
        self,
        *,
        local_community_id: int,
        actor_url: str,
        actor_handle: str | None,
    ) -> CommunityActorBan | None:
        """Find a scoped ban by exact actor URL first, then by normalized handle."""
        url_match = self.get_active_ban_by_actor_url(
            local_community_id=local_community_id,
            actor_url=actor_url,
        )
        if url_match is not None or actor_handle is None:
            return url_match
        return self.get_active_ban_by_handle(
            local_community_id=local_community_id,
            actor_handle=actor_handle,
        )

    def fill_actor_url_if_missing(self, *, ban_id: int, actor_url: str) -> None:
        """Cache an observed actor URL on a handle-created ban row.

        The cache is opportunistic only. It avoids network resolution and makes
        future inbound deliveries match exactly by actor URL after the first
        handle-derived hit.
        """
        with self.session() as session:
            ban = session.get(CommunityActorBan, ban_id)
            if ban is None or ban.actor_url:
                return
            ban.actor_url = actor_url
            ban.updated_at = utcnow()
            session.flush()
