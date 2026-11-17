"""Owner-scoped remote Person Follow and guild text-channel assignment reads.

The repository owns persistent row identity. The application service in later
checkpoints performs signed Follow and guild permission authorization before
submitting changes to these transactional structures.
"""

from __future__ import annotations

from sqlalchemy import select

from ...models import RemotePersonFollow
from .base import BaseRepository


class RemotePersonFollowRepository(BaseRepository):
    """Find immutable Follow generations without confusing local Person owners."""

    def get_follow_by_activity_id(self, follow_activity_id: str) -> RemotePersonFollow | None:
        """Load an exact outgoing Follow generation for authenticated responses."""
        with self.session() as session:
            return session.scalar(select(RemotePersonFollow).where(
                RemotePersonFollow.follow_activity_id == follow_activity_id
            ))
