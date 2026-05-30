"""Community-scoped remote actor ban persistence for local communities."""

from __future__ import annotations

from sqlalchemy import select

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
        """Create one active ban row scoped to one local community.

        Duplicate detection is performed by callers before this method so user
        messages can include the existing reason. The database uniqueness rule
        still protects the invariant under concurrent command attempts.
        """
        with self.session() as session:
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
