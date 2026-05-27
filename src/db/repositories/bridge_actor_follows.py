from __future__ import annotations

from sqlalchemy import select

from ...models import (
    BridgeActorFollow,
    ChannelCommunitySubscription,
    utcnow,
)
from .base import BaseRepository


"""Bridge actor Follow activity persistence."""


class BridgeActorFollowRepository(BaseRepository):
    """Persist the bridge actor follows domain."""

    def list_bridge_actor_follows(self) -> list[BridgeActorFollow]:
            """Return all bridge-actor follow rows in stable creation order."""
            with self.session() as session:
                return list(
                    session.scalars(
                        select(BridgeActorFollow).order_by(
                            BridgeActorFollow.created_at,
                            BridgeActorFollow.id,
                        )
                    )
                )

    def get_bridge_actor_follow(self, community_actor_id: str) -> BridgeActorFollow | None:
            """Load the bridge-actor follow row for one remote community, if it exists."""
            with self.session() as session:
                return session.scalar(
                    select(BridgeActorFollow).where(
                        BridgeActorFollow.community_actor_id == community_actor_id
                    )
                )

    def get_bridge_actor_follow_by_follow_activity_id(
            self, follow_activity_id: str
        ) -> BridgeActorFollow | None:
            """Load the bridge-actor follow row that owns one outbound Follow activity."""
            # Accept handlers match on follow_activity_id rather than community URL
            # to stay correct even if the canonical community ID differs from the
            # URL we originally used to send the Follow.
            with self.session() as session:
                return session.scalar(
                    select(BridgeActorFollow).where(
                        BridgeActorFollow.follow_activity_id == follow_activity_id
                    )
                )

    def create_bridge_actor_follow(
            self,
            *,
            community_actor_id: str,
            follow_activity_id: str | None,
            community_inbox_url: str | None,
            status: str = "pending",
        ) -> BridgeActorFollow:
            """Create the AP-level follow row for a remote community.

            Callers must verify no existing row exists before calling; the UNIQUE
            constraint on community_actor_id will raise IntegrityError on duplicates.
            """
            with self.session() as session:
                follow = BridgeActorFollow(
                    community_actor_id=community_actor_id,
                    follow_activity_id=follow_activity_id,
                    community_inbox_url=community_inbox_url,
                    status=status,
                )
                session.add(follow)
                session.flush()
                return follow

    def mark_bridge_actor_follow_accepted(self, community_actor_id: str) -> BridgeActorFollow:
            """Mark the bridge-actor follow row accepted after the remote instance confirms.

            Also marks all pending ChannelCommunitySubscription rows for this
            community as accepted so every waiting channel activates at once.
            """
            # Accepting the follow at the AP level means every channel that was
            # waiting on this community should transition simultaneously — they share
            # the same underlying federation state.
            with self.session() as session:
                follow = session.scalar(
                    select(BridgeActorFollow).where(
                        BridgeActorFollow.community_actor_id == community_actor_id
                    )
                )
                if follow is None:
                    raise RuntimeError(
                        f"Missing BridgeActorFollow for community {community_actor_id}"
                    )
                follow.status = "accepted"
                follow.updated_at = utcnow()

                # Mark all pending channel subscriptions for this community accepted.
                # Only pending rows are updated; accepted rows keep their status.
                pending_subs = list(
                    session.scalars(
                        select(ChannelCommunitySubscription).where(
                            ChannelCommunitySubscription.lemmy_community_actor_id == community_actor_id,
                            ChannelCommunitySubscription.status == "pending",
                        )
                    )
                )
                for sub in pending_subs:
                    sub.status = "accepted"
                    sub.updated_at = utcnow()

                session.flush()
                return follow

    def delete_bridge_actor_follow(self, community_actor_id: str) -> bool:
            """Delete the bridge-actor follow row for one remote community.

            Called after the last ChannelCommunitySubscription for a community is
            removed and an Undo(Follow) has been dispatched to the remote instance.
            Returns False if no row exists.
            """
            with self.session() as session:
                follow = session.scalar(
                    select(BridgeActorFollow).where(
                        BridgeActorFollow.community_actor_id == community_actor_id
                    )
                )
                if follow is None:
                    return False
                session.delete(follow)
                return True
