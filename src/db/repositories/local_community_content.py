from __future__ import annotations


from dataclasses import dataclass
from datetime import timedelta, timezone
from uuid import uuid4

from sqlalchemy import select

from ...models import utcnow

from ...models import (
    LocalCommunity,
    LocalCommunityMessage,
    LocalCommunityMessageSurface,
    LocalCommunityThread,
    LocalCommunityThreadSurface,
    RemoteSubscriber,
)
from .base import BaseRepository


"""Canonical local-community thread and message persistence."""


class HostIdentityConflict(ValueError):
    """The same tenant and AP object has contradictory immutable Create facts."""


@dataclass(frozen=True)
class HostReservation:
    """One canonical host row and the exclusive pre-REST attempt, if claimed."""

    canonical: LocalCommunityThread | LocalCommunityMessage
    attempt_id: str | None
    state: str


class LocalCommunityContentRepository(BaseRepository):
    """Persist the local community content domain."""

    def __init__(self, session_provider, immediate_session_provider=None) -> None:
        """Reserve inbound host attempts under the Database's BEGIN IMMEDIATE lock."""
        super().__init__(session_provider)
        self._immediate_session = immediate_session_provider or session_provider

    @staticmethod
    def _claim_host(row: LocalCommunityThread | LocalCommunityMessage) -> HostReservation:
        """Claim the unique canonical host before any Discord creation request.

        No replacement attempt is permitted after a send might have occurred.
        A stale sending attempt becomes unknown when a later verified event
        observes it; a currently running request remains the sole owner.
        """
        if row.host_delivery_state == "sending" and row.host_attempt_started_at is not None:
            started = row.host_attempt_started_at
            # SQLite DateTime loses tzinfo even when the ORM uses timezone=True.
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            if utcnow() - started > timedelta(minutes=10):
                row.host_delivery_state = "unknown"
        if row.host_delivery_state in ("reserved", "failed"):
            token = uuid4().hex
            row.host_delivery_state = "sending"
            row.host_attempt_id = token
            row.host_attempt_started_at = utcnow()
            return HostReservation(row, token, "claimed")
        return HostReservation(row, None, row.host_delivery_state or "unknown")

    @staticmethod
    def _require_accepted_source(
        session, local_community_id: int, subscriber_actor_id: str
    ) -> None:
        """Recheck the subscriber under the same writer lock as host admission."""
        subscriber = session.scalar(select(RemoteSubscriber.id).where(
            RemoteSubscriber.local_community_id == local_community_id,
            RemoteSubscriber.remote_actor_id == subscriber_actor_id,
            RemoteSubscriber.status == "accepted",
        ))
        if subscriber is None:
            # The actor-document fetch can race with a signed Undo; a stale
            # Python authorization must not commit a new Discord host claim.
            raise HostIdentityConflict("accepted origin subscriber was revoked")

    def reserve_inbound_thread(
        self, *, local_community_id: int, ap_activity_id: str,
        ap_object_id: str, source_owner_actor_id: str,
        origin_subscriber_actor_id: str, inbound_create_snapshot: dict[str, str],
    ) -> HostReservation:
        """Atomically reserve the first rendered post and its exclusive host attempt.

        A later Announce or duplicate may change its content, but cannot
        replace the already persisted first-Create Discord projection.
        """
        if not (ap_object_id and source_owner_actor_id and origin_subscriber_actor_id):
            raise HostIdentityConflict("missing verified original Create identity")
        if not (isinstance(inbound_create_snapshot, dict)
                and isinstance(inbound_create_snapshot.get("title"), str)
                and isinstance(inbound_create_snapshot.get("content"), str)):
            raise HostIdentityConflict("missing original Group post content snapshot")
        with self._immediate_session() as session:
            self._require_accepted_source(session, local_community_id, origin_subscriber_actor_id)
            row = session.scalar(select(LocalCommunityThread).where(
                LocalCommunityThread.local_community_id == local_community_id,
                LocalCommunityThread.ap_object_id == ap_object_id,
            ))
            if row is None:
                row = LocalCommunityThread(
                    local_community_id=local_community_id, ap_activity_id=ap_activity_id,
                    ap_object_id=ap_object_id, direction="ap_to_discord",
                    origin_kind="remote_follower", source_owner_actor_id=source_owner_actor_id,
                    origin_subscriber_actor_id=origin_subscriber_actor_id,
                    inbound_create_snapshot_json=dict(inbound_create_snapshot),
                    host_delivery_state="reserved",
                )
                session.add(row)
                session.flush()
            elif (
                row.direction != "ap_to_discord"
                or row.source_owner_actor_id != source_owner_actor_id
                or row.origin_subscriber_actor_id != origin_subscriber_actor_id
            ):
                # A later Announce/duplicate may not adopt an outbound or
                # ambiguous legacy row or replace a previously verified owner.
                raise HostIdentityConflict("original host post identity differs")
            if row.inbound_create_snapshot_json is None:
                # A legacy row without first-Create content cannot safely
                # use the latest envelope to populate new sibling surfaces.
                raise HostIdentityConflict("original Group post snapshot not recorded")
            reservation = self._claim_host(row)
            if reservation.state == "delivered":
                host = session.scalar(select(LocalCommunityThreadSurface.id).where(
                    LocalCommunityThreadSurface.local_community_thread_id == row.id,
                    LocalCommunityThreadSurface.role == "host",
                ))
                if host is None:
                    raise HostIdentityConflict("confirmed host thread has no surface")
            session.flush()
            return reservation

    def reserve_inbound_message(
        self, *, local_community_id: int, local_community_thread_id: int,
        ap_activity_id: str, ap_object_id: str, parent_ap_object_id: str,
        source_owner_actor_id: str, origin_subscriber_actor_id: str,
        inbound_create_snapshot: dict[str, str],
    ) -> HostReservation:
        """Freeze a reply's first body while reserving its tenant-scoped host."""
        if not (ap_object_id and parent_ap_object_id and source_owner_actor_id and origin_subscriber_actor_id):
            raise HostIdentityConflict("missing verified reply provenance")
        if not (isinstance(inbound_create_snapshot, dict)
                and isinstance(inbound_create_snapshot.get("content"), str)):
            raise HostIdentityConflict("missing original Group reply content snapshot")
        with self._immediate_session() as session:
            self._require_accepted_source(session, local_community_id, origin_subscriber_actor_id)
            thread = session.get(LocalCommunityThread, local_community_thread_id)
            if thread is None or thread.local_community_id != local_community_id:
                raise HostIdentityConflict("parent thread belongs to another Group")
            # A nested parent must already be a confirmed host reply in this
            # exact root; a same-object mapping in a neighboring tenant cannot
            # select the Discord parent for this reply.
            if parent_ap_object_id != thread.ap_object_id:
                parent = session.scalar(select(LocalCommunityMessage).where(
                    LocalCommunityMessage.local_community_id == local_community_id,
                    LocalCommunityMessage.ap_object_id == parent_ap_object_id,
                    LocalCommunityMessage.local_community_thread_id == local_community_thread_id,
                    LocalCommunityMessage.host_delivery_state == "delivered",
                ))
                if parent is None:
                    raise HostIdentityConflict("parent reply not delivered in target Group")
            row = session.scalar(select(LocalCommunityMessage).where(
                LocalCommunityMessage.local_community_id == local_community_id,
                LocalCommunityMessage.ap_object_id == ap_object_id,
            ))
            if row is None:
                row = LocalCommunityMessage(
                    local_community_id=local_community_id,
                    local_community_thread_id=local_community_thread_id,
                    ap_activity_id=ap_activity_id, ap_object_id=ap_object_id,
                    parent_ap_object_id=parent_ap_object_id, direction="ap_to_discord",
                    source_owner_actor_id=source_owner_actor_id,
                    origin_subscriber_actor_id=origin_subscriber_actor_id,
                    inbound_create_snapshot_json=dict(inbound_create_snapshot),
                    host_delivery_state="reserved",
                )
                session.add(row)
                session.flush()
            elif (
                row.direction != "ap_to_discord"
                or row.local_community_thread_id != local_community_thread_id
                or row.parent_ap_object_id != parent_ap_object_id
                or row.source_owner_actor_id != source_owner_actor_id
                or row.origin_subscriber_actor_id != origin_subscriber_actor_id
            ):
                raise HostIdentityConflict("original host reply identity differs")
            if row.inbound_create_snapshot_json is None:
                raise HostIdentityConflict("original Group reply snapshot not recorded")
            reservation = self._claim_host(row)
            if reservation.state == "delivered":
                host = session.scalar(select(LocalCommunityMessageSurface.id).where(
                    LocalCommunityMessageSurface.local_community_message_id == row.id,
                    LocalCommunityMessageSurface.role == "host",
                ))
                if host is None:
                    raise HostIdentityConflict("confirmed host reply has no surface")
            session.flush()
            return reservation

    def finish_inbound_host_attempt(
        self, *, canonical_id: int, attempt_id: str, kind: str,
        state: str, discord_thread_id: int | None = None,
        discord_message_id: int | None = None,
        parent_discord_message_id: int | None = None,
    ) -> None:
        """Fence stale results and atomically attach the confirmed host surface.

        A timeout or lost HTTP reply is unknown, never a proof of failure.
        Only the sending attempt with the original token may report delivered.
        """
        if kind not in ("post", "comment") or state not in ("delivered", "failed", "unknown"):
            raise ValueError("invalid host attempt completion")
        model = LocalCommunityThread if kind == "post" else LocalCommunityMessage
        with self._immediate_session() as session:
            row = session.get(model, canonical_id)
            if row is None or row.host_delivery_state != "sending" or row.host_attempt_id != attempt_id:
                raise HostIdentityConflict("host attempt no longer owns canonical row")
            if state == "delivered":
                if discord_message_id is None:
                    raise ValueError("confirmed host must have a Discord message ID")
                if kind == "post":
                    if discord_thread_id is None:
                        raise ValueError("confirmed thread requires Discord thread ID")
                    community = session.get(LocalCommunity, row.local_community_id)
                    if community is None:
                        raise HostIdentityConflict("host community disappeared")
                    session.add(LocalCommunityThreadSurface(
                        local_community_thread_id=row.id,
                        discord_forum_channel_id=community.discord_forum_channel_id,
                        discord_thread_id=discord_thread_id,
                        discord_starter_message_id=discord_message_id,
                        role="host", local_subscriber_id=None,
                    ))
                else:
                    parent_surface = session.scalar(select(LocalCommunityThreadSurface).where(
                        LocalCommunityThreadSurface.local_community_thread_id == row.local_community_thread_id,
                        LocalCommunityThreadSurface.role == "host",
                    ))
                    if parent_surface is None:
                        raise HostIdentityConflict("root host surface is missing")
                    session.add(LocalCommunityMessageSurface(
                        local_community_message_id=row.id,
                        local_community_thread_surface_id=parent_surface.id,
                        discord_forum_channel_id=parent_surface.discord_forum_channel_id,
                        discord_message_id=discord_message_id,
                        parent_discord_message_id=parent_discord_message_id,
                        role="host", local_subscriber_id=None,
                    ))
            row.host_delivery_state = state
            session.flush()

    def authorize_inbound_mutation(
        self, *, local_community_id: int, ap_object_id: str, kind: str,
        owner_actor_id: str, origin_subscriber_actor_id: str,
        post_ap_id: str | None, parent_ap_id: str | None,
        operation: str,
    ) -> tuple[LocalCommunityThread | LocalCommunityMessage, LocalCommunityThread] | None:
        """Read original owner and tenant with subscriber status under one lock.

        A verified sender is not allowed to adopt an unrelated tenant's object,
        modify an outbound/ambiguous legacy row, or address a foreign parent.
        All checks precede Discord edits and remote relay; no owner inference is
        made from a later ActivityPub envelope.
        """
        if kind not in ("post", "comment") or operation not in ("update", "delete"):
            raise HostIdentityConflict("unsupported mutation kind or operation")
        if not (owner_actor_id and origin_subscriber_actor_id and ap_object_id):
            raise HostIdentityConflict("mutation identity is incomplete")
        with self._immediate_session() as session:
            self._require_accepted_source(session, local_community_id, origin_subscriber_actor_id)
            model = LocalCommunityThread if kind == "post" else LocalCommunityMessage
            row = session.scalar(select(model).where(
                model.local_community_id == local_community_id,
                model.ap_object_id == ap_object_id,
            ))
            if row is None:
                return None
            if (
                row.direction != "ap_to_discord"
                or row.source_owner_actor_id != owner_actor_id
                or row.origin_subscriber_actor_id != origin_subscriber_actor_id
                or row.host_delivery_state != "delivered"
            ):
                raise HostIdentityConflict("original mutation owner, origin or host differs")
            if kind == "post":
                if post_ap_id is not None or parent_ap_id is not None:
                    raise HostIdentityConflict("post mutation supplies a foreign parent")
                thread = row
                surface_id = session.scalar(select(LocalCommunityThreadSurface.id).where(
                    LocalCommunityThreadSurface.local_community_thread_id == thread.id,
                    LocalCommunityThreadSurface.role == "host",
                ))
            else:
                thread = session.get(LocalCommunityThread, row.local_community_thread_id)
                if (
                    thread is None or thread.local_community_id != local_community_id
                    or thread.direction != "ap_to_discord"
                    or thread.host_delivery_state != "delivered"
                ):
                    raise HostIdentityConflict("comment root is not hosted in this Group")
                # Update must carry a consistent root/parent. Delete(Tombstone)
                # may be ID-only; any supplied ancestry must still agree with
                # the canonical Create and exact tenant-scoped parent chain.
                if operation == "update" and (not post_ap_id or not parent_ap_id):
                    raise HostIdentityConflict("comment Update lacks original ancestry")
                if post_ap_id is not None and post_ap_id != thread.ap_object_id:
                    raise HostIdentityConflict("comment root belongs to another Group")
                if parent_ap_id is not None and parent_ap_id != row.parent_ap_object_id:
                    raise HostIdentityConflict("comment parent differs from original Create")
                if row.parent_ap_object_id != thread.ap_object_id:
                    parent = session.scalar(select(LocalCommunityMessage.id).where(
                        LocalCommunityMessage.local_community_id == local_community_id,
                        LocalCommunityMessage.local_community_thread_id == thread.id,
                        LocalCommunityMessage.ap_object_id == row.parent_ap_object_id,
                        LocalCommunityMessage.host_delivery_state == "delivered",
                    ))
                    if parent is None:
                        raise HostIdentityConflict("original nested parent not hosted in this Group")
                surface_id = session.scalar(select(LocalCommunityMessageSurface.id).where(
                    LocalCommunityMessageSurface.local_community_message_id == row.id,
                    LocalCommunityMessageSurface.role == "host",
                ))
            if surface_id is None:
                raise HostIdentityConflict("confirmed mutation host surface is missing")
            return row, thread

    def create_local_community_thread(
        self,
        *,
        local_community_id: int,
        discord_thread_id: int,
        discord_starter_message_id: int,
        ap_activity_id: str,
        ap_object_id: str,
        direction: str,
        origin_kind: str,
    ) -> LocalCommunityThread:
        """Persist one canonical thread row plus its host Discord surface.

        Stage 2 keeps the public repository entry point small for callers while
        moving the actual Discord ownership into `LocalCommunityThreadSurface`.
        """
        with self.session() as session:
            thread = LocalCommunityThread(
                local_community_id=local_community_id,
                ap_activity_id=ap_activity_id,
                ap_object_id=ap_object_id,
                direction=direction,
                origin_kind=origin_kind,
            )
            session.add(thread)
            session.flush()
            # Stage 2 only creates host surfaces. Later stages can add more
            # surfaces for the same canonical thread without rewriting callers.
            local_community = session.get(LocalCommunity, local_community_id)
            if local_community is None:
                raise RuntimeError(
                    f"Missing LocalCommunity {local_community_id} while creating host thread surface"
                )
            session.add(
                LocalCommunityThreadSurface(
                    local_community_thread_id=thread.id,
                    discord_forum_channel_id=local_community.discord_forum_channel_id,
                    discord_thread_id=discord_thread_id,
                    discord_starter_message_id=discord_starter_message_id,
                    role="host",
                    local_subscriber_id=None,
                )
            )
            session.flush()
            return thread

    def create_local_community_thread_canonical(
        self,
        *,
        local_community_id: int,
        ap_activity_id: str,
        ap_object_id: str,
        direction: str,
        origin_kind: str,
    ) -> LocalCommunityThread:
        """Persist one canonical local-community thread without a host surface.

        Stage 4 local-subscriber source events create the source surface first
        and then fan out to host/sibling targets.  This helper keeps that path
        from incorrectly storing the source Discord thread as the host surface.
        """
        with self.session() as session:
            thread = LocalCommunityThread(
                local_community_id=local_community_id,
                ap_activity_id=ap_activity_id,
                ap_object_id=ap_object_id,
                direction=direction,
                origin_kind=origin_kind,
            )
            session.add(thread)
            session.flush()
            return thread

    def get_local_community_thread_by_ap_object_id(
        self, ap_object_id: str, *, local_community_id: int | None = None
    ) -> LocalCommunityThread | None:
        """Load a Group-scoped post, rejecting ambiguous global legacy lookup."""
        with self.session() as session:
            query = select(LocalCommunityThread).where(LocalCommunityThread.ap_object_id == ap_object_id)
            if local_community_id is not None:
                query = query.where(LocalCommunityThread.local_community_id == local_community_id)
            rows = list(session.scalars(query.limit(2)))
            # Source-only historical callers may use the global lookup only
            # while one canonical tenant candidate can be proven.
            return rows[0] if len(rows) == 1 else None

    def create_local_community_message(
        self,
        *,
        local_community_thread_id: int,
        discord_message_id: int,
        ap_activity_id: str,
        ap_object_id: str,
        parent_ap_object_id: str | None,
        parent_discord_message_id: int | None,
        direction: str,
    ) -> LocalCommunityMessage:
        """Persist one canonical message row plus its host Discord surface."""
        with self.session() as session:
            # Tenant comes from the canonical parent, never a caller-supplied
            # object ID or a global AP mapping shared with another Group.
            thread = session.get(LocalCommunityThread, local_community_thread_id)
            if thread is None:
                raise RuntimeError("Missing canonical local-community parent thread")
            message = LocalCommunityMessage(
                local_community_id=thread.local_community_id,
                local_community_thread_id=local_community_thread_id,
                ap_activity_id=ap_activity_id,
                ap_object_id=ap_object_id,
                parent_ap_object_id=parent_ap_object_id,
                direction=direction,
            )
            session.add(message)
            session.flush()
            thread_surface = session.scalar(
                select(LocalCommunityThreadSurface).where(
                    LocalCommunityThreadSurface.local_community_thread_id
                    == local_community_thread_id,
                    LocalCommunityThreadSurface.role == "host",
                )
            )
            if thread_surface is None:
                raise RuntimeError(
                    f"Missing host thread surface for local community thread {local_community_thread_id}"
                )
            session.add(
                LocalCommunityMessageSurface(
                    local_community_message_id=message.id,
                    local_community_thread_surface_id=thread_surface.id,
                    discord_forum_channel_id=thread_surface.discord_forum_channel_id,
                    discord_message_id=discord_message_id,
                    parent_discord_message_id=parent_discord_message_id,
                    role="host",
                    local_subscriber_id=None,
                )
            )
            session.flush()
            return message

    def create_local_community_message_canonical(
        self,
        *,
        local_community_thread_id: int,
        ap_activity_id: str,
        ap_object_id: str,
        parent_ap_object_id: str | None,
        direction: str,
    ) -> LocalCommunityMessage:
        """Persist one canonical local-community comment without a host surface.

        Local-subscriber source comments must first record the source message
        surface, then copy into host and sibling surfaces.  Creating a host
        surface here would bind the source Discord message to the wrong forum.
        """
        with self.session() as session:
            # Tenant comes from the canonical parent, never a caller-supplied
            # object ID or a global AP mapping shared with another Group.
            thread = session.get(LocalCommunityThread, local_community_thread_id)
            if thread is None:
                raise RuntimeError("Missing canonical local-community parent thread")
            message = LocalCommunityMessage(
                local_community_id=thread.local_community_id,
                local_community_thread_id=local_community_thread_id,
                ap_activity_id=ap_activity_id,
                ap_object_id=ap_object_id,
                parent_ap_object_id=parent_ap_object_id,
                direction=direction,
            )
            session.add(message)
            session.flush()
            return message

    def get_local_community_message_by_ap_object_id(
        self, ap_object_id: str, *, local_community_id: int | None = None
    ) -> LocalCommunityMessage | None:
        """Load a Group-scoped comment, rejecting ambiguous global lookup."""
        with self.session() as session:
            query = select(LocalCommunityMessage).where(LocalCommunityMessage.ap_object_id == ap_object_id)
            if local_community_id is not None:
                query = query.where(LocalCommunityMessage.local_community_id == local_community_id)
            rows = list(session.scalars(query.limit(2)))
            return rows[0] if len(rows) == 1 else None

    def list_delivered_local_group_message_tenants(self, ap_object_id: str) -> list[int]:
        """Resolve canonical host tenants, never infer one from MessageMapping."""
        with self.session() as session:
            return list(session.scalars(select(LocalCommunityMessage.local_community_id).where(
                LocalCommunityMessage.ap_object_id == ap_object_id,
                LocalCommunityMessage.direction == "ap_to_discord",
                LocalCommunityMessage.host_delivery_state == "delivered",
            ).order_by(LocalCommunityMessage.local_community_id).limit(3)))

    def list_local_community_messages_for_thread(
        self, local_community_thread_id: int
    ) -> list[LocalCommunityMessage]:
        """Load all mapped messages for one local-community thread."""
        with self.session() as session:
            return list(
                session.scalars(
                    select(LocalCommunityMessage)
                    .where(
                        LocalCommunityMessage.local_community_thread_id
                        == local_community_thread_id
                    )
                    .order_by(
                        LocalCommunityMessage.created_at, LocalCommunityMessage.id
                    )
                )
            )

    def get_local_community_thread_by_id(
        self, local_community_thread_id: int
    ) -> LocalCommunityThread | None:
        """Load one local-community thread row by its primary key."""
        with self.session() as session:
            return session.get(LocalCommunityThread, local_community_thread_id)
