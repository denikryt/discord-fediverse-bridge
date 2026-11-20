from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import select

from ...models import (
    CommunityMessageGroup,
    CommunityMessageGroupDelivery,
    CommunityThreadGroup,
    CommunityThreadGroupDelivery,
    CommunityDiscordTargetClaim,
    ChannelCommunitySubscription,
)
from .base import BaseRepository


"""Discord fanout group and delivery persistence."""


class RemoteCommunityGroupConflict(ValueError):
    """Reject an AP replay that conflicts with immutable Group provenance."""


@dataclass(frozen=True, slots=True)
class RemoteTargetReservation:
    """One winner's claim token or a durable non-send outcome."""

    state: str
    attempt_id: str | None = None
    view_revision: int | None = None
    display_object_json: dict | None = None


class DiscordFanoutGroupRepository(BaseRepository):
    """Persist the discord fanout groups domain."""

    def __init__(self, session_provider, immediate_session_provider=None) -> None:
        """Use writer-reserved transactions for canonical and pre-REST claims."""
        super().__init__(session_provider)
        self._immediate_session_provider = immediate_session_provider or session_provider

    def immediate_session(self):
        """Open the Database-owned SQLite BEGIN IMMEDIATE transaction."""
        return self._immediate_session_provider()

    def create_thread_group(
        self,
        *,
        community_actor_id: str,
        source_channel_id: int | None,
        source_thread_id: int | None,
        source_starter_message_id: int | None,
        ap_activity_id: str | None = None,
        ap_object_id: str | None = None,
    ) -> CommunityThreadGroup:
        """Create the canonical thread-group record for one source thread event.

        source_* fields are None for inbound AP events, which create Discord threads
        in all subscribed channels simultaneously without a single source channel.
        """
        with self.session() as session:
            group = CommunityThreadGroup(
                community_actor_id=community_actor_id,
                source_channel_id=source_channel_id,
                source_thread_id=source_thread_id,
                source_starter_message_id=source_starter_message_id,
                ap_activity_id=ap_activity_id,
                ap_object_id=ap_object_id,
            )
            session.add(group)
            session.flush()
            return group

    def get_source_starter_delivery_by_message(
        self, message_id: int
    ) -> CommunityThreadGroupDelivery | None:
        """Admit only the native source starter of a retained Community root.

        A mirrored/inbound starter has the same shape of Discord ID but must
        never authorize a local Person Update. Match both source fields and
        the delivery role instead of assuming any thread starter is the owner.
        """
        with self.session() as session:
            return session.scalar(select(CommunityThreadGroupDelivery).join(
                CommunityThreadGroup,
                CommunityThreadGroup.id == CommunityThreadGroupDelivery.thread_group_id,
            ).where(
                CommunityThreadGroupDelivery.discord_starter_message_id == message_id,
                CommunityThreadGroupDelivery.role == 'source',
                CommunityThreadGroup.source_starter_message_id == message_id,
                CommunityThreadGroup.source_thread_id == CommunityThreadGroupDelivery.discord_thread_id,
                CommunityThreadGroup.source_channel_id == CommunityThreadGroupDelivery.discord_channel_id,
            ))

    def get_thread_group_by_source_thread(
        self, source_thread_id: int
    ) -> CommunityThreadGroup | None:
        """Load the thread group that owns one source Discord thread."""
        with self.session() as session:
            return session.scalar(
                select(CommunityThreadGroup).where(
                    CommunityThreadGroup.source_thread_id == source_thread_id
                )
            )

    def get_thread_group_by_ap_object(
        self, ap_object_id: str, *, community_actor_id: str | None = None
    ) -> CommunityThreadGroup | None:
        """Find a post by AP ID; incoming remote lookups must specify Group."""
        with self.session() as session:
            query = select(CommunityThreadGroup).where(
                CommunityThreadGroup.ap_object_id == ap_object_id
            )
            if community_actor_id is not None:
                query = query.where(CommunityThreadGroup.community_actor_id == community_actor_id)
            return session.scalar(query)

    def get_thread_group_by_id(
        self, thread_group_id: int
    ) -> CommunityThreadGroup | None:
        """Retrieve a thread group by its primary key ID."""
        with self.session() as session:
            return session.get(CommunityThreadGroup, thread_group_id)

    def get_thread_group_by_any_thread(
        self, discord_thread_id: int
    ) -> CommunityThreadGroup | None:
        """Load the thread group for any thread (source, mirror, or inbound).

        Phase 9: Looks up the thread group via CommunityThreadGroupDelivery,
        allowing mirror and inbound threads to resolve their post context.
        Unlike get_thread_group_by_source_thread, this works for any role.
        """
        with self.session() as session:
            delivery = session.scalar(
                select(CommunityThreadGroupDelivery).where(
                    CommunityThreadGroupDelivery.discord_thread_id == discord_thread_id
                )
            )
            if delivery is None:
                return None
            return session.scalar(
                select(CommunityThreadGroup).where(
                    CommunityThreadGroup.id == delivery.thread_group_id
                )
            )

    def add_thread_delivery(
        self,
        *,
        thread_group_id: int,
        discord_channel_id: int,
        discord_thread_id: int,
        discord_starter_message_id: int,
        role: str,
    ) -> CommunityThreadGroupDelivery:
        """Record one per-channel thread delivery for a CommunityThreadGroup."""
        with self.session() as session:
            delivery = CommunityThreadGroupDelivery(
                thread_group_id=thread_group_id,
                discord_channel_id=discord_channel_id,
                discord_thread_id=discord_thread_id,
                discord_starter_message_id=discord_starter_message_id,
                role=role,
            )
            session.add(delivery)
            session.flush()
            return delivery

    def get_thread_deliveries(
        self, thread_group_id: int
    ) -> list[CommunityThreadGroupDelivery]:
        """Load all delivery rows for one thread group."""
        # Fanout routing needs to know which channels have already received
        # a thread so it can skip those and only deliver to remaining targets.
        with self.session() as session:
            return list(
                session.scalars(
                    select(CommunityThreadGroupDelivery).where(
                        CommunityThreadGroupDelivery.thread_group_id == thread_group_id
                    )
                )
            )

    def get_thread_delivery_by_thread(
        self, discord_thread_id: int
    ) -> CommunityThreadGroupDelivery | None:
        """Load the delivery row for one Discord thread ID."""
        with self.session() as session:
            return session.scalar(
                select(CommunityThreadGroupDelivery).where(
                    CommunityThreadGroupDelivery.discord_thread_id == discord_thread_id
                )
            )

    def create_message_group(
        self,
        *,
        community_actor_id: str,
        thread_group_id: int,
        source_channel_id: int | None,
        source_thread_id: int | None,
        source_message_id: int | None,
        ap_activity_id: str | None = None,
        ap_object_id: str | None = None,
        parent_message_group_id: int | None = None,
    ) -> CommunityMessageGroup:
        """Create the canonical message-group record for one source message event.

        source_* fields are None for inbound AP events, which deliver into all
        subscribed threads simultaneously without a single source message.
        """
        with self.session() as session:
            group = CommunityMessageGroup(
                community_actor_id=community_actor_id,
                thread_group_id=thread_group_id,
                source_channel_id=source_channel_id,
                source_thread_id=source_thread_id,
                source_message_id=source_message_id,
                ap_activity_id=ap_activity_id,
                ap_object_id=ap_object_id,
                parent_message_group_id=parent_message_group_id,
            )
            session.add(group)
            session.flush()
            return group

    def get_message_group_by_id(
        self, message_group_id: int
    ) -> CommunityMessageGroup | None:
        """Retrieve a message group by its primary key ID."""
        with self.session() as session:
            return session.get(CommunityMessageGroup, message_group_id)

    def get_message_group_by_source_message(
        self, source_message_id: int
    ) -> CommunityMessageGroup | None:
        """Load the message group that owns one source Discord message."""
        with self.session() as session:
            return session.scalar(
                select(CommunityMessageGroup).where(
                    CommunityMessageGroup.source_message_id == source_message_id
                )
            )

    def get_message_group_by_ap_object(
        self, ap_object_id: str, *, community_actor_id: str | None = None
    ) -> CommunityMessageGroup | None:
        """Find a reply by AP ID, restricted to its verified remote Group."""
        with self.session() as session:
            query = select(CommunityMessageGroup).where(
                CommunityMessageGroup.ap_object_id == ap_object_id
            )
            if community_actor_id is not None:
                query = query.where(CommunityMessageGroup.community_actor_id == community_actor_id)
            return session.scalar(query)

    def get_message_group_by_delivered_message(
        self, discord_message_id: int
    ) -> CommunityMessageGroup | None:
        """Load the message group that has one Discord message as a delivery."""
        # Reply-chain resolution needs to find the parent message group by the
        # Discord message ID so the parent AP object ID can be resolved.
        with self.session() as session:
            delivery = session.scalar(
                select(CommunityMessageGroupDelivery).where(
                    CommunityMessageGroupDelivery.discord_message_id
                    == discord_message_id
                )
            )
            if delivery is None:
                return None
            return session.scalar(
                select(CommunityMessageGroup).where(
                    CommunityMessageGroup.id == delivery.message_group_id
                )
            )

    def add_message_delivery(
        self,
        *,
        message_group_id: int,
        discord_channel_id: int,
        discord_thread_id: int,
        discord_message_id: int,
        role: str,
    ) -> CommunityMessageGroupDelivery:
        """Record one per-channel message delivery for a CommunityMessageGroup."""
        with self.session() as session:
            delivery = CommunityMessageGroupDelivery(
                message_group_id=message_group_id,
                discord_channel_id=discord_channel_id,
                discord_thread_id=discord_thread_id,
                discord_message_id=discord_message_id,
                role=role,
            )
            session.add(delivery)
            session.flush()
            return delivery

    def get_message_delivery_by_message(
        self, discord_message_id: int
    ) -> CommunityMessageGroupDelivery | None:
        """Load the delivery row for one Discord message ID.

        Used by on_raw_message_edit and on_raw_message_delete to check the
        delivery role before deciding whether to propagate the event to AP.
        Returns None if the message is not part of any tracked delivery.
        """
        with self.session() as session:
            return session.scalar(
                select(CommunityMessageGroupDelivery).where(
                    CommunityMessageGroupDelivery.discord_message_id
                    == discord_message_id
                )
            )

    def get_message_deliveries(
        self, message_group_id: int
    ) -> list[CommunityMessageGroupDelivery]:
        """Load all delivery rows for one message group."""
        with self.session() as session:
            return list(
                session.scalars(
                    select(CommunityMessageGroupDelivery).where(
                        CommunityMessageGroupDelivery.message_group_id
                        == message_group_id
                    )
                )
            )

    def get_message_delivery_in_thread(
        self, message_group_id: int, discord_thread_id: int
    ) -> CommunityMessageGroupDelivery | None:
        """Load the delivery row for one message group in one specific thread."""
        # Used by fanout retry logic to check whether a specific thread already
        # received this message before attempting a duplicate delivery.
        with self.session() as session:
            return session.scalar(
                select(CommunityMessageGroupDelivery).where(
                    CommunityMessageGroupDelivery.message_group_id == message_group_id,
                    CommunityMessageGroupDelivery.discord_thread_id
                    == discord_thread_id,
                )
            )

    def get_or_create_remote_thread_group(
        self, *, community_actor_id: str, ap_object_id: str,
        source_owner_actor_id: str, ap_activity_id: str,
        first_object_json: dict | None = None,
    ) -> CommunityThreadGroup:
        """Reserve a post before REST, with immutable Group/object/owner identity."""
        if not all((community_actor_id, ap_object_id, source_owner_actor_id, ap_activity_id)):
            raise RemoteCommunityGroupConflict("remote root requires verified Group and owner")
        with self.immediate_session() as session:
            group = session.scalar(select(CommunityThreadGroup).where(
                CommunityThreadGroup.community_actor_id == community_actor_id,
                CommunityThreadGroup.ap_object_id == ap_object_id,
            ))
            if group is not None:
                if group.source_owner_actor_id != source_owner_actor_id:
                    raise RemoteCommunityGroupConflict("remote post owner differs from original Create")
                if group.source_channel_id is not None or group.source_thread_id is not None:
                    raise RemoteCommunityGroupConflict("outbound source is not a remote post reservation")
                return group
            group = CommunityThreadGroup(
                community_actor_id=community_actor_id, ap_object_id=ap_object_id,
                ap_activity_id=ap_activity_id, source_owner_actor_id=source_owner_actor_id,
                inbound_create_snapshot_json=first_object_json,
                display_object_json=first_object_json,
                source_channel_id=None, source_thread_id=None, source_starter_message_id=None,
            )
            session.add(group)
            session.flush()
            return group

    def get_or_create_remote_message_group(
        self, *, community_actor_id: str, ap_object_id: str,
        source_owner_actor_id: str, ap_activity_id: str, thread_group_id: int,
        parent_message_group_id: int | None,
        first_object_json: dict | None = None,
    ) -> CommunityMessageGroup:
        """Reserve one immutable comment and verify parent/thread belong to this Group."""
        if not all((community_actor_id, ap_object_id, source_owner_actor_id, ap_activity_id)):
            raise RemoteCommunityGroupConflict("remote reply requires verified Group and owner")
        with self.immediate_session() as session:
            root = session.get(CommunityThreadGroup, thread_group_id)
            if root is None or root.community_actor_id != community_actor_id:
                raise RemoteCommunityGroupConflict("reply root belongs to another Group")
            if parent_message_group_id is not None:
                parent = session.get(CommunityMessageGroup, parent_message_group_id)
                if parent is None or parent.thread_group_id != root.id or parent.community_actor_id != community_actor_id:
                    raise RemoteCommunityGroupConflict("reply parent belongs to another Group/root")
            group = session.scalar(select(CommunityMessageGroup).where(
                CommunityMessageGroup.community_actor_id == community_actor_id,
                CommunityMessageGroup.ap_object_id == ap_object_id,
            ))
            if group is not None:
                if (group.source_owner_actor_id != source_owner_actor_id
                    or group.thread_group_id != thread_group_id
                    or group.parent_message_group_id != parent_message_group_id):
                    raise RemoteCommunityGroupConflict("remote reply owner, root or parent conflict")
                if group.source_channel_id is not None or group.source_message_id is not None:
                    raise RemoteCommunityGroupConflict("outbound source is not a remote reply reservation")
                return group
            group = CommunityMessageGroup(
                community_actor_id=community_actor_id, ap_object_id=ap_object_id,
                ap_activity_id=ap_activity_id, source_owner_actor_id=source_owner_actor_id,
                inbound_create_snapshot_json=first_object_json,
                display_object_json=first_object_json,
                thread_group_id=thread_group_id, parent_message_group_id=parent_message_group_id,
                source_channel_id=None, source_thread_id=None, source_message_id=None,
            )
            session.add(group)
            session.flush()
            return group

    def reserve_remote_target(
        self, *, kind: str, group_id: int, discord_channel_id: int
    ) -> RemoteTargetReservation:
        """Use one serialized v1 claim before each potentially mutating Discord REST call.

        The current channel binding must still be accepted for this immutable
        Group inside the writer transaction. Otherwise return not_subscribed
        without altering an existing claim. Completed/unknown/in-flight attempts
        cannot be resent; only proven failed pre-send attempts can get a new token.
        """
        if kind not in ("post", "comment"):
            raise ValueError("invalid remote target kind")
        group_fk = (CommunityDiscordTargetClaim.thread_group_id if kind == "post"
                    else CommunityDiscordTargetClaim.message_group_id)
        delivery = CommunityThreadGroupDelivery if kind == "post" else CommunityMessageGroupDelivery
        delivery_fk = delivery.thread_group_id if kind == "post" else delivery.message_group_id
        with self.immediate_session() as session:
            # A previously read candidate is not permission to place content.
            # Resolve the immutable canonical Group inside the same SQLite writer
            # transaction as the delivery/claim decision, and require an exact,
            # currently accepted channel binding before any new Discord attempt.
            group = session.get(
                CommunityThreadGroup if kind == "post" else CommunityMessageGroup,
                group_id,
            )
            if group is None or session.scalar(select(ChannelCommunitySubscription.id).where(
                ChannelCommunitySubscription.discord_channel_id == discord_channel_id,
                ChannelCommunitySubscription.lemmy_community_actor_id == group.community_actor_id,
                ChannelCommunitySubscription.status == "accepted",
            )) is None:
                return RemoteTargetReservation("not_subscribed")
            # An authenticated Group tombstone fences even a newly subscribed
            # destination. A Person Note returning HTTP 200 cannot override it.
            if group.view_state == "deleted":
                return RemoteTargetReservation("deleted")
            if group.display_object_json is None:
                return RemoteTargetReservation("unverified_view")
            if session.scalar(select(delivery.id).where(
                delivery_fk == group_id, delivery.discord_channel_id == discord_channel_id
            )) is not None:
                return RemoteTargetReservation("delivered")
            claim = session.scalar(select(CommunityDiscordTargetClaim).where(
                group_fk == group_id,
                CommunityDiscordTargetClaim.discord_channel_id == discord_channel_id,
            ))
            if claim is not None and claim.status != "failed":
                return RemoteTargetReservation(claim.status)
            attempt_id = uuid4().hex
            if claim is None:
                claim = CommunityDiscordTargetClaim(
                    thread_group_id=group_id if kind == "post" else None,
                    message_group_id=group_id if kind == "comment" else None,
                    discord_channel_id=discord_channel_id, kind=kind, status="claimed",
                )
                session.add(claim)
            claim.attempt_id = attempt_id
            claim.attempt_started_at = datetime.now(timezone.utc)
            claim.status = "claimed"
            claim.reserved_view_revision = group.view_revision
            claim.reconciliation_marker_version = 1
            session.flush()
            return RemoteTargetReservation(
                "reserved", attempt_id, group.view_revision, dict(group.display_object_json)
            )

    def confirm_remote_target_pre_send(
        self, *, kind: str, group_id: int, discord_channel_id: int, attempt_id: str,
    ) -> bool:
        """Recheck the reserved Group revision immediately before Discord POST.

        All read-only Discord channel/parent checks may suspend this caller.
        A tombstone, revision advance, removed subscription or replaced attempt
        during those awaits must suppress an unstarted outbound Create.
        """
        if kind not in ("post", "comment"):
            raise ValueError("invalid remote target kind")
        group_fk = (CommunityDiscordTargetClaim.thread_group_id if kind == "post"
                    else CommunityDiscordTargetClaim.message_group_id)
        with self.immediate_session() as session:
            claim = session.scalar(select(CommunityDiscordTargetClaim).where(
                group_fk == group_id,
                CommunityDiscordTargetClaim.discord_channel_id == discord_channel_id,
                CommunityDiscordTargetClaim.kind == kind,
                CommunityDiscordTargetClaim.attempt_id == attempt_id,
                CommunityDiscordTargetClaim.status == "claimed",
            ))
            if claim is None:
                return False
            group = session.get(CommunityThreadGroup if kind == "post"
                                else CommunityMessageGroup, group_id)
            still_allowed = (group is not None and group.view_state == "live"
                and group.view_revision == claim.reserved_view_revision
                and session.scalar(select(ChannelCommunitySubscription.id).where(
                    ChannelCommunitySubscription.discord_channel_id == discord_channel_id,
                    ChannelCommunitySubscription.lemmy_community_actor_id == group.community_actor_id,
                    ChannelCommunitySubscription.status == "accepted",
                )) is not None)
            if not still_allowed:
                # This attempt has not crossed the POST boundary, so failure
                # (not unknown) can be re-reserved against a newer live view.
                claim.status = "failed"
                claim.reconciliation_marker_version = None
            return still_allowed

    def finish_remote_target(
        self, *, kind: str, group_id: int, discord_channel_id: int, attempt_id: str,
        discord_thread_id: int, discord_object_id: int,
    ) -> None:
        """Atomically record the proven Discord surface and complete its CAS claim."""
        with self.immediate_session() as session:
            claim = self._claimed_target(session, kind, group_id, discord_channel_id, attempt_id)
            if kind == "post":
                delivery = CommunityThreadGroupDelivery(
                    thread_group_id=group_id, discord_channel_id=discord_channel_id,
                    discord_thread_id=discord_thread_id,
                    discord_starter_message_id=discord_object_id, role="inbound")
            else:
                delivery = CommunityMessageGroupDelivery(
                    message_group_id=group_id, discord_channel_id=discord_channel_id,
                    discord_thread_id=discord_thread_id, discord_message_id=discord_object_id,
                    role="inbound")
            session.add(delivery)
            session.flush()
            claim.status = "completed"
            # The source revision reserved before REST is the maximum proven
            # on this Discord copy. A concurrent mutation remains outstanding.
            claim.applied_revision = claim.reserved_view_revision
            claim.mutation_attempt_token = None
            claim.next_attempt_at = None

    def mark_remote_target(
        self, *, kind: str, group_id: int, discord_channel_id: int,
        attempt_id: str, status: str
    ) -> None:
        """Save a proven pre-send failure or an ambiguous external result."""
        if status not in ("failed", "unknown"):
            raise ValueError("invalid target failure status")
        with self.immediate_session() as session:
            claim = self._claimed_target(session, kind, group_id, discord_channel_id, attempt_id)
            claim.status = status
            if status == "failed":
                # A proven pre-send failure never has a Discord history marker
                # worth reconciling; the next reservation builds a fresh one.
                claim.reconciliation_marker_version = None

    @staticmethod
    def _claimed_target(session, kind: str, group_id: int, channel_id: int, attempt_id: str):
        """Fence stale callbacks against replacement attempts and wrong typed FK."""
        column = CommunityDiscordTargetClaim.thread_group_id if kind == "post" else CommunityDiscordTargetClaim.message_group_id
        claim = session.scalar(select(CommunityDiscordTargetClaim).where(
            column == group_id, CommunityDiscordTargetClaim.discord_channel_id == channel_id,
            CommunityDiscordTargetClaim.kind == kind,
            CommunityDiscordTargetClaim.status.in_(("claimed", "unknown")),
            CommunityDiscordTargetClaim.attempt_id == attempt_id,
        ))
        if claim is None:
            raise RemoteCommunityGroupConflict("stale or unverified Discord target attempt")
        return claim
