from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ...models import (
    BridgeActorFollow,
    ChannelCommunitySubscription,
    utcnow,
)
from .base import BaseRepository


class FollowReservationConflict(ValueError):
    """An existing durable Follow generation cannot be replaced safely."""


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

    def get_bridge_actor_follow(
        self, community_actor_id: str
    ) -> BridgeActorFollow | None:
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

    @staticmethod
    def reserve_with_binding(
        session: Session,
        *,
        community_actor_id: str,
        follow_activity_id: str,
        community_inbox_url: str,
        remote_software_profile: str,
        discord_channel_id: int,
        discord_guild_id: int | None,
        community_name: str | None,
        numeric_id: int | None,
        community_handle: str | None,
        initiated_by_discord_user_id: str,
    ) -> tuple[BridgeActorFollow, bool]:
        """Reserve one verified Follow generation and its channel in one writer transaction.

        The caller must own an IMMEDIATE SQLite transaction. The returned flag
        means a Follow is prepared and can be CAS-claimed by exactly one sender;
        it never means that network delivery already happened.
        """
        previous_channel = session.scalar(select(ChannelCommunitySubscription).where(
            ChannelCommunitySubscription.discord_channel_id == discord_channel_id
        ))
        if previous_channel is not None:
            if previous_channel.status != "failed":
                raise FollowReservationConflict("channel already has an active or pending subscription")
            session.delete(previous_channel)
            session.flush()

        follow = session.scalar(select(BridgeActorFollow).where(
            BridgeActorFollow.community_actor_id == community_actor_id
        ))
        if follow is not None and follow.status == "failed":
            # A failed *send* with a non-null ID may have reached the remote
            # inbox. Never replace that identity without verified cleanup.
            if follow.follow_activity_id is not None:
                raise FollowReservationConflict("failed Follow generation has unresolved network outcome")
            # A corrupt or partially migrated row may still have live
            # bindings; replacing it would detach their immutable provenance.
            active_binding = session.scalar(select(ChannelCommunitySubscription.id).where(
                ChannelCommunitySubscription.lemmy_community_actor_id == community_actor_id,
                ChannelCommunitySubscription.status.in_(("pending", "accepted")),
            ).limit(1))
            if active_binding is not None:
                raise FollowReservationConflict("failed Follow still has active channel bindings")
            session.delete(follow)
            session.flush()
            follow = None

        if follow is None:
            follow = BridgeActorFollow(
                community_actor_id=community_actor_id,
                community_inbox_url=community_inbox_url,
                follow_activity_id=follow_activity_id,
                remote_software_profile=remote_software_profile,
                follow_dispatch_state="prepared",
                cancel_requested=False,
                status="pending",
            )
            session.add(follow)
            session.flush()
        else:
            if follow.cancel_requested or follow.follow_dispatch_state == "undo_sending":
                # Last-channel cancellation and any started Undo need a proven
                # cleanup before this operation may reserve another binding.
                raise FollowReservationConflict("Follow cancellation needs on-demand cleanup")
            if not follow.follow_activity_id or not follow.community_inbox_url:
                raise FollowReservationConflict("existing Follow has incomplete immutable identity")
            if follow.community_inbox_url != community_inbox_url:
                # The first Follow owns the recipient. A new actor document
                # cannot silently redirect an already reserved generation.
                raise FollowReservationConflict("saved Follow inbox differs from verified Group inbox")
            # Confirmed software evidence must not silently change on reuse.
            if remote_software_profile != "unknown" and follow.remote_software_profile not in (
                "unknown", remote_software_profile
            ):
                raise FollowReservationConflict("remote Group software profile conflicts with saved Follow")
            if follow.remote_software_profile == "unknown" and remote_software_profile != "unknown":
                follow.remote_software_profile = remote_software_profile

        session.add(ChannelCommunitySubscription(
            discord_channel_id=discord_channel_id,
            discord_guild_id=discord_guild_id,
            lemmy_community_actor_id=follow.community_actor_id,
            lemmy_community_name=community_name,
            lemmy_community_id=numeric_id,
            community_handle=community_handle,
            community_inbox_url=follow.community_inbox_url,
            follow_activity_id=follow.follow_activity_id,
            initiated_by_discord_user_id=initiated_by_discord_user_id,
            status="accepted" if follow.status == "accepted" else "pending",
        ))
        session.flush()
        # Even the second concurrent subscribing channel may claim a still
        # prepared Follow. The CAS below selects the unique actual sender.
        return follow, follow.follow_dispatch_state == "prepared"

    def claim_prepared_follow(self, follow_id: int) -> bool:
        """Atomically select the sole sender for a durable prepared Follow."""
        with self.session() as session:
            result = session.execute(
                update(BridgeActorFollow)
                .where(
                    BridgeActorFollow.id == follow_id,
                    BridgeActorFollow.follow_dispatch_state == "prepared",
                    BridgeActorFollow.cancel_requested.is_(False),
                    BridgeActorFollow.status.in_(("pending", "accepted")),
                )
                .values(follow_dispatch_state="sending", updated_at=utcnow())
            )
            return result.rowcount == 1

    def complete_prepared_follow(self, follow_id: int) -> bool:
        """Record proven transport completion without undoing a fast Accept."""
        with self.session() as session:
            result = session.execute(
                update(BridgeActorFollow)
                .where(
                    BridgeActorFollow.id == follow_id,
                    BridgeActorFollow.follow_dispatch_state == "sending",
                )
                .values(follow_dispatch_state="sent", updated_at=utcnow())
            )
            return result.rowcount == 1

    def accept_verified_follow(self, *, follow_activity_id: str, actor_id: str) -> bool:
        """Apply signed Accept only to the matching original Group Follow."""
        with self.session() as session:
            # A guarded UPDATE obtains the write lock before reading bindings.
            # It cannot race a last-unsubscribe cancellation into acceptance.
            updated = session.execute(update(BridgeActorFollow).where(
                BridgeActorFollow.follow_activity_id == follow_activity_id,
                BridgeActorFollow.community_actor_id == actor_id,
                BridgeActorFollow.cancel_requested.is_(False),
                BridgeActorFollow.follow_dispatch_state != "undo_sending",
                BridgeActorFollow.status == "pending",
            ).values(status="accepted", updated_at=utcnow()))
            if updated.rowcount != 1:
                return False
            for sub in session.scalars(select(ChannelCommunitySubscription).where(
                ChannelCommunitySubscription.lemmy_community_actor_id == actor_id,
                ChannelCommunitySubscription.follow_activity_id == follow_activity_id,
                ChannelCommunitySubscription.status == "pending",
            )):
                sub.status = "accepted"
                sub.updated_at = utcnow()
            session.flush()
            return True

    def accept_implicit_threadiverse_follow(self, community_actor_id: str) -> list[ChannelCommunitySubscription]:
        """Atomically accept only a proven Lemmy Follow, never NodeBB/unknown."""
        with self.session() as session:
            # The UPDATE is the authorization boundary: concurrent software
            # reclassification, Undo and duplicate deliveries cannot bypass it.
            updated = session.execute(update(BridgeActorFollow).where(
                BridgeActorFollow.community_actor_id == community_actor_id,
                BridgeActorFollow.remote_software_profile == "threadiverse",
                BridgeActorFollow.follow_activity_id.is_not(None),
                BridgeActorFollow.community_inbox_url.is_not(None),
                BridgeActorFollow.cancel_requested.is_(False),
                BridgeActorFollow.follow_dispatch_state != "undo_sending",
                BridgeActorFollow.status == "pending",
            ).values(status="accepted", updated_at=utcnow()))
            if updated.rowcount != 1:
                return []
            follow_id = session.scalar(select(BridgeActorFollow.follow_activity_id).where(
                BridgeActorFollow.community_actor_id == community_actor_id,
            ))
            pending = list(session.scalars(select(ChannelCommunitySubscription).where(
                ChannelCommunitySubscription.lemmy_community_actor_id == community_actor_id,
                ChannelCommunitySubscription.follow_activity_id == follow_id,
                ChannelCommunitySubscription.status == "pending",
            )))
            for sub in pending:
                sub.status = "accepted"
                sub.updated_at = utcnow()
            session.flush()
            return pending

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
