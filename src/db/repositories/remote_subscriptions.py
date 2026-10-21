from __future__ import annotations

from sqlalchemy import select

from ...models import (
    ChannelCommunitySubscription,
)
from .base import BaseRepository
from .platform_surfaces import PlatformSurfaceRepository, discord_claim_query
from ...models import ChannelSurface

"""Remote community subscription lifecycle persistence."""


class RemoteSubscriptionRepository(BaseRepository):
    """Persist the remote subscriptions domain."""

    def __init__(self, session_provider, writer_provider):
        """Keep read sessions separate from writer-reserved mutation transactions."""
        super().__init__(session_provider)
        self._writer_provider = writer_provider

    def get_subscription_by_channel(
        self, discord_channel_id: int
    ) -> ChannelCommunitySubscription | None:
        """Load the single community subscription owned by one Discord channel."""
        # Each channel maps to at most one community, so this is a point lookup.
        # MULTIPLATFORM_DISCORD_PROJECTION_TEMP: Discord channel-ID wrapper is scoped to the primary Discord connector.
        with self.session() as session:
            return session.scalar(
                discord_claim_query(
                    ChannelCommunitySubscription, "remote_subscription_id"
                ).where(
                    ChannelCommunitySubscription.discord_channel_id
                    == discord_channel_id
                )
            )

    def get_subscriptions_by_community(
        self, lemmy_community_actor_id: str
    ) -> list[ChannelCommunitySubscription]:
        """Load every accepted Discord subscription for one Lemmy community."""
        # Inbound routing only uses subscriptions that completed the Follow ->
        # Accept lifecycle. Pending/failed rows are visible to moderator flows
        # but must not fan out remote content into Discord.
        # MULTIPLATFORM_DISCORD_PROJECTION_TEMP: Legacy subscription recipient query delegates to Discord-scoped surface claims.
        return self.list_discord_accepted_subscriptions_for_community(
            lemmy_community_actor_id
        )

    def get_all_subscriptions(self) -> list[ChannelCommunitySubscription]:
        """Return all subscription rows in stable creation order."""
        # Ordered by creation time so the /list-subscriptions command shows a
        # stable, predictable list.
        with self.session() as session:
            return list(
                session.scalars(
                    select(ChannelCommunitySubscription).order_by(
                        ChannelCommunitySubscription.created_at
                    )
                )
            )

    def list_subscriptions(
        self, *, status: str | None = None
    ) -> list[ChannelCommunitySubscription]:
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

    def get_subscriptions_by_guild(
        self, discord_guild_id: int
    ) -> list[ChannelCommunitySubscription]:
        """Return all subscription rows for one Discord guild in creation order.

        Used by /list-subscriptions to show only the subscriptions that belong
        to the guild where the command was invoked.
        """
        # MULTIPLATFORM_DISCORD_PROJECTION_TEMP: Legacy subscription recipient query delegates to Discord-scoped surface claims.
        return self.list_discord_subscriptions(guild_id=discord_guild_id)

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
        # MULTIPLATFORM_DISCORD_PROJECTION_TEMP: Discord channel-ID wrapper is scoped to the primary Discord connector.
        with self._writer_provider() as session:
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
            surface = PlatformSurfaceRepository.discord_channel(
                session, discord_channel_id, discord_guild_id, "forum"
            )
            PlatformSurfaceRepository.claim_remote_subscription(
                session, surface.id, sub.id
            )
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
        # Follow state lives on the subscription row because later work need a
        # single source of truth for whether the bridge is pending/accepted.
        # MULTIPLATFORM_DISCORD_PROJECTION_TEMP: Discord channel-ID wrapper is scoped to the primary Discord connector.
        with self.session() as session:
            subscription = session.scalar(
                discord_claim_query(
                    ChannelCommunitySubscription, "remote_subscription_id"
                ).where(
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
        # MULTIPLATFORM_DISCORD_PROJECTION_TEMP: Discord channel-ID wrapper is scoped to the primary Discord connector.
        with self._writer_provider() as session:
            sub = session.scalar(
                discord_claim_query(
                    ChannelCommunitySubscription, "remote_subscription_id"
                ).where(
                    ChannelCommunitySubscription.discord_channel_id
                    == discord_channel_id
                )
            )
            if sub is None:
                return False
            surface = session.scalar(
                select(ChannelSurface).where(
                    ChannelSurface.remote_subscription_id == sub.id
                )
            )
            if surface is None:
                raise ValueError("Missing current typed channel claim")
            PlatformSurfaceRepository.release_claim(
                session,
                surface.id,
                owner_column="remote_subscription_id",
                expected_owner_id=sub.id,
            )
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
                select(func.count())
                .select_from(ChannelCommunitySubscription)
                .where(
                    ChannelCommunitySubscription.lemmy_community_actor_id
                    == community_actor_id
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
        # MULTIPLATFORM_DISCORD_PROJECTION_TEMP: Legacy subscription recipient query delegates to Discord-scoped surface claims.
        return self.list_discord_pending_subscriptions_for_notification(
            community_actor_id
        )

    def list_discord_subscriptions(
        self, *, status=None, guild_id=None, community_actor_id=None
    ):
        """Read only live primary Discord claims; AP-global lists remain unfiltered."""
        with self.session() as session:
            query = discord_claim_query(
                ChannelCommunitySubscription, "remote_subscription_id"
            )
            if status is not None:
                query = query.where(ChannelCommunitySubscription.status == status)
            if guild_id is not None:
                query = query.where(
                    ChannelCommunitySubscription.discord_guild_id == guild_id
                )
            if community_actor_id is not None:
                query = query.where(
                    ChannelCommunitySubscription.lemmy_community_actor_id
                    == community_actor_id
                )
            return list(
                session.scalars(
                    query.order_by(
                        ChannelCommunitySubscription.created_at,
                        ChannelCommunitySubscription.id,
                    )
                )
            )

    def list_discord_accepted_subscriptions_for_community(self, actor_id):
        """Select accepted recipients before any Discord fan-out side effect."""
        return self.list_discord_subscriptions(
            status="accepted", community_actor_id=actor_id
        )

    def list_discord_pending_subscriptions_for_notification(self, actor_id):
        """Read Discord notification recipients separately from global Follow promotion."""
        return self.list_discord_subscriptions(
            status="pending", community_actor_id=actor_id
        )

    def add_subscription(self, session, surface_id, **fields):
        """Insert domain state and its typed claim in one caller-reserved transaction."""
        from ...models import PlatformConnector

        surface = session.get(ChannelSurface, surface_id)
        if surface is None:
            raise ValueError("Missing physical channel surface")
        connector = session.get(PlatformConnector, surface.connector_id)
        discord = (
            connector.connector_key == "discord-primary"
            and connector.platform == "discord"
        )
        row = ChannelCommunitySubscription(
            discord_channel_id=int(surface.external_channel_id) if discord else None,
            discord_guild_id=(
                int(surface.external_guild_id)
                if discord and surface.external_guild_id is not None
                else None
            ),
            **fields,
        )
        session.add(row)
        session.flush()
        PlatformSurfaceRepository.claim_remote_subscription(session, surface.id, row.id)
        return row

    def upsert_retry_state(
        self,
        session,
        *,
        expected_subscription_id,
        surface_id,
        expected_status="failed",
        expected_actor_id,
        expected_follow_activity_id,
        expected_created_at,
        expected_updated_at,
        **fields,
    ):
        """Update the same failed owner only if snapshot and live claim still agree."""
        row = session.get(ChannelCommunitySubscription, expected_subscription_id)
        surface = session.get(ChannelSurface, surface_id)
        if (
            row is None
            or surface is None
            or surface.remote_subscription_id != expected_subscription_id
            or row.status != expected_status
            or row.lemmy_community_actor_id != expected_actor_id
            or row.follow_activity_id != expected_follow_activity_id
            or row.created_at.replace(tzinfo=None)
            != expected_created_at.replace(tzinfo=None)
            or row.updated_at.replace(tzinfo=None)
            != expected_updated_at.replace(tzinfo=None)
        ):
            raise ValueError("Subscription owner changed during retry")
        # Identity, creation time and claim stay untouched; only successful
        # lifecycle results may replace the failed owner's target metadata.
        for field, value in fields.items():
            if field not in {
                "discord_guild_id",
                "lemmy_community_actor_id",
                "lemmy_community_name",
                "lemmy_community_id",
                "community_handle",
                "community_inbox_url",
                "follow_activity_id",
                "initiated_by_discord_user_id",
                "status",
            }:
                raise ValueError("Unsupported retry field")
            setattr(row, field, value)
        session.flush()
        PlatformSurfaceRepository.claim_remote_subscription(session, surface_id, row.id)
        return row
