"""Durable per-channel Discord root delivery through bot-owned Incoming webhooks.

The SQLite claim transition to ``sending`` is the last authorization point.
Network I/O occurs outside transactions; an ambiguous webhook POST remains
``uncertain`` until history proves a matching root, never blindly duplicated.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import or_, select

from .models import RemotePersonPost, RemotePersonPostDelivery, utcnow
from .remote_person_feeds import RemotePersonChannelService
from .remote_person_webhooks import RemotePersonWebhookTransport, remote_root_content

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RootSnapshot:
    """Database-committed post and one Discord destination identity."""

    delivery_id: int
    post_id: int
    channel_id: int
    guild_id: int
    state: str
    remote_actor_id: str
    remote_handle: str | None
    remote_avatar_url: str | None
    content_markdown: str
    source_activity_json: str
    object_id: str
    revision_fingerprint: str
    last_applied_revision: str | None
    deleted_at: datetime | None
    discord_root_message_id: int | None
    discord_thread_id: int | None
    webhook_id: int | None
    claim_token: str | None
    attempt_count: int


def _snapshot(row: RemotePersonPostDelivery, post: RemotePersonPost) -> RootSnapshot:
    """Detach a stable ORM read before any asynchronous Discord operation."""
    return RootSnapshot(
        delivery_id=row.id, post_id=post.id, channel_id=row.discord_channel_id,
        guild_id=row.discord_guild_id, state=row.state,
        remote_actor_id=post.remote_actor_id, remote_handle=post.remote_handle,
        remote_avatar_url=post.remote_avatar_url, content_markdown=post.content_markdown,
        source_activity_json=post.source_activity_json, object_id=post.object_id,
        revision_fingerprint=post.revision_fingerprint, deleted_at=post.deleted_at,
        last_applied_revision=row.last_applied_revision,
        discord_root_message_id=row.discord_root_message_id,
        discord_thread_id=row.discord_thread_id,
        webhook_id=row.webhook_id, claim_token=row.claim_token,
        attempt_count=row.attempt_count,
    )


class RemotePersonRootDeliveryWorker:
    """Own new-create claims, root reconciliation, threads and retained mutations."""

    INTERVAL_SECONDS = 8.0
    MAX_ATTEMPTS = 3

    def __init__(self, *, database: object, bot: object, policy_service: object,
                 transport: object | None = None) -> None:
        """Inject only the Discord network edge for deterministic offline tests."""
        self.database = database
        self.bot = bot
        self.policy_service = policy_service
        self.transport = transport or RemotePersonWebhookTransport(bot)
        self._task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self._stopping = False
        self.comment_worker: object | None = None

    def bind_comment_worker(self, worker: object) -> None:
        """Run comment copies after root/thread work under the same scheduler."""
        self.comment_worker = worker

    def start(self) -> None:
        """Recover interrupted attempts and start one ready-gated scheduler."""
        if self._task is not None:
            return
        with self.database.immediate_session() as session:
            for row in session.scalars(select(RemotePersonPostDelivery).where(
                RemotePersonPostDelivery.state.in_(('claimed', 'sending')),
            )):
                # A prepared claim never entered network I/O. A sent claim
                # cannot be retried without checking Discord message history.
                row.state = 'pending' if row.state == 'claimed' else 'uncertain'
                row.claim_token = None
                row.last_error = 'recovered interrupted root dispatch'
        self._stopping = False
        if self.comment_worker is not None:
            self.comment_worker.recover()
        self._task = asyncio.create_task(self._run(), name='remote-person-root-delivery')

    def wake(self) -> None:
        """Request a prompt drain after inbound canonical persistence."""
        self._wake.set()

    async def stop(self) -> None:
        """Cancel network work while preserving SQLite restart/reconcile state."""
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
        # Any claimed work is safe to retry; sending must be reconciled by
        # Discord history before a possible second webhook POST.
        with self.database.immediate_session() as session:
            for row in session.scalars(select(RemotePersonPostDelivery).where(
                RemotePersonPostDelivery.state.in_(('claimed', 'sending')),
            )):
                row.state = 'pending' if row.state == 'claimed' else 'uncertain'
                row.claim_token = None

    async def _run(self) -> None:
        """Process destination failures independently without blocking others."""
        while not self._stopping:
            self._wake.clear()
            try:
                await self.drain_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception('Remote Person root delivery pass failed')
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.INTERVAL_SECONDS)
            except TimeoutError:
                pass

    def _policy_ok(self, guild_id: int, actor_id: str) -> bool:
        """Recheck current guild and federation policy at the send boundary."""
        snapshot = self.policy_service.snapshot()
        return snapshot.is_discord_guild_allowed(guild_id) and snapshot.federation_decision(actor_id).allowed

    def _accepted(self, session: object, root: RootSnapshot) -> bool:
        """Require a current accepted Follow assignment for each new create."""
        return RemotePersonChannelService._active_owner_exists(
            session, root.remote_actor_id, root.guild_id, root.channel_id,
        )

    def _claim_pending(self) -> RootSnapshot | None:
        """Select one due create or suppress it when its author lost this channel."""
        with self.database.immediate_session() as session:
            row = session.scalar(select(RemotePersonPostDelivery).where(
                RemotePersonPostDelivery.state == 'pending',
                or_(RemotePersonPostDelivery.next_attempt_at.is_(None),
                    RemotePersonPostDelivery.next_attempt_at <= utcnow()),
            ).order_by(RemotePersonPostDelivery.id).limit(1))
            if row is None:
                return None
            post = session.get(RemotePersonPost, row.post_id)
            if post is None:
                raise RuntimeError('root queue references a missing canonical Note')
            draft = _snapshot(row, post)
            if post.deleted_at is not None or not self._policy_ok(draft.guild_id, draft.remote_actor_id) or not self._accepted(session, draft):
                row.state = 'suppressed'
                row.claim_token = None
                row.last_error = 'root author/channel no longer eligible before claim'
                return replace(draft, state='suppressed')
            row.state = 'claimed'
            row.claim_token = uuid4().hex
            row.attempt_count += 1
            row.next_attempt_at = None
            return _snapshot(row, post)

    def _authorize_sending(self, claim: RootSnapshot, webhook_id: int) -> bool:
        """Atomically linearize dispatch after all local gates and webhook lookup."""
        with self.database.immediate_session() as session:
            row = session.get(RemotePersonPostDelivery, claim.delivery_id)
            if row is None or row.state != 'claimed' or row.claim_token != claim.claim_token:
                return False
            post = session.get(RemotePersonPost, row.post_id)
            if (post is None or post.deleted_at is not None
                    or not self._policy_ok(claim.guild_id, claim.remote_actor_id)
                    or not self._accepted(session, claim)):
                row.state = 'suppressed'
                row.claim_token = None
                row.last_error = 'assignment removed before authorized HTTP send'
                return False
            row.webhook_id = webhook_id
            # Pin the revision actually submitted. A newer Update can arrive
            # before an uncertain POST is reconciled from Discord history.
            row.last_applied_revision = claim.revision_fingerprint
            row.state = 'sending'
            return True

    def _local_failure(self, claim: RootSnapshot, detail: str) -> None:
        """Retry only effects proven not to have submitted a webhook POST."""
        with self.database.immediate_session() as session:
            row = session.get(RemotePersonPostDelivery, claim.delivery_id)
            if row is None or row.claim_token != claim.claim_token or row.state != 'claimed':
                return
            row.state = 'failed' if row.attempt_count >= self.MAX_ATTEMPTS else 'pending'
            row.claim_token = None
            row.last_error = detail[:500]
            if row.state == 'pending':
                row.next_attempt_at = utcnow() + timedelta(seconds=10 * row.attempt_count)

    def _render_failure(self, claim: RootSnapshot, detail: str) -> None:
        """Fail a deterministic unrenderable root without claiming an HTTP POST."""
        with self.database.immediate_session() as session:
            row = session.get(RemotePersonPostDelivery, claim.delivery_id)
            if row is not None and row.state == 'claimed' and row.claim_token == claim.claim_token:
                # No webhook request has happened; reconciliation would never
                # find a message and must not quarantine this row as uncertain.
                row.state = 'failed'
                row.claim_token = None
                row.last_applied_revision = None
                row.next_attempt_at = None
                row.last_error = detail[:500]

    def _ambiguous_failure(self, claim: RootSnapshot, detail: str) -> None:
        """Never retry a network-attempted webhook POST without reconciliation."""
        with self.database.immediate_session() as session:
            row = session.get(RemotePersonPostDelivery, claim.delivery_id)
            if row is not None and row.claim_token == claim.claim_token and row.state == 'sending':
                row.state = 'uncertain'
                row.claim_token = None
                row.last_error = detail[:500]

    def _mark_root(self, delivery_id: int, *, message_id: int, webhook_id: int,
                   revision: str) -> None:
        """Commit the original webhook/message identity before trying a thread."""
        with self.database.immediate_session() as session:
            row = session.get(RemotePersonPostDelivery, delivery_id)
            if row is None or row.state not in ('sending', 'uncertain'):
                return
            row.discord_root_message_id = message_id
            row.webhook_id = webhook_id
            row.state = 'delivered'
            row.claim_token = None
            row.last_applied_revision = revision
            row.last_error = None
            row.next_attempt_at = None

    def _set_thread(self, delivery_id: int, thread_id: int) -> None:
        """Persist thread identity independently from successful root delivery."""
        with self.database.immediate_session() as session:
            row = session.get(RemotePersonPostDelivery, delivery_id)
            if row is not None and row.state == 'delivered' and row.discord_root_message_id == thread_id:
                row.discord_thread_id = thread_id
                row.last_error = None
                row.next_attempt_at = None

    def _thread_error(self, delivery_id: int, message: str) -> None:
        """Keep the delivered root while bounding thread creation failures."""
        with self.database.immediate_session() as session:
            row = session.get(RemotePersonPostDelivery, delivery_id)
            if row is not None and row.state == 'delivered' and row.discord_thread_id is None:
                row.attempt_count = min(self.MAX_ATTEMPTS, row.attempt_count + 1)
                row.next_attempt_at = (utcnow() + timedelta(seconds=30 * row.attempt_count)
                                       if row.attempt_count < self.MAX_ATTEMPTS else None)
                row.last_error = ('thread creation failed: ' + message)[:500]

    async def _ensure_thread(self, root: RootSnapshot) -> None:
        """Create the associated bot-owned public thread without re-posting root."""
        if root.discord_root_message_id is None or root.discord_thread_id is not None:
            return
        try:
            thread_id = await self.transport.create_thread(root.channel_id, root.discord_root_message_id, root)
            self._set_thread(root.delivery_id, int(thread_id))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # The webhook message is already committed. A failed boundary test
            # cannot justify another root or a switch to bot-authored posts.
            logger.warning('Remote Person thread failed delivery=%s', root.delivery_id)
            self._thread_error(root.delivery_id, str(exc))

    async def _send_new(self, claim: RootSnapshot) -> None:
        """Commit guarded webhook ID then perform one and only one POST."""
        if claim.state == 'suppressed':
            return
        try:
            # Validate the immutable author/identity budget before network
            # authorization so a local ValueError can never mean uncertain.
            remote_root_content(claim)
        except ValueError as exc:
            self._render_failure(claim, str(exc))
            return
        try:
            prepared = await self.transport.prepare(claim.channel_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._local_failure(claim, str(exc))
            return
        if not self._authorize_sending(claim, prepared.webhook_id):
            return
        try:
            message_id = await self.transport.create(prepared, claim)
        except asyncio.CancelledError:
            self._ambiguous_failure(claim, 'cancelled after webhook send authorization')
            raise
        except Exception as exc:
            self._ambiguous_failure(claim, str(exc))
            return
        self._mark_root(claim.delivery_id, message_id=message_id,
                        webhook_id=prepared.webhook_id, revision=claim.revision_fingerprint)
        await self._ensure_thread(replace(claim, state='delivered', webhook_id=prepared.webhook_id, discord_root_message_id=message_id))

    def _retained(self, *, kind: str, limit: int = 20) -> list[RootSnapshot]:
        """Select delivered roots for thread repair or authoritative mutations."""
        with self.database.session() as session:
            # Errors in a retained Edit/Delete must not monopolize the first
            # 20 slots. Threads keep their existing id-ordered repair scan.
            ordering = ((RemotePersonPostDelivery.updated_at, RemotePersonPostDelivery.id)
                        if kind == 'mutation' else (RemotePersonPostDelivery.id,))
            rows = session.scalars(select(RemotePersonPostDelivery).where(
                RemotePersonPostDelivery.state == 'delivered',
            ).order_by(*ordering)).all()
            selected = []
            now = utcnow()
            for row in rows:
                post = session.get(RemotePersonPost, row.post_id)
                if post is None:
                    continue
                if kind == 'thread':
                    due = (row.discord_thread_id is None and row.attempt_count < self.MAX_ATTEMPTS
                           and (row.next_attempt_at is None or (row.next_attempt_at.replace(tzinfo=timezone.utc) if row.next_attempt_at.tzinfo is None else row.next_attempt_at) <= now))
                else:
                    due = ((post.deleted_at is not None and row.last_applied_revision != 'deleted')
                           or (post.deleted_at is None and row.last_applied_revision != post.revision_fingerprint))
                if due:
                    selected.append(_snapshot(row, post))
                if len(selected) >= limit:
                    break
            return selected

    async def _reconcile(self, root: RootSnapshot) -> None:
        """Prove a possibly successful webhook POST through original hook history."""
        if root.webhook_id is None:
            # This can only be an obsolete/corrupt uncertain row; never repost.
            return
        try:
            found = await self.transport.find_created_root(root.channel_id, root.webhook_id, root.object_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning('Remote Person root reconciliation failed delivery=%s', root.delivery_id)
            self._defer_uncertain(root.delivery_id)
            return
        if found is not None:
            self._mark_root(root.delivery_id, message_id=found,
                            webhook_id=root.webhook_id,
                            revision=root.last_applied_revision or '')
            await self._ensure_thread(replace(root, state='delivered', discord_root_message_id=found))
        else:
            # A missing history match is not evidence of a failed webhook
            # POST. Defer this row so later uncertain targets are not starved.
            self._defer_uncertain(root.delivery_id)

    def _defer_uncertain(self, delivery_id: int) -> None:
        """Move an unresolved row to the end of the fairness-ordered scan."""
        with self.database.immediate_session() as session:
            row = session.get(RemotePersonPostDelivery, delivery_id)
            if row is not None and row.state == 'uncertain':
                # The reconciliation read is safe to repeat; unlike POST it
                # cannot duplicate a root. Advance the row's sort position so
                # 21st and later channels also get inspected on the next pass.
                row.updated_at = utcnow()

    def _mark_mutation(self, root: RootSnapshot, applied: str) -> None:
        """Persist revision only after original webhook confirms edit or delete."""
        with self.database.immediate_session() as session:
            row = session.get(RemotePersonPostDelivery, root.delivery_id)
            if row is not None and row.state == 'delivered':
                row.last_applied_revision = applied
                row.last_error = None

    def _mutation_error(self, root: RootSnapshot, exc: Exception) -> None:
        """Keep the original placement for later authorized reconciliation."""
        with self.database.immediate_session() as session:
            row = session.get(RemotePersonPostDelivery, root.delivery_id)
            if row is not None and row.state == 'delivered':
                row.last_error = ('original webhook mutation failed: ' + str(exc))[:500]
                row.updated_at = utcnow()  # Leave other due webhook copies a turn.

    async def _mutate(self, root: RootSnapshot) -> None:
        """Edit/delete retained roots even when the last Follow was removed."""
        if root.webhook_id is None or root.discord_root_message_id is None:
            return
        try:
            prepared = await self.transport.prepare(root.channel_id, original_webhook_id=root.webhook_id)
            if root.deleted_at is not None:
                await self.transport.delete(prepared, root.discord_root_message_id)
                self._mark_mutation(root, 'deleted')
            else:
                await self.transport.edit(prepared, root.discord_root_message_id, root)
                self._mark_mutation(root, root.revision_fingerprint)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if getattr(exc, 'code', None) == 10008:
                # Discord has positively identified an absent root message:
                # deleting it is idempotent; editing it cannot succeed.
                if root.deleted_at is not None:
                    self._mark_mutation(root, 'deleted')
                else:
                    with self.database.immediate_session() as session:
                        row = session.get(RemotePersonPostDelivery, root.delivery_id)
                        if row is not None and row.state == 'delivered':
                            row.state = 'failed'
                            row.last_error = 'original Discord root was removed'
                return
            logger.warning('Remote Person retained root mutation failed delivery=%s', root.delivery_id)
            self._mutation_error(root, exc)

    async def drain_once(self) -> None:
        """Drain bounded work without allowing one destination to block another."""
        for _ in range(20):
            root = self._claim_pending()
            if root is None:
                break
            await self._send_new(root)
        with self.database.session() as session:
            rows = session.scalars(select(RemotePersonPostDelivery).where(
                RemotePersonPostDelivery.state == 'uncertain',
            ).order_by(RemotePersonPostDelivery.updated_at,
                       RemotePersonPostDelivery.id).limit(20)).all()
            uncertain = [_snapshot(row, session.get(RemotePersonPost, row.post_id)) for row in rows]
        for root in uncertain:
            await self._reconcile(root)
        for root in self._retained(kind='thread'):
            await self._ensure_thread(root)
        for root in self._retained(kind='mutation'):
            await self._mutate(root)
        if self.comment_worker is not None:
            await self.comment_worker.drain_once()
