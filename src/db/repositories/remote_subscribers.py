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


"""Remote subscriber persistence for bridge-owned local communities."""


class RemoteSubscriberRepository(BaseRepository):
    """Persist the remote subscribers domain."""

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
                return list(session.scalars(statement.order_by(RemoteSubscriber.created_at, RemoteSubscriber.id)))

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
