"""Persistence for bridge-local community and global user bans."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ...models import CommunityActorBan, utcnow
from .base import BaseRepository


@dataclass(slots=True)
class BanActivationResult:
    """Describe a ban create/reactivate mutation and its audit-ready deltas."""

    ban: CommunityActorBan
    kind: Literal["created", "reactivated"]
    before: dict[str, object] | None
    after: dict[str, object]


class CommunityActorBanRepository(BaseRepository):
    """Persist and query bridge-local bans with explicit scope invariants."""

    @staticmethod
    def _scope_values(local_community_id: int | None) -> tuple[str, str]:
        """Return stable scope columns used by SQLite uniqueness constraints."""
        return ("global", "global") if local_community_id is None else ("community", str(local_community_id))

    def create_active_ban(
        self, *, local_community_id: int, actor_handle: str, actor_url: str | None,
        created_by_discord_user_id: str, reason: str | None,
    ) -> CommunityActorBan:
        """Preserve the legacy community-scoped helper for existing callers/tests."""
        with self.session() as session:
            return self.create_or_reactivate_active_ban(
                session, local_community_id=local_community_id, actor_handle=actor_handle,
                actor_url=actor_url, target_discord_user_id=None,
                created_by_discord_user_id=created_by_discord_user_id, reason=reason,
            ).ban

    def create_or_reactivate_active_ban(
        self, session: Session, *, local_community_id: int | None, actor_handle: str,
        actor_url: str | None, target_discord_user_id: str | None = None,
        created_by_discord_user_id: str, reason: str | None,
    ) -> BanActivationResult:
        """Create or reactivate one scope-specific row in a caller transaction."""
        scope, scope_key = self._scope_values(local_community_id)
        inactive = session.scalar(select(CommunityActorBan).where(
            CommunityActorBan.scope == scope, CommunityActorBan.scope_key == scope_key,
            CommunityActorBan.actor_handle == actor_handle, CommunityActorBan.status == "inactive",
        ))
        if inactive is not None:
            before = {
                "status": inactive.status, "reason": inactive.reason,
                "created_by_discord_user_id": inactive.created_by_discord_user_id,
                "actor_url": inactive.actor_url,
                "target_discord_user_id": inactive.target_discord_user_id,
            }
            inactive.status = "active"
            inactive.reason = reason
            inactive.created_by_discord_user_id = created_by_discord_user_id
            inactive.actor_url = actor_url or inactive.actor_url
            inactive.target_discord_user_id = target_discord_user_id or inactive.target_discord_user_id
            inactive.updated_at = utcnow()
            session.flush()
            return BanActivationResult(inactive, "reactivated", before, self._snapshot(inactive))
        ban = CommunityActorBan(
            scope=scope, scope_key=scope_key, local_community_id=local_community_id,
            actor_handle=actor_handle, actor_url=actor_url,
            target_discord_user_id=target_discord_user_id, status="active",
            created_by_discord_user_id=created_by_discord_user_id, reason=reason,
        )
        session.add(ban)
        session.flush()
        return BanActivationResult(ban, "created", None, self._snapshot(ban))

    @staticmethod
    def _snapshot(ban: CommunityActorBan) -> dict[str, object]:
        """Return changed moderation fields for canonical audit JSON."""
        return {
            "scope": ban.scope, "local_community_id": ban.local_community_id,
            "actor_handle": ban.actor_handle, "actor_url": ban.actor_url,
            "target_discord_user_id": ban.target_discord_user_id,
            "created_by_discord_user_id": ban.created_by_discord_user_id,
            "reason": ban.reason, "status": ban.status,
        }

    def get_active_ban_by_handle(self, *, local_community_id: int | None, actor_handle: str) -> CommunityActorBan | None:
        """Load one active scope-specific ban by canonical handle."""
        scope, scope_key = self._scope_values(local_community_id)
        with self.session() as session:
            return session.scalar(select(CommunityActorBan).where(
                CommunityActorBan.scope == scope, CommunityActorBan.scope_key == scope_key,
                CommunityActorBan.actor_handle == actor_handle, CommunityActorBan.status == "active",
            ))

    def get_inactive_ban_by_handle(self, *, local_community_id: int | None, actor_handle: str) -> CommunityActorBan | None:
        """Load one inactive scope-specific ban by canonical handle."""
        scope, scope_key = self._scope_values(local_community_id)
        with self.session() as session:
            return session.scalar(select(CommunityActorBan).where(
                CommunityActorBan.scope == scope, CommunityActorBan.scope_key == scope_key,
                CommunityActorBan.actor_handle == actor_handle, CommunityActorBan.status == "inactive",
            ))

    def get_active_global_ban_by_discord_user_id(self, *, discord_user_id: str) -> CommunityActorBan | None:
        """Load active global moderation attached to one Discord account."""
        with self.session() as session:
            return session.scalar(select(CommunityActorBan).where(
                CommunityActorBan.scope == "global",
                CommunityActorBan.target_discord_user_id == discord_user_id,
                CommunityActorBan.status == "active",
            ))

    def get_active_community_ban_by_discord_user_id(self, *, local_community_id: int, discord_user_id: str) -> CommunityActorBan | None:
        """Load active moderation for one Discord account in one community."""
        with self.session() as session:
            return session.scalar(select(CommunityActorBan).where(
                CommunityActorBan.scope == "community",
                CommunityActorBan.local_community_id == local_community_id,
                CommunityActorBan.target_discord_user_id == discord_user_id,
                CommunityActorBan.status == "active",
            ))

    def deactivate_active_ban_by_handle(self, *, local_community_id: int | None, actor_handle: str) -> CommunityActorBan | None:
        """Deactivate one scope-specific active row in a self-owned transaction."""
        with self.session() as session:
            return self.deactivate_active_ban_by_handle_in_session(session, local_community_id=local_community_id, actor_handle=actor_handle)

    def deactivate_active_ban_by_handle_in_session(self, session: Session, *, local_community_id: int | None, actor_handle: str) -> CommunityActorBan | None:
        """Deactivate one scope-specific active row in a caller transaction."""
        scope, scope_key = self._scope_values(local_community_id)
        ban = session.scalar(select(CommunityActorBan).where(
            CommunityActorBan.scope == scope, CommunityActorBan.scope_key == scope_key,
            CommunityActorBan.actor_handle == actor_handle, CommunityActorBan.status == "active",
        ))
        if ban is None:
            return None
        ban.status = "inactive"
        ban.updated_at = utcnow()
        session.flush()
        return ban

    def list_active_bans_for_community(self, *, local_community_id: int, limit: int | None = None, offset: int = 0) -> list[CommunityActorBan]:
        """Return active community bans newest first."""
        return self._list(scope="community", local_community_id=local_community_id, limit=limit, offset=offset)

    def list_active_global_bans(self, *, limit: int | None = None, offset: int = 0) -> list[CommunityActorBan]:
        """Return active global bans newest first."""
        return self._list(scope="global", local_community_id=None, limit=limit, offset=offset)

    def _list(self, *, scope: str, local_community_id: int | None, limit: int | None, offset: int) -> list[CommunityActorBan]:
        """Execute one ordered active-ban list query."""
        stmt = select(CommunityActorBan).where(
            CommunityActorBan.scope == scope, CommunityActorBan.local_community_id == local_community_id,
            CommunityActorBan.status == "active",
        ).order_by(CommunityActorBan.created_at.desc(), CommunityActorBan.id.desc()).offset(offset)
        if limit is not None:
            stmt = stmt.limit(limit)
        with self.session() as session:
            return list(session.scalars(stmt))

    def count_active_bans_for_community(self, *, local_community_id: int) -> int:
        """Count active community bans without loading rows."""
        return self._count(scope="community", local_community_id=local_community_id)

    def count_active_global_bans(self) -> int:
        """Count active global bans without loading rows."""
        return self._count(scope="global", local_community_id=None)

    def _count(self, *, scope: str, local_community_id: int | None) -> int:
        """Execute one active-ban count query."""
        with self.session() as session:
            return int(session.scalar(select(func.count(CommunityActorBan.id)).where(
                CommunityActorBan.scope == scope, CommunityActorBan.local_community_id == local_community_id,
                CommunityActorBan.status == "active",
            )) or 0)

    def find_active_ban_for_actor(self, *, local_community_id: int | None, actor_url: str, actor_handle: str | None) -> CommunityActorBan | None:
        """Find URL-first then handle-fallback match in one explicit scope."""
        scope, scope_key = self._scope_values(local_community_id)
        with self.session() as session:
            url_match = session.scalar(select(CommunityActorBan).where(
                CommunityActorBan.scope == scope, CommunityActorBan.scope_key == scope_key,
                CommunityActorBan.actor_url == actor_url, CommunityActorBan.status == "active",
            ))
            if url_match is not None or actor_handle is None:
                return url_match
            return session.scalar(select(CommunityActorBan).where(
                CommunityActorBan.scope == scope, CommunityActorBan.scope_key == scope_key,
                CommunityActorBan.actor_handle == actor_handle, CommunityActorBan.status == "active",
            ))

    def fill_actor_url_if_missing(self, *, ban_id: int, actor_url: str) -> None:
        """Cache an observed actor URL on a handle-created row."""
        with self.session() as session:
            ban = session.get(CommunityActorBan, ban_id)
            if ban is None or ban.actor_url:
                return
            ban.actor_url = actor_url
            ban.updated_at = utcnow()
            session.flush()
