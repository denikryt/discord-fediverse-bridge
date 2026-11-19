"""Recover ambiguous remote Community Creates and apply current Group views.

The canonical Group row owns the current display; the existing typed target
claim owns uncertainty and the applied-revision cursor. Discord REST happens
outside all writer transactions. This worker can only reconcile or edit a
bridge-created surface; it never retries an ambiguous Create with another POST.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
from uuid import uuid4

from sqlalchemy import select

from ..formatting import (has_feed_comment_identity, render_remote_community_root,
                          render_remote_community_comment)
from ..models import (ChannelCommunitySubscription, CommunityDiscordTargetClaim,
                      CommunityMessageGroup, CommunityMessageGroupDelivery,
                      CommunityThreadGroup, CommunityThreadGroupDelivery, utcnow)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class GroupClaimSnapshot:
    """Exact typed Group/channel identity detached before any Discord I/O."""

    claim_id: int
    kind: str
    group_id: int
    channel_id: int
    object_id: str
    actor_id: str
    author_name: str
    title: str | None
    body: str | None
    external_url: str | None
    view_state: str
    view_revision: int
    applied_revision: int | None
    reserved_revision: int | None
    token: str
    operation: str
    thread_id: int | None = None
    message_id: int | None = None


def _as_utc(value: datetime | None) -> datetime | None:
    """SQLite round trips can return naive datetimes from UTC-aware columns."""
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _claim_group(session, claim: CommunityDiscordTargetClaim):
    """Return the existing remote canonical row for one typed target claim."""
    return session.get(CommunityThreadGroup, claim.thread_group_id) if claim.kind == 'post' else (
        session.get(CommunityMessageGroup, claim.message_group_id))


def _inbound_delivery(session, claim: CommunityDiscordTargetClaim):
    """Require the Discord identity of the bot-owned remote materialization."""
    table = CommunityThreadGroupDelivery if claim.kind == 'post' else CommunityMessageGroupDelivery
    field = (table.thread_group_id == claim.thread_group_id if claim.kind == 'post'
             else table.message_group_id == claim.message_group_id)
    return session.scalar(select(table).where(
        field, table.discord_channel_id == claim.discord_channel_id,
        table.role == 'inbound',
    ))


def _render(snapshot: GroupClaimSnapshot) -> str:
    """Reuse the identity-safe Create renderer for retained Update/Delete."""
    body = '*Deleted from Community*' if snapshot.view_state == 'deleted' else snapshot.body
    if snapshot.kind == 'post':
        return render_remote_community_root(actor_name=snapshot.author_name,
            actor_id=snapshot.actor_id, object_id=snapshot.object_id,
            title=snapshot.title if snapshot.view_state != 'deleted' else 'Deleted from Community',
            body=body, external_url=snapshot.external_url if snapshot.view_state != 'deleted' else None)
    return render_remote_community_comment(actor_name=snapshot.author_name,
        actor_id=snapshot.actor_id, object_id=snapshot.object_id, body=body)


class CommunityDiscordViewTransport:
    """Bounded Discord history proof and bot-owned Edit; no new Create method."""

    MAX_HISTORY = 200
    MAX_FORUM_THREADS = 100

    def __init__(self, bot: object) -> None:
        """Retain the runtime's connected Discord bot client."""
        self.bot = bot

    def _bot_id(self) -> int:
        """Missing bot identity cannot prove ownership of a Discord message."""
        result = getattr(getattr(self.bot, 'user', None), 'id', None)
        if result is None:
            raise PermissionError('Discord bot identity is unavailable')
        return int(result)

    @staticmethod
    def _check_history_permissions(thread: object) -> None:
        """Reject history scans that cannot account for all visible candidates."""
        permissions = thread.permissions_for(thread.guild.me)
        if not (permissions.view_channel and permissions.read_message_history):
            raise PermissionError('Discord history is inaccessible')

    async def _thread(self, thread_id: int, channel_id: int):
        """Require exact parent forum for a proven inbound Discord surface."""
        thread = await self.bot.get_thread_by_id(thread_id)
        if (getattr(thread, 'id', None) != thread_id
                or getattr(thread, 'parent_id', None) != channel_id):
            raise PermissionError('Discord thread is outside the expected Community forum')
        self._check_history_permissions(thread)
        return thread

    async def reconcile(self, snapshot: GroupClaimSnapshot) -> tuple[int, int] | None:
        """Adopt only one complete bot-owned marker in the exact destination."""
        if snapshot.kind == 'comment':
            if snapshot.thread_id is None:
                return None
            thread = await self._thread(snapshot.thread_id, snapshot.channel_id)
            matches: list[int] = []
            total = 0
            async for message in thread.history(limit=self.MAX_HISTORY + 1):
                total += 1
                if (getattr(getattr(message, 'author', None), 'id', None) == self._bot_id()
                        and getattr(getattr(message, 'channel', None), 'id', thread.id) == thread.id
                        and has_feed_comment_identity(getattr(message, 'content', ''), snapshot.object_id)):
                    matches.append(int(message.id))
                    if len(matches) > 1:
                        return None
            if total > self.MAX_HISTORY:
                return None  # Bounded history cannot prove uniqueness.
            return (thread.id, matches[0]) if len(matches) == 1 else None

        forum = await self.bot.fetch_forum_channel(snapshot.channel_id)
        if getattr(forum, 'id', None) != snapshot.channel_id:
            raise PermissionError('unexpected Community forum')
        self._check_history_permissions(forum)
        active = list(getattr(forum, 'threads', ()) or ())
        if len(active) > self.MAX_FORUM_THREADS:
            return None
        # Discord forum threads can be archived before an HTTP timeout is
        # reconciled. Without archived visibility we cannot prove uniqueness.
        archived = []
        async for thread in forum.archived_threads(limit=self.MAX_FORUM_THREADS + 1):
            archived.append(thread)
            if len(archived) > self.MAX_FORUM_THREADS:
                return None
        matched: list[tuple[int, int]] = []
        for thread in {int(t.id): t for t in active + archived}.values():
            if getattr(thread, 'parent_id', None) != snapshot.channel_id:
                continue
            checked = await self._thread(int(thread.id), snapshot.channel_id)
            try:
                message = await checked.fetch_message(checked.id)
            except Exception as exc:
                # Missing or inaccessible candidates make the history scan
                # incomplete. A single observed match is not unique proof.
                raise PermissionError('Community forum history is incomplete') from exc
            if (getattr(getattr(message, 'author', None), 'id', None) == self._bot_id()
                    and str(getattr(message, 'content', '')).splitlines()[-1:] == [snapshot.object_id]):
                matched.append((int(thread.id), int(message.id)))
                if len(matched) > 1:
                    return None
        return matched[0] if len(matched) == 1 else None

    async def edit(self, snapshot: GroupClaimSnapshot) -> None:
        """Update only the original bot-authored Discord message by exact ID."""
        if snapshot.thread_id is None or snapshot.message_id is None:
            raise RuntimeError('missing proven Discord delivery for mutation')
        thread = await self._thread(snapshot.thread_id, snapshot.channel_id)
        message = await thread.fetch_message(snapshot.message_id)
        if getattr(getattr(message, 'author', None), 'id', None) != self._bot_id():
            raise PermissionError('Community message was not created by this bot')
        import discord
        await message.edit(content=_render(snapshot), allowed_mentions=discord.AllowedMentions.none())


class CommunityGroupViewDeliveryWorker:
    """One durable claim scheduler for unknown Creates and current revisions."""

    INTERVAL_SECONDS = 10.0
    RETRY_DELAY = timedelta(minutes=5)
    LEASE = timedelta(minutes=2)
    MAX_NETWORK_ACTIONS = 20

    def __init__(self, *, database: object, bot: object,
                 policy_service: object, transport: object | None = None) -> None:
        """Inject an outer Discord transport for controlled behavioral tests."""
        self.database = database
        self.bot = bot
        self.policy_service = policy_service
        self.transport = transport or CommunityDiscordViewTransport(bot)
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._stopping = False

    def start(self) -> None:
        """Start only after the bot's Discord-ready barrier was opened."""
        if self._task is not None:
            return
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name='community-group-view-delivery')

    def wake(self) -> None:
        """Notify the scheduler that a Group-view revision was just committed."""
        self._wake.set()

    async def stop(self) -> None:
        """Stop new attempts while retaining all durable claims and leases."""
        self._stopping = True
        self._wake.set()
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _run(self) -> None:
        """Wake promptly after ingress and periodically recover stale leases."""
        while not self._stopping:
            self._wake.clear()
            try:
                await self.drain_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception('Community Group-view worker drain failed')
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.INTERVAL_SECONDS)
            except TimeoutError:
                pass

    def _reserve_claim(self, claim_id: int) -> GroupClaimSnapshot | None:
        """Under BEGIN IMMEDIATE recheck rights and lease one due operation."""
        now = datetime.now(timezone.utc)
        with self.database.immediate_session() as session:
            claim = session.get(CommunityDiscordTargetClaim, claim_id)
            if claim is None or claim.status not in ('claimed', 'unknown', 'completed'):
                return None
            group = _claim_group(session, claim)
            if (group is None or not group.ap_object_id or not group.source_owner_actor_id
                    or not isinstance(group.display_object_json, dict)
                    or getattr(group, 'source_channel_id', None) is not None):
                return None
            if session.scalar(select(ChannelCommunitySubscription.id).where(
                ChannelCommunitySubscription.lemmy_community_actor_id == group.community_actor_id,
                ChannelCommunitySubscription.discord_channel_id == claim.discord_channel_id,
                ChannelCommunitySubscription.status == 'accepted',
            )) is None:
                return None
            if (not self.policy_service.federation_decision(group.source_owner_actor_id).allowed
                    or not self.policy_service.federation_decision(group.community_actor_id).allowed):
                return None
            if (claim.next_attempt_at is not None
                    and _as_utc(claim.next_attempt_at) > now):
                return None
            if claim.status == 'claimed':
                # An interrupted POST could have been accepted even if Python
                # died before receiving the response. Never infer no-send.
                if (_as_utc(claim.attempt_started_at) is None
                        or _as_utc(claim.attempt_started_at) > now - self.LEASE):
                    return None
                claim.status = 'unknown'
            if claim.status == 'unknown':
                if claim.kind == 'post' and session.scalar(select(CommunityDiscordTargetClaim.id).join(
                    CommunityThreadGroup,
                    CommunityDiscordTargetClaim.thread_group_id == CommunityThreadGroup.id,
                ).where(
                    CommunityDiscordTargetClaim.id != claim.id,
                    CommunityDiscordTargetClaim.discord_channel_id == claim.discord_channel_id,
                    CommunityDiscordTargetClaim.status.in_(('claimed', 'unknown', 'completed')),
                    CommunityThreadGroup.community_actor_id != group.community_actor_id,
                    CommunityThreadGroup.ap_object_id == group.ap_object_id,
                )) is not None:
                    # The visible footer identifies a Note, not its distributing
                    # Group. A second Group's post in the same forum is not
                    # proof that this claim's POST ever succeeded.
                    claim.last_error = 'same Note distributed by another Group in forum; manual review required'
                    claim.next_attempt_at = now + self.RETRY_DELAY
                    return None
                if claim.reconciliation_marker_version != 1:
                    claim.last_error = 'legacy Community Create has no exact identity marker; manual review required'
                    claim.next_attempt_at = now + self.RETRY_DELAY
                    return None
                operation = 'reconcile'
            else:
                if (claim.applied_revision is None or
                        claim.applied_revision >= group.view_revision):
                    return None
                operation = 'edit'
            delivery = _inbound_delivery(session, claim)
            if operation == 'edit' and delivery is None:
                # A completed claim without a proven bot-owned delivery has
                # no legal Discord mutation target; never guess an ID.
                claim.last_error = 'completed claim has no proven inbound delivery'
                claim.next_attempt_at = now + self.RETRY_DELAY
                return None
            content = group.display_object_json
            token = uuid4().hex
            claim.mutation_attempt_token = token
            claim.next_attempt_at = now + self.LEASE
            if claim.kind == 'comment':
                thread_id = delivery.discord_thread_id if delivery is not None else session.scalar(
                    select(CommunityThreadGroupDelivery.discord_thread_id).where(
                        CommunityThreadGroupDelivery.thread_group_id == group.thread_group_id,
                        CommunityThreadGroupDelivery.discord_channel_id == claim.discord_channel_id,
                        CommunityThreadGroupDelivery.role == 'inbound',
                    ))
            else:
                thread_id = delivery.discord_thread_id if delivery is not None else None
            message_id = None if delivery is None else (delivery.discord_starter_message_id
                if claim.kind == 'post' else delivery.discord_message_id)
            return GroupClaimSnapshot(
                claim_id=claim.id, kind=claim.kind,
                group_id=group.id, channel_id=claim.discord_channel_id,
                object_id=group.ap_object_id, actor_id=group.source_owner_actor_id,
                author_name=content.get('author_name') or 'remote',
                title=content.get('title'), body=content.get('body_markdown'),
                external_url=content.get('url'),
                view_state=group.view_state, view_revision=group.view_revision,
                applied_revision=claim.applied_revision,
                reserved_revision=claim.reserved_view_revision,
                token=token, operation=operation,
                thread_id=thread_id, message_id=message_id,
            )

    def _finish(self, snapshot: GroupClaimSnapshot, *, proven: tuple[int, int] | None,
                error: str | None) -> None:
        """CAS result onto the original claim, never overwriting newer views."""
        now = datetime.now(timezone.utc)
        with self.database.immediate_session() as session:
            claim = session.get(CommunityDiscordTargetClaim, snapshot.claim_id)
            if claim is None or claim.mutation_attempt_token != snapshot.token:
                return
            if snapshot.operation == 'reconcile' and claim.status == 'completed':
                # A late, original Discord POST callback proved and finished
                # the same target while history was running. Its claim wins.
                claim.mutation_attempt_token = None
                claim.next_attempt_at = None
                return
            group = _claim_group(session, claim)
            if group is None:
                return
            if error is not None or (snapshot.operation == 'reconcile' and proven is None):
                claim.last_error = (error or 'no unique proven Discord identity')[:1024]
                claim.next_attempt_at = now + self.RETRY_DELAY
                claim.mutation_attempt_token = None
                return
            if snapshot.operation == 'reconcile':
                if claim.status != 'unknown' or proven is None:
                    return
                thread_id, message_id = proven
                if claim.kind == 'post':
                    delivery = CommunityThreadGroupDelivery(
                        thread_group_id=group.id, discord_channel_id=claim.discord_channel_id,
                        discord_thread_id=thread_id, discord_starter_message_id=message_id,
                        role='inbound',
                    )
                else:
                    delivery = CommunityMessageGroupDelivery(
                        message_group_id=group.id, discord_channel_id=claim.discord_channel_id,
                        discord_thread_id=thread_id, discord_message_id=message_id,
                        role='inbound',
                    )
                if _inbound_delivery(session, claim) is not None:
                    return
                session.add(delivery)
                session.flush()
                claim.status = 'completed'
                claim.applied_revision = claim.reserved_view_revision
            else:
                if claim.status != 'completed':
                    return
                claim.applied_revision = max(claim.applied_revision or 0, snapshot.view_revision)
            claim.mutation_attempt_token = None
            claim.last_error = None
            claim.next_attempt_at = None if (claim.applied_revision is not None
                and claim.applied_revision >= group.view_revision) else now

    async def drain_once(self) -> int:
        """Make bounded network progress past blocked/unresolved/failing rows."""
        with self.database.session() as session:
            ids = list(session.scalars(select(CommunityDiscordTargetClaim.id).where(
                CommunityDiscordTargetClaim.status.in_(('claimed', 'unknown', 'completed')),
            ).order_by(CommunityDiscordTargetClaim.id)))
        attempted = 0
        for claim_id in ids:
            if attempted >= self.MAX_NETWORK_ACTIONS:
                break
            snapshot = self._reserve_claim(claim_id)
            if snapshot is None:
                continue
            attempted += 1
            try:
                if snapshot.operation == 'reconcile':
                    result = await self.transport.reconcile(snapshot)
                    self._finish(snapshot, proven=result, error=None)
                else:
                    await self.transport.edit(snapshot)
                    self._finish(snapshot, proven=None, error=None)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._finish(snapshot, proven=None, error=str(exc))
                logger.exception('Community Group view delivery failed for claim=%s', claim_id)
        return attempted
