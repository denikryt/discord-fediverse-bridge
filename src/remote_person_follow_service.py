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

    def __init__(self, database: Database, gateway: object, origin: str,
                 policy_service: object) -> None:
        """Bind the durable SQLite writer to an authenticated gateway client."""
        self.database = database
        self.gateway = gateway
        self.origin = origin
        self.policy_service = policy_service

    def _federation_allowed(self, actor_id: str, inbox_url: str | None = None) -> bool:
        """Require current policy for both signed identity and delivery endpoint."""
        return (self.policy_service.federation_decision(actor_id).allowed
                and (inbox_url is None or self.policy_service.federation_decision(inbox_url).allowed))

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
        if not self.policy_service.is_discord_guild_allowed(discord_guild_id):
            return RemotePersonFollowResult('guild_denied')
        # Reject blocked handles before WebFinger: resolving an expressly
        # denied host must not initiate federation network traffic.
        actor_host = actor.rsplit('@', 1)[-1] if '@' in actor and '://' not in actor else actor
        if not self.policy_service.federation_decision(actor_host).allowed:
            return RemotePersonFollowResult('federation_denied')
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
        if not self._federation_allowed(resolved.actor_id, resolved.inbox_url):
            return RemotePersonFollowResult('federation_denied')
        with self.database.immediate_session() as session:
            if (not self.policy_service.is_discord_guild_allowed(discord_guild_id)
                    or not self._federation_allowed(resolved.actor_id, resolved.inbox_url)):
                return RemotePersonFollowResult('federation_denied')
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

    def cancel_follow(self, *, discord_user_id: str, remote_actor_id: str) -> str:
        """Remove every destination of one owner and request their exact Undo.

        The last-link suppression shares a BEGIN IMMEDIATE writer with removal;
        subscriptions of unrelated owners cannot be revoked by this operation.
        """
        from .remote_person_feeds import RemotePersonChannelService
        with self.database.immediate_session() as session:
            user = session.scalar(select(User).where(User.discord_user_id == discord_user_id))
            if user is None:
                return 'not_following'
            row = session.scalar(select(RemotePersonFollow).where(
                RemotePersonFollow.user_id == user.id,
                RemotePersonFollow.remote_actor_id == remote_actor_id,
                RemotePersonFollow.relationship_status.in_(('pending', 'accepted')),
            ))
            if row is None:
                return 'not_following'
            targets = session.scalars(select(RemotePersonChannelAssignment).where(
                RemotePersonChannelAssignment.follow_id == row.id,
            )).all()
            coordinates = {(item.discord_guild_id, item.discord_channel_id) for item in targets}
            for item in targets:
                session.delete(item)
            session.flush()
            row.relationship_status = 'cancel_requested'
            if row.undo_dispatch_state == 'not_requested':
                row.undo_dispatch_state = 'prepared'
            session.flush()
            # Preserve pending posts in channels still owned by another accepted
            # subscriber, while suppressing old work in newly empty channels.
            for guild_id, channel_id in coordinates:
                if not RemotePersonChannelService._active_owner_exists(session, remote_actor_id, guild_id, channel_id):
                    RemotePersonChannelService._suppress_pending_creates(session, remote_actor_id, guild_id, channel_id)
            return 'cancel_requested'

    def apply_remote_decision(
        self, follow_activity_id: str, remote_actor_id: str, decision: str,
        *, local_recipient_actor_id: str | None = None,
    ) -> str:
        """Linearize signed Accept/Reject by current generation and exact sender.

        The gateway verifies the signed remote actor. Its completion is a
        relationship transition, independent from Follow HTTP transport state.
        """
        if decision not in ('accepted', 'rejected'):
            raise ValueError('unsupported remote Follow decision')
        with self.database.immediate_session() as session:
            row = session.scalar(select(RemotePersonFollow).where(
                RemotePersonFollow.follow_activity_id == follow_activity_id,
                RemotePersonFollow.remote_actor_id == remote_actor_id,
            ))
            if row is None:
                return 'unmatched'
            owner = session.get(User, row.user_id)
            if owner is None or not follow_activity_id.startswith(f'{owner.actor_url}/activities/follow/'):
                return 'unmatched'
            if local_recipient_actor_id is not None and owner.actor_url != local_recipient_actor_id:
                # Inbox ownership is verified by the HTTP route, never by the
                # remote activity's self-declared `to` recipient.
                return 'unmatched'
            if row.relationship_status == decision:
                return 'duplicate'
            if row.relationship_status != 'pending' or row.follow_dispatch_state not in ('sending', 'sent', 'uncertain'):
                # Unsent, cancelled, rejected, or an older Follow generation
                # cannot authorize an incoming relationship or a new channel.
                return 'unmatched'
            row.relationship_status = decision
            return 'applied'

    async def dispatch_cancellations(self, limit: int = 20) -> int:
        """Undo only the signed Follow of a departing user, preserving its ID."""
        completed = 0
        for _ in range(limit):
            token = uuid4().hex
            with self.database.immediate_session() as session:
                row = session.scalar(select(RemotePersonFollow).where(
                    RemotePersonFollow.relationship_status == 'cancel_requested',
                    RemotePersonFollow.undo_dispatch_state == 'prepared',
                    RemotePersonFollow.follow_dispatch_state != 'sending',
                ).order_by(RemotePersonFollow.id).limit(1))
                if row is None:
                    break
                # A Follow that never left Python does not require network Undo.
                if row.follow_dispatch_state == 'prepared':
                    row.relationship_status = 'cancelled'
                    row.undo_dispatch_state = 'not_requested'
                    continue
                user = session.get(User, row.user_id)
                if user is None:
                    raise ValueError('persisted Follow cancellation has no user owner')
                row.undo_dispatch_state = 'sending'
                row.undo_claim_token = token
                prepared = PreparedPersonFollow(
                    actor_username=user.activitypub_username,
                    actor_url=user.actor_url,
                    remote_actor_id=row.remote_actor_id,
                    remote_inbox_url=row.remote_inbox_url,
                    follow_activity_id=row.follow_activity_id,
                )
                row_id = row.id
            try:
                await self.gateway.send_prepared_person_undo(prepared)
                next_status, last_error = 'sent', None
                completed += 1
            except FollowNotDispatchedError as error:
                next_status, last_error = 'prepared', str(error)
            except Exception as error:
                # Ambiguous Undo must not be blindly duplicated or silently
                # counted as confirmed cancellation.
                next_status, last_error = 'uncertain', str(error)
            with self.database.immediate_session() as session:
                row = session.get(RemotePersonFollow, row_id)
                if row is not None and row.undo_claim_token == token and row.undo_dispatch_state == 'sending':
                    row.undo_dispatch_state = next_status
                    row.undo_claim_token = None
                    row.last_error = last_error
                    if next_status == 'sent':
                        row.relationship_status = 'cancelled'
            if next_status == 'prepared':
                break
        return completed

    async def dispatch_prepared(self, limit: int = 20) -> int:
        """Claim one prepared generation at a time and persist exact send outcomes."""
        completed = 0
        attempted = 0
        last_scanned_id = 0
        while attempted < limit:
            token = uuid4().hex
            with self.database.immediate_session() as session:
                # Advance across prepared rows without reselecting a blocked
                # or proven-unsent Follow in the same pass. Only actual send
                # attempts count against the dispatch budget.
                row = session.scalar(select(RemotePersonFollow).where(
                    RemotePersonFollow.relationship_status == 'pending',
                    RemotePersonFollow.follow_dispatch_state == 'prepared',
                    RemotePersonFollow.id > last_scanned_id,
                ).order_by(RemotePersonFollow.id).limit(1))
                if row is None:
                    break
                last_scanned_id = row.id
                user = session.get(User, row.user_id)
                if user is None:
                    raise ValueError('persisted Follow has no local Person owner')
                if not self._federation_allowed(row.remote_actor_id, row.remote_inbox_url):
                    # Policy denial happens before transport. Keep the same
                    # prepared identity for a future explicitly authorized
                    # attempt; it is not a network-ambiguous outcome.
                    row.last_error = 'federation policy denied outbound Follow'
                    continue
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
            attempted += 1
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
            # A proved pre-network failure stays prepared under its original
            # ID, but must not starve younger independent Follow generations.
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
