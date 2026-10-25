from __future__ import annotations


from dataclasses import dataclass

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ...models import (
    RemoteSubscriber, LocalCommunityAuthoredAnnounceDelivery,
    utcnow,
)
from .base import BaseRepository


"""Remote subscriber persistence for bridge-owned local communities."""


class FollowActivityIdCollision(ValueError):
    """Signal that a Follow activity ID is owned by another subscriber row."""


@dataclass(frozen=True, slots=True)
class RemoteSubscriberTransition:
    """Describe the outcome of one atomic remote-subscriber mutation."""

    remote_subscriber_id: int
    transition: str


class RemoteSubscriberRepository(BaseRepository):
    """Persist the remote subscribers domain."""


    def create_or_refresh_accepted(
        self,
        session: Session,
        *,
        local_community_id: int,
        remote_actor_id: str,
        remote_inbox_url: str,
        follow_activity_id: str,
    ) -> RemoteSubscriberTransition:
        """Atomically create or refresh one accepted remote-subscriber row.

        The `(local_community_id, remote_actor_id)` relationship and globally
        unique `follow_activity_id` ownership are checked in the same caller-owned
        immediate transaction so concurrent Follow deliveries do not escape as
        raw database integrity errors.
        """
        existing_follow_owner = session.scalar(
            select(RemoteSubscriber).where(
                RemoteSubscriber.follow_activity_id == follow_activity_id
            )
        )
        if (
            existing_follow_owner is not None
            and (
                existing_follow_owner.local_community_id != local_community_id
                or existing_follow_owner.remote_actor_id != remote_actor_id
            )
        ):
            raise FollowActivityIdCollision(follow_activity_id)

        remote_subscriber = session.scalar(
            select(RemoteSubscriber).where(
                RemoteSubscriber.local_community_id == local_community_id,
                RemoteSubscriber.remote_actor_id == remote_actor_id,
            )
        )
        if remote_subscriber is None:
            remote_subscriber = RemoteSubscriber(
                local_community_id=local_community_id,
                remote_actor_id=remote_actor_id,
                remote_inbox_url=remote_inbox_url,
                follow_activity_id=follow_activity_id,
                status="accepted",
            )
            session.add(remote_subscriber)
            session.flush()
            transition = "created"
        else:
            # Follow(F2) or an endpoint refresh terminates *only* proven-unsent
            # F1 Group Announce rows in the same writer transaction. Historical
            # delivered and ambiguous evidence must not be erased or retargeted.
            if (remote_subscriber.follow_activity_id != follow_activity_id
                or remote_subscriber.remote_inbox_url != remote_inbox_url):
                self._cancel_unsent_authored(
                    session, local_community_id=local_community_id,
                    remote_actor_id=remote_actor_id,
                    follow_activity_id=remote_subscriber.follow_activity_id,
                )
            # A repeated Follow refreshes the request fields used by the
            # recovery Accept and later fanout without creating another target.
            remote_subscriber.remote_inbox_url = remote_inbox_url
            remote_subscriber.follow_activity_id = follow_activity_id
            remote_subscriber.status = "accepted"
            remote_subscriber.updated_at = utcnow()
            session.flush()
            transition = "refreshed"

        return RemoteSubscriberTransition(remote_subscriber.id, transition)

    @staticmethod
    def _cancel_unsent_authored(
        session: Session, *, local_community_id: int,
        remote_actor_id: str, follow_activity_id: str,
    ) -> None:
        """Fence unsent Group Announce snapshots before the Follow ID changes."""
        session.execute(update(LocalCommunityAuthoredAnnounceDelivery).where(
            LocalCommunityAuthoredAnnounceDelivery.local_community_id == local_community_id,
            LocalCommunityAuthoredAnnounceDelivery.target_remote_actor_id == remote_actor_id,
            LocalCommunityAuthoredAnnounceDelivery.target_follow_activity_id == follow_activity_id,
            LocalCommunityAuthoredAnnounceDelivery.status.in_(('pending','failed_proven_unsent')),
        ).values(status='cancelled'))

    def delete_if_current_follow(
        self, session: Session, *, local_community_id: int,
        remote_actor_id: str, follow_activity_id: str | None,
    ) -> str:
        """Atomically compare Follow incarnation, cancel unsent work and Undo.

        A delayed Undo(F1) cannot remove newly accepted F2, including when
        concurrent with a refresh on a separate SQLite connection.
        """
        row = session.scalar(select(RemoteSubscriber).where(
            RemoteSubscriber.local_community_id == local_community_id,
            RemoteSubscriber.remote_actor_id == remote_actor_id,
        ))
        if row is None:
            return 'missing'
        if follow_activity_id is not None and row.follow_activity_id != follow_activity_id:
            return 'stale'
        self._cancel_unsent_authored(
            session, local_community_id=local_community_id,
            remote_actor_id=remote_actor_id,
            follow_activity_id=row.follow_activity_id,
        )
        session.delete(row)
        session.flush()
        return 'removed'

    def create_remote_subscriber(
        self,
        *,
        local_community_id: int,
        remote_actor_id: str,
        remote_inbox_url: str,
        follow_activity_id: str,
        status: str = "accepted",
    ) -> RemoteSubscriber:
        """Persist one remote subscriber for a local community."""
        with self.session() as session:
            remote_subscriber = RemoteSubscriber(
                local_community_id=local_community_id,
                remote_actor_id=remote_actor_id,
                remote_inbox_url=remote_inbox_url,
                follow_activity_id=follow_activity_id,
                status=status,
            )
            session.add(remote_subscriber)
            session.flush()
            return remote_subscriber

    def get_remote_subscriber(
        self,
        *,
        local_community_id: int,
        remote_actor_id: str,
    ) -> RemoteSubscriber | None:
        """Load the remote-subscriber row for one actor and local community."""
        with self.session() as session:
            return session.scalar(
                select(RemoteSubscriber).where(
                    RemoteSubscriber.local_community_id == local_community_id,
                    RemoteSubscriber.remote_actor_id == remote_actor_id,
                )
            )

    def get_remote_subscriber_by_follow_activity_id(
        self, follow_activity_id: str
    ) -> RemoteSubscriber | None:
        """Load one remote-subscriber row by the original Follow ID."""
        with self.session() as session:
            return session.scalar(
                select(RemoteSubscriber).where(
                    RemoteSubscriber.follow_activity_id == follow_activity_id
                )
            )

    def update_remote_subscriber_acceptance(
        self,
        *,
        local_community_id: int,
        remote_actor_id: str,
        remote_inbox_url: str,
        follow_activity_id: str,
        status: str = "accepted",
    ) -> RemoteSubscriber | None:
        """Refresh one remote-subscriber row before re-sending Accept(Follow).

        Mastodon and other ActivityPub servers can retry a Follow after the
        bridge already persisted the remote subscriber but the original Accept was lost
        or rejected. Updating the inbox and Follow ID keeps the recovery Accept
        tied to the latest request while preserving the existing subscriber row.
        """
        with self.session() as session:
            remote_subscriber = session.scalar(
                select(RemoteSubscriber).where(
                    RemoteSubscriber.local_community_id == local_community_id,
                    RemoteSubscriber.remote_actor_id == remote_actor_id,
                )
            )
            if remote_subscriber is None:
                return None
            # The remote actor can send a fresh Follow with a different activity
            # ID or inbox; the Accept must target the current request, not stale
            # values from an earlier delivery attempt.
            remote_subscriber.remote_inbox_url = remote_inbox_url
            remote_subscriber.follow_activity_id = follow_activity_id
            remote_subscriber.status = status
            remote_subscriber.updated_at = utcnow()
            session.flush()
            return remote_subscriber

    def delete_remote_subscriber(
        self,
        *,
        local_community_id: int,
        remote_actor_id: str,
    ) -> bool:
        """Remove one accepted or pending remote-subscriber row.

        The delete is idempotent: callers get False when no row exists. Deleting
        the row keeps accepted remote-subscriber queries as the single source
        of truth for future fanout.
        """
        with self.session() as session:
            remote_subscriber = session.scalar(
                select(RemoteSubscriber).where(
                    RemoteSubscriber.local_community_id == local_community_id,
                    RemoteSubscriber.remote_actor_id == remote_actor_id,
                )
            )
            if remote_subscriber is None:
                return False
            session.delete(remote_subscriber)
            session.flush()
            return True

    def list_remote_subscribers(
        self,
        local_community_id: int,
        *,
        status: str | None = "accepted",
    ) -> list[RemoteSubscriber]:
        """Load remote subscribers for one local community by status."""
        with self.session() as session:
            statement = select(RemoteSubscriber).where(
                RemoteSubscriber.local_community_id == local_community_id
            )
            if status is not None:
                statement = statement.where(RemoteSubscriber.status == status)
            return list(
                session.scalars(
                    statement.order_by(RemoteSubscriber.created_at, RemoteSubscriber.id)
                )
            )


    def count_accepted_for_community(self, *, local_community_id: int) -> int:
        """Count accepted subscribers for one public followers collection."""
        with self.session() as session:
            return int(
                session.scalar(
                    select(func.count()).select_from(RemoteSubscriber).where(
                        RemoteSubscriber.local_community_id == local_community_id,
                        RemoteSubscriber.status == "accepted",
                    )
                )
                or 0
            )

    def list_accepted_actor_ids_for_community(
        self, *, local_community_id: int, offset: int, limit: int
    ) -> list[str]:
        """Return a bounded page of accepted subscriber actor IDs."""
        with self.session() as session:
            return list(
                session.scalars(
                    select(RemoteSubscriber.remote_actor_id)
                    .where(
                        RemoteSubscriber.local_community_id == local_community_id,
                        RemoteSubscriber.status == "accepted",
                    )
                    .order_by(RemoteSubscriber.created_at, RemoteSubscriber.id)
                    .offset(offset)
                    .limit(limit)
                )
            )

    def list_remote_subscribers_for_all(
        self,
        *,
        status: str | None = "accepted",
    ) -> list[RemoteSubscriber]:
        """Load remote subscribers across every local community.

        The public dashboard aggregates accepted remote-subscriber counts and
        instance hosts across all local communities, so it needs one helper
        with stable ordering and optional status filtering.
        """
        with self.session() as session:
            statement = select(RemoteSubscriber)
            if status is not None:
                statement = statement.where(RemoteSubscriber.status == status)
            return list(
                session.scalars(
                    statement.order_by(
                        RemoteSubscriber.local_community_id,
                        RemoteSubscriber.created_at,
                        RemoteSubscriber.id,
                    )
                )
            )
