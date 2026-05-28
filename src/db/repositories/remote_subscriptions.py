from __future__ import annotations

from sqlalchemy import select

from ...models import (
    BridgeActorFollow,
    ChannelCommunitySubscription,
    utcnow,
)
from .base import BaseRepository


"""Remote community subscription lifecycle persistence."""


class RemoteSubscriptionRepository(BaseRepository):
    """Persist the remote subscriptions domain."""

    def get_subscription_by_channel(self, discord_channel_id: int) -> ChannelCommunitySubscription | None:
            """Load the single community subscription owned by one Discord channel."""
            # Each channel maps to at most one community, so this is a point lookup.
            with self.session() as session:
                return session.scalar(select(ChannelCommunitySubscription).where(ChannelCommunitySubscription.discord_channel_id == discord_channel_id))

    def get_subscriptions_by_community(self, lemmy_community_actor_id: str) -> list[ChannelCommunitySubscription]:
            """Load every accepted Discord subscription for one Lemmy community."""
            # Inbound routing only uses subscriptions that completed the Follow ->
            # Accept lifecycle. Pending/failed rows are visible to moderator flows
            # but must not fan out remote content into Discord.
            with self.session() as session:
                return list(
                    session.scalars(
                        select(ChannelCommunitySubscription).where(
                            ChannelCommunitySubscription.lemmy_community_actor_id
                            == lemmy_community_actor_id,
                            ChannelCommunitySubscription.status == "accepted",
                        )
                    )
                )

    def get_all_subscriptions(self) -> list[ChannelCommunitySubscription]:
            """Return all subscription rows in stable creation order."""
            # Ordered by creation time so the /list-subscriptions command shows a
            # stable, predictable list.
            with self.session() as session:
                return list(session.scalars(select(ChannelCommunitySubscription).order_by(ChannelCommunitySubscription.created_at)))

    def list_subscriptions(self, *, status: str | None = None) -> list[ChannelCommunitySubscription]:
            """Return subscription rows, optionally filtered by lifecycle status."""
            # Dashboard rendering uses accepted rows only because public guild
            # placement should describe active routing, not failed/pending attempts.
            with self.session() as session:
                query = select(ChannelCommunitySubscription)
                if status is not None:
                    query = query.where(ChannelCommunitySubscription.status == status)
                return list(
                    session.scalars(
                        query.order_by(
                            ChannelCommunitySubscription.created_at,
                            ChannelCommunitySubscription.id,
                        )
                    )
                )

    def get_subscriptions_by_guild(self, discord_guild_id: int) -> list[ChannelCommunitySubscription]:
            """Return all subscription rows for one Discord guild in creation order.

            Used by /list-subscriptions to show only the subscriptions that belong
            to the guild where the command was invoked.
            """
            with self.session() as session:
                return list(session.scalars(
                    select(ChannelCommunitySubscription)
                    .where(ChannelCommunitySubscription.discord_guild_id == discord_guild_id)
                    .order_by(ChannelCommunitySubscription.created_at)
                ))

    def create_subscription(
            self,
            *,
            discord_channel_id: int,
            discord_guild_id: int | None = None,
            lemmy_community_actor_id: str,
            lemmy_community_name: str | None,
            lemmy_community_id: int | None,
            community_handle: str | None = None,
            community_inbox_url: str | None = None,
            follow_activity_id: str | None = None,
            initiated_by_discord_user_id: str | None = None,
            status: str = "pending",
        ) -> ChannelCommunitySubscription:
            """Create one channel-to-community subscription row with follow state."""
            # Callers are responsible for checking uniqueness before calling this;
            # the DB UNIQUE constraint on discord_channel_id is the final safety net
            # and will raise IntegrityError if a duplicate is attempted.
            with self.session() as session:
                sub = ChannelCommunitySubscription(
                    discord_channel_id=discord_channel_id,
                    discord_guild_id=discord_guild_id,
                    lemmy_community_actor_id=lemmy_community_actor_id,
                    lemmy_community_name=lemmy_community_name,
                    lemmy_community_id=lemmy_community_id,
                    community_handle=community_handle,
                    community_inbox_url=community_inbox_url,
                    follow_activity_id=follow_activity_id,
                    initiated_by_discord_user_id=initiated_by_discord_user_id,
                    status=status,
                )
                session.add(sub)
                session.flush()
                return sub

    def update_subscription_follow_state(
            self,
            *,
            discord_channel_id: int,
            community_handle: str | None = None,
            community_inbox_url: str | None,
            follow_activity_id: str | None,
            status: str,
        ) -> None:
            """Update the federation follow state for one existing subscription."""
            # Follow state lives on the subscription row because later stages need a
            # single source of truth for whether the bridge is pending/accepted.
            with self.session() as session:
                subscription = session.scalar(
                    select(ChannelCommunitySubscription).where(
                        ChannelCommunitySubscription.discord_channel_id
                        == discord_channel_id
                    )
                )
                if subscription is None:
                    raise RuntimeError(
                        f"Missing subscription for Discord channel {discord_channel_id}"
                    )
                subscription.community_handle = community_handle
                subscription.community_inbox_url = community_inbox_url
                subscription.follow_activity_id = follow_activity_id
                subscription.status = status

    def delete_subscription(self, discord_channel_id: int) -> bool:
            """Delete one channel subscription if it exists."""
            # Returns False when no subscription exists so callers can give a
            # meaningful response without a separate existence check.
            with self.session() as session:
                sub = session.scalar(select(ChannelCommunitySubscription).where(ChannelCommunitySubscription.discord_channel_id == discord_channel_id))
                if sub is None:
                    return False
                session.delete(sub)
                return True

    def count_subscriptions_for_community(self, community_actor_id: str) -> int:
            """Return the number of channel subscriptions pointing at one community.

            Used by the unsubscribe flow to decide whether to send Undo(Follow):
            an Undo is only dispatched when this count drops to zero.
            """
            from sqlalchemy import func
            with self.session() as session:
                result = session.scalar(
                    select(func.count()).select_from(ChannelCommunitySubscription).where(
                        ChannelCommunitySubscription.lemmy_community_actor_id == community_actor_id
                    )
                )
                return result or 0

    def get_pending_channel_subscriptions_for_community(
            self, community_actor_id: str
        ) -> list[ChannelCommunitySubscription]:
            """Return all pending channel subscriptions for one community.

            Used by handle_follow_accepted to DM each initiating Discord user when
            the bridge actor follow is confirmed by the remote instance.
            """
            with self.session() as session:
                return list(
                    session.scalars(
                        select(ChannelCommunitySubscription).where(
                            ChannelCommunitySubscription.lemmy_community_actor_id == community_actor_id,
                            ChannelCommunitySubscription.status == "pending",
                        )
                    )
                )
