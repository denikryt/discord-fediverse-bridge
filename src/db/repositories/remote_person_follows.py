"""Owner-scoped remote Person Follow and guild text-channel assignment reads.

The repository owns persistent row identity. The application service in later
checkpoints performs signed Follow and guild permission authorization before
submitting changes to these transactional structures.
"""

from __future__ import annotations

from sqlalchemy import select

from ...models import RemotePersonFollow, RemotePersonChannelAssignment
from .base import BaseRepository


class RemotePersonFollowRepository(BaseRepository):
    """Find immutable Follow generations without confusing local Person owners."""

    def get_follow_by_activity_id(self, follow_activity_id: str) -> RemotePersonFollow | None:
        """Load an exact outgoing Follow generation for authenticated responses."""
        with self.session() as session:
            return session.scalar(select(RemotePersonFollow).where(
                RemotePersonFollow.follow_activity_id == follow_activity_id
            ))

    def get_live_follow(self, *, user_id: int, remote_actor_id: str) -> RemotePersonFollow | None:
        """Resolve one owner's active generation, excluding rejected history."""
        with self.session() as session:
            return session.scalar(select(RemotePersonFollow).where(
                RemotePersonFollow.user_id == user_id,
                RemotePersonFollow.remote_actor_id == remote_actor_id,
                RemotePersonFollow.relationship_status.in_(
                    ("pending", "accepted", "cancel_requested")
                ),
            ))

    def list_owner_follows(self, user_id: int) -> list[RemotePersonFollow]:
        """Show only a registered local user's own Follow generations."""
        with self.session() as session:
            return list(session.scalars(select(RemotePersonFollow).where(
                RemotePersonFollow.user_id == user_id
            ).order_by(RemotePersonFollow.id)))

    def list_assignments(self, follow_id: int) -> list[RemotePersonChannelAssignment]:
        """Return per-guild destinations of exactly one Follow identity."""
        with self.session() as session:
            return list(session.scalars(select(RemotePersonChannelAssignment).where(
                RemotePersonChannelAssignment.follow_id == follow_id
            ).order_by(RemotePersonChannelAssignment.id)))
