"""Shared Discord channel admission for local-user-owned remote Person follows.

The service performs no federation network I/O. Commands must first run the
existing DiscordOps policy guard; the caller then passes effective guild,
channel, and bot permission facts to the serialized database boundary.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .channel_ownership import resolve_channel_owner
from .db import Database
from .models import (
    User, RemotePersonFollow, RemotePersonChannelAssignment,
    RemotePersonPost, RemotePersonPostDelivery, RemotePersonCommentPlacement,
)


REQUIRED_BOT_PERMISSIONS = (
    "view_channel", "read_message_history", "send_messages",
    "manage_webhooks", "create_public_threads", "send_messages_in_threads",
)


@dataclass(frozen=True, slots=True)
class RemotePersonChannelResult:
    """Expose only mutation disposition and durable ownership to a command."""

    status: str
    follow_id: int | None = None
    assignment_id: int | None = None
    conflict_kind: str | None = None
    missing_permissions: tuple[str, ...] = ()


class RemotePersonChannelService:
    """Serialize shared assignment changes against exclusive bridge channels.

    Deleting the final accepted assignment also suppresses *unsubmitted* create
    jobs in the same writer transaction, so a later subscriber cannot resurrect
    a different subscriber's old queued publication or comment.
    """

    def __init__(self, database: Database) -> None:
        """Use the Database-owned immediate writer and existing owner lookup."""
        self.database = database

    @staticmethod
    def _missing_permissions(bot_permissions: object) -> tuple[str, ...]:
        """Require effective channel-level permissions, not role flags alone."""
        return tuple(name for name in REQUIRED_BOT_PERMISSIONS
                     if not bool(getattr(bot_permissions, name, False)))

    def add_channel(
        self, *, discord_user_id: str, remote_actor_id: str,
        discord_guild_id: int, discord_channel_id: int,
        target_channel_guild_id: int | None, channel_type: str,
        caller_can_manage_guild: bool, bot_permissions: object,
    ) -> RemotePersonChannelResult:
        """Add one user's existing Follow to an admissible guild text channel.

        Identity, ownership, and channel exclusivity are checked again inside
        BEGIN IMMEDIATE even after the command's outer authorization gate.
        """
        if not caller_can_manage_guild:
            return RemotePersonChannelResult("forbidden")
        if (discord_guild_id <= 0 or discord_channel_id <= 0
            or target_channel_guild_id != discord_guild_id or channel_type != "text"):
            return RemotePersonChannelResult("invalid_target")
        missing = self._missing_permissions(bot_permissions)
        if missing:
            return RemotePersonChannelResult("missing_bot_permissions", missing_permissions=missing)
        try:
            with self.database.immediate_session() as session:
                user = session.scalar(select(User).where(User.discord_user_id == str(discord_user_id)))
                if user is None:
                    return RemotePersonChannelResult("not_registered")
                follow = session.scalar(select(RemotePersonFollow).where(
                    RemotePersonFollow.user_id == user.id,
                    RemotePersonFollow.remote_actor_id == remote_actor_id,
                    RemotePersonFollow.relationship_status.in_(("pending", "accepted")),
                ))
                if follow is None:
                    return RemotePersonChannelResult("not_following")
                previous = session.scalar(select(RemotePersonChannelAssignment).where(
                    RemotePersonChannelAssignment.follow_id == follow.id,
                    RemotePersonChannelAssignment.discord_channel_id == discord_channel_id,
                ))
                if previous is not None:
                    return RemotePersonChannelResult("already_assigned", follow.id, previous.id)
                owner = resolve_channel_owner(session, discord_channel_id)
                if owner is not None and owner.kind != "remote_person_feed":
                    return RemotePersonChannelResult("channel_conflict", follow.id, conflict_kind=owner.kind)
                assignment = RemotePersonChannelAssignment(
                    follow_id=follow.id, discord_guild_id=discord_guild_id,
                    discord_channel_id=discord_channel_id,
                )
                session.add(assignment)
                session.flush()
                return RemotePersonChannelResult("assigned", follow.id, assignment.id)
        except IntegrityError:
            return RemotePersonChannelResult("channel_conflict")

    def remove_channel(
        self, *, discord_user_id: str, remote_actor_id: str,
        discord_guild_id: int, discord_channel_id: int,
    ) -> RemotePersonChannelResult:
        """Remove exactly the caller's assignment and cancel stale pending sends."""
        if discord_guild_id <= 0 or discord_channel_id <= 0:
            return RemotePersonChannelResult("invalid_target")
        with self.database.immediate_session() as session:
            user = session.scalar(select(User).where(User.discord_user_id == str(discord_user_id)))
            if user is None:
                return RemotePersonChannelResult("not_registered")
            rows = session.execute(select(RemotePersonChannelAssignment, RemotePersonFollow).join(
                RemotePersonFollow,
                RemotePersonFollow.id == RemotePersonChannelAssignment.follow_id,
            ).where(
                RemotePersonFollow.user_id == user.id,
                RemotePersonFollow.remote_actor_id == remote_actor_id,
                RemotePersonChannelAssignment.discord_guild_id == discord_guild_id,
                RemotePersonChannelAssignment.discord_channel_id == discord_channel_id,
            )).all()
            if not rows:
                return RemotePersonChannelResult("not_assigned")
            assignment, follow = rows[-1]
            assignment_id = assignment.id
            session.delete(assignment)
            session.flush()
            # Follow cancellation belongs to the *user*, not the shared
            # channel. Other accepted users keep their independent rows.
            remaining = session.scalar(select(RemotePersonChannelAssignment.id).where(
                RemotePersonChannelAssignment.follow_id == follow.id,
            ).limit(1))
            if remaining is None and follow.relationship_status in ("pending", "accepted"):
                follow.relationship_status = "cancel_requested"
                if follow.undo_dispatch_state == "not_requested":
                    follow.undo_dispatch_state = "prepared"
            session.flush()
            if not self._active_owner_exists(session, remote_actor_id, discord_guild_id, discord_channel_id):
                self._suppress_pending_creates(session, remote_actor_id, discord_guild_id, discord_channel_id)
            return RemotePersonChannelResult("removed", follow.id, assignment_id)

    @staticmethod
    def _active_owner_exists(
        session: Session, actor_id: str, guild_id: int, channel_id: int,
    ) -> bool:
        """Admit only currently accepted user-owned assignments in this guild."""
        return session.scalar(select(RemotePersonChannelAssignment.id).join(
            RemotePersonFollow,
            RemotePersonFollow.id == RemotePersonChannelAssignment.follow_id,
        ).where(
            RemotePersonFollow.remote_actor_id == actor_id,
            RemotePersonFollow.relationship_status == "accepted",
            RemotePersonChannelAssignment.discord_guild_id == guild_id,
            RemotePersonChannelAssignment.discord_channel_id == channel_id,
        ).limit(1)) is not None

    @staticmethod
    def _suppress_pending_creates(
        session: Session, actor_id: str, guild_id: int, channel_id: int,
    ) -> None:
        """Cancel only pre-dispatch creates; retain delivered copies/mutations."""
        root_rows = session.scalars(select(RemotePersonPostDelivery).join(
            RemotePersonPost, RemotePersonPost.id == RemotePersonPostDelivery.post_id,
        ).where(
            RemotePersonPost.remote_actor_id == actor_id,
            RemotePersonPostDelivery.discord_guild_id == guild_id,
            RemotePersonPostDelivery.discord_channel_id == channel_id,
        )).all()
        for root in root_rows:
            if root.state in {"pending", "claimed"}:
                root.state = "suppressed"
                root.claim_token = None
                root.next_attempt_at = None
                root.last_error = "last accepted channel assignment removed before dispatch"
            # A delivered root remains accessible for authenticated Update/
            # Delete, but not-yet-dispatched bot comments/mirrors are cancelled.
            placements = session.scalars(select(RemotePersonCommentPlacement).where(
                RemotePersonCommentPlacement.root_delivery_id == root.id,
                RemotePersonCommentPlacement.state.in_((
                    "pending_parent", "pending_thread", "pending", "claimed",
                )),
            )).all()
            for placement in placements:
                placement.state = "suppressed"
                placement.claim_token = None
                placement.next_attempt_at = None
                placement.last_error = "last accepted channel assignment removed before dispatch"
