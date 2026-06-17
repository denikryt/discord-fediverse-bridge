"""Persistence operations for local User Actor follower relationships."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from ...models import UserActorFollower, UserActorFollowActivityId
from .base import BaseRepository


class FollowActivityIdCollision(ValueError):
    """Signal that a globally owned Follow ID was reused by another relationship."""


@dataclass(frozen=True, slots=True)
class UserActorFollowerTransition:
    """Describe the durable relationship incarnation returned by one Follow mutation."""

    relationship_id: int
    transition: str
    notification_state: str


class UserActorFollowerRepository(BaseRepository):
    """Persist and query User Actor followers without sharing community subscriber state."""

    def create_or_refresh(
        self,
        session: Session,
        *,
        user_id: int,
        remote_actor_id: str,
        remote_inbox_url: str,
        remote_shared_inbox_url: str | None,
        follow_activity_id: str,
    ) -> UserActorFollowerTransition:
        """Register Follow-ID ownership and create or refresh one accepted relationship."""
        ledger = session.scalar(
            select(UserActorFollowActivityId).where(
                UserActorFollowActivityId.follow_activity_id == follow_activity_id
            )
        )
        if ledger is None:
            session.add(
                UserActorFollowActivityId(
                    follow_activity_id=follow_activity_id,
                    user_id=user_id,
                    remote_actor_id=remote_actor_id,
                )
            )
        elif ledger.user_id != user_id or ledger.remote_actor_id != remote_actor_id:
            raise FollowActivityIdCollision(follow_activity_id)

        row = session.scalar(
            select(UserActorFollower).where(
                UserActorFollower.user_id == user_id,
                UserActorFollower.remote_actor_id == remote_actor_id,
            )
        )
        if row is None:
            row = UserActorFollower(
                user_id=user_id,
                remote_actor_id=remote_actor_id,
                remote_inbox_url=remote_inbox_url,
                remote_shared_inbox_url=remote_shared_inbox_url,
                follow_activity_id=follow_activity_id,
                status="accepted",
                owner_notification_state="pending",
            )
            session.add(row)
            session.flush()
            transition = "created"
        else:
            row.remote_inbox_url = remote_inbox_url
            row.remote_shared_inbox_url = remote_shared_inbox_url
            row.follow_activity_id = follow_activity_id
            row.status = "accepted"
            session.flush()
            transition = "refreshed"
        return UserActorFollowerTransition(
            row.id, transition, row.owner_notification_state
        )

    def delete_relationship(
        self, session: Session, *, user_id: int, remote_actor_id: str
    ) -> bool:
        """Delete the current relationship incarnation while retaining Follow-ID history."""
        result = session.execute(
            delete(UserActorFollower).where(
                UserActorFollower.user_id == user_id,
                UserActorFollower.remote_actor_id == remote_actor_id,
            )
        )
        return bool(result.rowcount)

    def claim_notification_attempt(
        self, relationship_id: int
    ) -> UserActorFollower | None:
        """Claim one pending notification only for the exact relationship incarnation."""
        now = datetime.now(timezone.utc)
        with self.session() as session:
            result = session.execute(
                update(UserActorFollower)
                .where(
                    UserActorFollower.id == relationship_id,
                    UserActorFollower.owner_notification_state == "pending",
                )
                .values(
                    owner_notification_state="attempted",
                    owner_notification_attempted_at=now,
                    updated_at=now,
                )
            )
            if not result.rowcount:
                return None
            return session.get(UserActorFollower, relationship_id)

    def get_relationship(
        self, *, user_id: int, remote_actor_id: str
    ) -> UserActorFollower | None:
        """Load the current relationship for one local-user/remote-actor pair."""
        with self.session() as session:
            return session.scalar(
                select(UserActorFollower).where(
                    UserActorFollower.user_id == user_id,
                    UserActorFollower.remote_actor_id == remote_actor_id,
                )
            )

    @staticmethod
    def list_accepted_for_snapshot(
        session: Session, *, user_id: int
    ) -> list[UserActorFollower]:
        """Return every accepted row in deterministic relationship order."""
        return list(
            session.scalars(
                select(UserActorFollower)
                .where(
                    UserActorFollower.user_id == user_id,
                    UserActorFollower.status == "accepted",
                )
                .order_by(UserActorFollower.id)
            )
        )

    def count_accepted(self, *, user_id: int) -> int:
        """Count accepted followers for one local user."""
        with self.session() as session:
            return int(
                session.scalar(
                    select(func.count())
                    .select_from(UserActorFollower)
                    .where(
                        UserActorFollower.user_id == user_id,
                        UserActorFollower.status == "accepted",
                    )
                )
                or 0
            )

    def list_accepted_actor_ids(
        self, *, user_id: int, offset: int, limit: int
    ) -> list[str]:
        """Return one deterministic bounded page of public follower actor IDs."""
        with self.session() as session:
            statement = (
                select(UserActorFollower.remote_actor_id)
                .where(
                    UserActorFollower.user_id == user_id,
                    UserActorFollower.status == "accepted",
                )
                .order_by(UserActorFollower.id.desc())
                .offset(offset)
                .limit(limit)
            )
            return list(session.scalars(statement))
