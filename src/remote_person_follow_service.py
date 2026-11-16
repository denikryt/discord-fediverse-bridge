"""Reserve and dispatch signed local-Person Follow generations to remote actors.

This service owns the durable reservation/transport boundary, not the response
lifecycle. Signed Accept/Reject and Undo are reconciled separately by the next
stage. Network errors must never mint a replacement Follow identity.
"""
from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4
from urllib.parse import urlsplit

from sqlalchemy import func, select

from .activitypub_identity_urls import build_user_follow_activity_id
from .channel_ownership import resolve_channel_owner
from .db import Database
from .fedify_gateway_client import FollowNotDispatchedError, PreparedPersonFollow
from .models import RemotePersonChannelAssignment, RemotePersonFollow, User
from .remote_person_feeds import REQUIRED_BOT_PERMISSIONS


@dataclass(frozen=True, slots=True)
class RemotePersonFollowResult:
    """Describe one authenticated user's reserved Follow and destination."""

    status: str
    follow_activity_id: str | None = None
    follow_id: int | None = None
    missing_permissions: tuple[str, ...] = ()


class RemotePersonFollowService:
    """Reserve per-user Follow identity and permit only durable queued delivery."""

    def __init__(self, database: Database, gateway: object, origin: str) -> None:
        """Bind the durable SQLite writer to an authenticated gateway client."""
        self.database = database
        self.gateway = gateway
        self.origin = origin

    async def follow(
        self, *, discord_user_id: str, actor: str, discord_guild_id: int,
        discord_channel_id: int, target_channel_guild_id: int | None,
        channel_type: str, caller_can_manage_guild: bool, bot_permissions: object,
    ) -> RemotePersonFollowResult:
        """Verify guild capabilities, dereference a Person, and reserve before network."""
        if not caller_can_manage_guild:
            return RemotePersonFollowResult('forbidden')
        if (discord_guild_id <= 0 or discord_channel_id <= 0
                or discord_guild_id != target_channel_guild_id or channel_type != 'text'):
            return RemotePersonFollowResult('invalid_target')
        missing = tuple(name for name in REQUIRED_BOT_PERMISSIONS
                        if not bool(getattr(bot_permissions, name, False)))
        if missing:
            return RemotePersonFollowResult('missing_bot_permissions', missing_permissions=missing)
        with self.database.session() as session:
            local_user = session.scalar(select(User).where(User.discord_user_id == str(discord_user_id)))
            if local_user is None:
                return RemotePersonFollowResult('not_registered')
            owner = resolve_channel_owner(session, discord_channel_id)
            if owner is not None and owner.kind != 'remote_person_feed':
                return RemotePersonFollowResult('channel_conflict')

        # WebFinger lookup is a network boundary; only its verified canonical
        # Person actor and inbox are ever persisted, never the user's raw input.
        resolved = await self.gateway.resolve_remote_person(actor)
        if not _is_safe_remote_url(resolved.actor_id, self.origin):
            return RemotePersonFollowResult('invalid_actor')
        if not _is_safe_remote_url(resolved.inbox_url, self.origin):
            return RemotePersonFollowResult('invalid_actor')
        with self.database.immediate_session() as session:
            user = session.scalar(select(User).where(User.discord_user_id == str(discord_user_id)))
            if user is None:
                return RemotePersonFollowResult('not_registered')
            owner = resolve_channel_owner(session, discord_channel_id)
            if owner is not None and owner.kind != 'remote_person_feed':
                return RemotePersonFollowResult('channel_conflict')
            current = session.scalar(select(RemotePersonFollow).where(
                RemotePersonFollow.user_id == user.id,
                RemotePersonFollow.remote_actor_id == resolved.actor_id,
                RemotePersonFollow.relationship_status.in_(("pending", "accepted", "cancel_requested")),
            ))
            if current is not None and current.relationship_status == 'cancel_requested':
                return RemotePersonFollowResult('cancelling', current.follow_activity_id, current.id)
            is_new_follow = current is None
            if current is None:
                max_generation = session.scalar(select(func.max(RemotePersonFollow.generation)).where(
                    RemotePersonFollow.user_id == user.id,
                    RemotePersonFollow.remote_actor_id == resolved.actor_id,
                )) or 0
                current = RemotePersonFollow(
                    user_id=user.id, remote_actor_id=resolved.actor_id,
                    remote_inbox_url=resolved.inbox_url,
                    generation=max_generation + 1,
                    follow_activity_id=build_user_follow_activity_id(
                        self.origin, user.activitypub_username, uuid4().hex,
                    ),
                    relationship_status='pending', follow_dispatch_state='prepared',
                )
                session.add(current)
                session.flush()
            already = session.scalar(select(RemotePersonChannelAssignment).where(
                RemotePersonChannelAssignment.follow_id == current.id,
                RemotePersonChannelAssignment.discord_channel_id == discord_channel_id,
            ))
            if already is not None:
                return RemotePersonFollowResult('already_assigned', current.follow_activity_id, current.id)
            session.add(RemotePersonChannelAssignment(
                follow_id=current.id, discord_guild_id=discord_guild_id,
                discord_channel_id=discord_channel_id,
            ))
            return RemotePersonFollowResult(
                'prepared' if is_new_follow else 'assigned_existing',
                current.follow_activity_id, current.id,
            )

    async def dispatch_prepared(self, limit: int = 20) -> int:
        """Claim one prepared generation at a time and persist exact send outcomes."""
        completed = 0
        for _ in range(limit):
            token = uuid4().hex
            with self.database.immediate_session() as session:
                row = session.scalar(select(RemotePersonFollow).where(
                    RemotePersonFollow.relationship_status == 'pending',
                    RemotePersonFollow.follow_dispatch_state == 'prepared',
                ).order_by(RemotePersonFollow.id).limit(1))
                if row is None:
                    break
                user = session.get(User, row.user_id)
                if user is None:
                    raise ValueError('persisted Follow has no local Person owner')
                row.follow_dispatch_state = 'sending'
                row.follow_claim_token = token
                prepared = PreparedPersonFollow(
                    actor_username=user.activitypub_username,
                    actor_url=user.actor_url,
                    remote_actor_id=row.remote_actor_id,
                    remote_inbox_url=row.remote_inbox_url,
                    follow_activity_id=row.follow_activity_id,
                )
                row_id = row.id
            try:
                await self.gateway.send_prepared_person_follow(prepared)
                next_status, last_error = 'sent', None
                completed += 1
            except FollowNotDispatchedError as error:
                # Gateway proved it never attempted the network send. A
                # same-ID retry is safe; avoid a hot loop in this invocation.
                next_status, last_error = 'prepared', str(error)
            except Exception as error:
                # HTTP timeouts or errors may follow a successful remote send;
                # marking uncertain prevents a duplicate Follow on restart.
                next_status, last_error = 'uncertain', str(error)
            with self.database.immediate_session() as session:
                row = session.get(RemotePersonFollow, row_id)
                if row is not None and row.follow_claim_token == token and row.follow_dispatch_state == 'sending':
                    row.follow_dispatch_state = next_status
                    row.follow_claim_token = None
                    row.last_error = last_error
            if next_status == 'prepared':
                break
        return completed


def _is_safe_remote_url(value: str, origin: str) -> bool:
    """Reject untrusted schemes, credentials, local origin, and trivial IP escapes.

    The gateway additionally applies full remote-endpoint admission and Fedify
    WebFinger/DNS rules; this check protects Python's immutable provenance.
    """
    try:
        parsed = urlsplit(value)
        local = urlsplit(origin)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
            return False
        if parsed.fragment or parsed.hostname.lower() == local.hostname.lower():
            return False
        from ipaddress import ip_address
        try:
            address = ip_address(parsed.hostname)
        except ValueError:
            return parsed.hostname != 'localhost' and '.' in parsed.hostname
        return address.is_global
    except (TypeError, ValueError, AttributeError):
        return False
