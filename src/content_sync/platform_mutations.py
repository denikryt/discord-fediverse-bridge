"""Durable physical mutation orchestration over the existing four copy tables.

An inbound ActivityPub Update or Delete changes its owning canonical row
before any physical side effect. Copies from released v0 lack Create intents:
copy.applied_revision, not the Create ledger, determines mutation recovery.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from sqlalchemy import select, update

from ..models import (
    ChannelCommunitySubscription, ChannelSurface, CommunityMessageGroup,
    CommunityMessageGroupDelivery, CommunityThreadGroup,
    CommunityThreadGroupDelivery, LocalCommunity, LocalCommunityMessage,
    LocalCommunityMessageSurface, LocalCommunityThread, LocalCommunityThreadSurface,
    LocalSubscriber, PlatformConnector,
)
from ..platform_types import ContentDocument, MessageRef, MessageView
from .platform_delivery_scheduler import DispatchAdmissionClosed

logger = logging.getLogger(__name__)

# Only these existing records may own a physical mutation slot. No second
# table, queue or synthetic Create intent is allowed for inherited v0 copies.
_COPY_MODELS = (
    ('remote', 'post', CommunityThreadGroupDelivery, CommunityThreadGroup, 'thread_group_id'),
    ('remote', 'comment', CommunityMessageGroupDelivery, CommunityMessageGroup, 'message_group_id'),
    ('local', 'post', LocalCommunityThreadSurface, LocalCommunityThread, 'local_community_thread_id'),
    ('local', 'comment', LocalCommunityMessageSurface, LocalCommunityMessage, 'local_community_message_id'),
)


@dataclass(frozen=True, slots=True)
class PhysicalMutationClaim:
    """One exact copy, revision and immutable transport target claimed in SQLite."""

    domain: str
    kind: str
    copy_id: int
    token: str
    revision: int
    surface_id: int
    message: MessageRef
    deleted: bool
    content: ContentDocument | None


@dataclass(frozen=True, slots=True)
class PhysicalMutationCandidate:
    """Read-only snapshot; this object carries no durable dispatch authority."""

    domain: str
    kind: str
    copy_id: int
    connector_key: str
    revision: int
    applied_revision: int | None
    surface_id: int
    message: MessageRef
    deleted: bool
    content_json: str | None
    content: ContentDocument | None
    render_error: str | None = None


class PlatformPhysicalMutationCoordinator:
    """Claim, dispatch and CAS-settle edits/deletes through typed platform ports.

    The injected renderer is a pure domain boundary: shared coordination knows
    neither Discord message formatting nor provider SDK types. `target_policy`
    rechecks current connector-specific permission outside the SQLite lock.
    """

    def __init__(
        self, database, registry, *,
        target_policy: Callable[[str, int], bool],
        content_renderer: Callable[[str, str, dict], ContentDocument] | None = None,
        tombstone_renderer: Callable[[str, str], ContentDocument] | None = None,
        dispatch_gate=None,
    ) -> None:
        """Bind the durable database, connector registry and authorization edges."""
        self.database = database
        self.registry = registry
        self.target_policy = target_policy
        self.content_renderer = content_renderer
        self.tombstone_renderer = tombstone_renderer
        self.dispatch_gate = dispatch_gate
        # Fair, bounded read-only scanning across four existing copy families.
        # Per-connector/family bounded SQL pagination, not a private work queue.
        # The cursor advances after each 32-row read, even past settled rows.
        self._scan_cursor: dict[tuple[str | None, int], int] = {}
        self._family_rotation: dict[str | None, int] = {}

    @staticmethod
    def _current_owner(session, domain: str, canonical, surface: ChannelSurface, copy) -> bool:
        """Authorize the *present* surface owner, not a historical copy role."""
        if surface.kind != 'forum':
            return False
        if domain == 'remote':
            if copy.role == 'source':
                return False
            subscription = session.get(ChannelCommunitySubscription, surface.remote_subscription_id)
            return bool(
                subscription is not None and subscription.status == 'accepted'
                and subscription.lemmy_community_actor_id == canonical.community_actor_id
            )
        if isinstance(canonical, LocalCommunityThread):
            community_id = canonical.local_community_id
        else:
            owning_thread = session.get(LocalCommunityThread, canonical.local_community_thread_id)
            # A verified child tombstone must still reach existing copies after
            # its parent was deleted. Live content remains fenced by that parent.
            if owning_thread is None or (owning_thread.deleted_at is not None
                                         and canonical.deleted_at is None):
                return False
            community_id = owning_thread.local_community_id
        community = session.get(LocalCommunity, community_id)
        if community is None or community.status != 'active':
            return False
        if surface.local_community_id == community_id:
            return copy.role == 'host' and copy.local_subscriber_id is None
        if surface.local_subscriber_id is None:
            return False
        if copy.role != 'local_subscriber' or copy.local_subscriber_id != surface.local_subscriber_id:
            return False
        subscriber = session.get(LocalSubscriber, surface.local_subscriber_id)
        return bool(subscriber is not None and subscriber.local_community_id == community_id
                    and subscriber.status == 'active')

    @staticmethod
    def _physical_target(session, domain: str, kind: str, canonical, copy, connector_key: str) -> MessageRef | None:
        """Resolve a bot-owned physical MessageRef in its immediate container."""
        if kind == 'post':
            container = copy.external_thread_id
            message_id = copy.external_starter_message_id
            if (canonical.source_channel_surface_id == copy.channel_surface_id
                and canonical.source_external_thread_id == container):
                # A Discord-authored source is human-owned and cannot be edited
                # through a bridge-bot mirror API.
                return None
        elif domain == 'remote':
            container = copy.external_thread_id
            message_id = copy.external_message_id
            if (canonical.source_channel_surface_id == copy.channel_surface_id
                and canonical.source_external_container_id == container
                and canonical.source_external_message_id == message_id):
                return None
            parent = session.get(CommunityThreadGroup, canonical.thread_group_id)
            target_parent = session.scalar(select(CommunityThreadGroupDelivery).where(
                CommunityThreadGroupDelivery.thread_group_id == canonical.thread_group_id,
                CommunityThreadGroupDelivery.channel_surface_id == copy.channel_surface_id,
                CommunityThreadGroupDelivery.external_thread_id == container,
            ))
            if parent is None or target_parent is None:
                return None
        else:
            message_id = copy.external_message_id
            target_parent = session.get(LocalCommunityThreadSurface, copy.local_community_thread_surface_id)
            if (target_parent is None or target_parent.channel_surface_id != copy.channel_surface_id
                or target_parent.local_community_thread_id != canonical.local_community_thread_id):
                return None
            container = target_parent.external_thread_id
            if (canonical.source_channel_surface_id == copy.channel_surface_id
                and canonical.source_external_container_id == container
                and canonical.source_external_message_id == message_id):
                return None
        return MessageRef(connector_key, container, message_id)

    def _expire_abandoned_claims(self) -> None:
        """Fence abandoned *previously claimed* writes, not read-only candidates."""
        now = datetime.now(timezone.utc)
        with self.database.immediate_session() as session:
            for _domain, _kind, model, _canonical, _field in _COPY_MODELS:
                session.execute(update(model).where(
                    model.mutation_state == 'dispatching',
                    model.mutation_lease_until < now,
                ).values(mutation_state='unresolved',
                         mutation_last_error='expired mutation claim: outcome unknown'))

    def _discover_due(self, *, connector_key: str | None = None,
                      excluded: set[tuple[str, str, int]] | None = None
                      ) -> list[PhysicalMutationCandidate]:
        """Read at most one 32-row SQLite page per family in a single sweep.

        Cursors belong to this runtime/connector only and may reset on restart;
        they never authorize a write or bypass the durable unknown-effect fence.
        """
        excluded = excluded or set()
        due: list[PhysicalMutationCandidate] = []
        start_family = self._family_rotation.get(connector_key, 0)
        with self.database.session() as session:
            for offset in range(len(_COPY_MODELS)):
                family_index = (start_family + offset) % len(_COPY_MODELS)
                domain, kind, model, canonical_model, field = _COPY_MODELS[family_index]
                cursor_key = (connector_key, family_index)
                last_id = self._scan_cursor.get(cursor_key, 0)
                # Filter *before* LIMIT so 32 low-ID Discord copies cannot
                # starve a Fluxer lane (or vice versa) on every scheduler tick.
                # This remains a read-only query; no mutation is reserved yet.
                scope = (model.connector_id.in_(
                    select(PlatformConnector.id).where(
                        PlatformConnector.connector_key == connector_key
                    )) if connector_key is not None else True)
                query = select(model).where(
                    model.mutation_state.in_(('idle', 'pending', 'failed_definite')),
                    model.id > last_id,
                    scope,
                ).order_by(model.id).limit(32)
                copies = session.scalars(query).all()
                if not copies and last_id:
                    # Wrap on a later sweep; do not spin within one call.
                    last_id = 0
                    self._scan_cursor[cursor_key] = 0
                    copies = session.scalars(select(model).where(
                        model.mutation_state.in_(('idle', 'pending', 'failed_definite')),
                        scope,
                    ).order_by(model.id).limit(32)).all()
                if not copies:
                    continue
                for copy in copies:
                    if (domain, kind, copy.id) in excluded:
                        continue
                    canonical = session.get(canonical_model, getattr(copy, field))
                    surface = session.get(ChannelSurface, copy.channel_surface_id)
                    connector = session.get(PlatformConnector, copy.connector_id)
                    if (canonical is None or surface is None or connector is None
                        or (connector_key is not None and connector.connector_key != connector_key)
                        or surface.connector_id != copy.connector_id
                        or not self._current_owner(session, domain, canonical, surface, copy)):
                        continue
                    revision = canonical.content_revision
                    if revision == 0 and canonical.deleted_at is None:
                        continue
                    if copy.applied_revision is not None and copy.applied_revision >= revision:
                        continue
                    if (copy.mutation_state == 'failed_definite'
                        and copy.mutation_target_revision == revision):
                        # A known-unsent definitive refusal is durable. Only
                        # a new revision or explicit policy revalidation may
                        # readmit this same copy for physical writes.
                        continue
                    message = self._physical_target(session, domain, kind, canonical, copy,
                                                    connector.connector_key)
                    if message is None:
                        continue
                    content = None
                    render_error = None
                    if canonical.deleted_at is None:
                        if self.content_renderer is None or not canonical.current_content_json:
                            continue
                        try:
                            snapshot = json.loads(canonical.current_content_json)
                            if (not isinstance(snapshot, dict) or snapshot.get('schemaVersion') != 1
                                or snapshot.get('kind') != kind
                                or snapshot.get('apObjectId') != canonical.ap_object_id
                                or not isinstance(snapshot.get('bodyMarkdown'), str)):
                                raise ValueError('mismatched canonical content identity or shape')
                            content = self.content_renderer(domain, kind, snapshot)
                            if not isinstance(content, ContentDocument):
                                raise ValueError('domain mutation renderer must return ContentDocument')
                        except (TypeError, ValueError, KeyError) as exc:
                            # Rejected work is not a claim and cannot strand
                            # its siblings; scan later copies in this sweep.
                            render_error = 'invalid canonical content: ' + str(exc)
                    due.append(PhysicalMutationCandidate(
                        domain, kind, copy.id, connector.connector_key, revision,
                        copy.applied_revision, copy.channel_surface_id, message,
                        canonical.deleted_at is not None,
                        canonical.current_content_json, content, render_error,
                    ))
                    if len(due) >= 32:
                        self._scan_cursor[cursor_key] = copy.id
                        self._family_rotation[connector_key] = (family_index + 1) % len(_COPY_MODELS)
                        return due
                # The next tick continues beyond this page; later families
                # still get a bounded opportunity within the current tick.
                self._scan_cursor[cursor_key] = copies[-1].id
        self._family_rotation[connector_key] = (start_family + 1) % len(_COPY_MODELS)
        return due

    def _claim_one(self, candidate: PhysicalMutationCandidate) -> PhysicalMutationClaim | None:
        """CAS-reserve one already-vetted candidate immediately before I/O.

        The full physical target, canonical content/revision and copy's applied
        revision must still equal the read-only snapshot under the writer lock.
        """
        descriptor = next(spec for spec in _COPY_MODELS
                          if spec[0] == candidate.domain and spec[1] == candidate.kind)
        _domain, _kind, model, canonical_model, field = descriptor
        with self.database.immediate_session() as session:
            copy = session.get(model, candidate.copy_id)
            if (copy is None or copy.mutation_state not in ('idle', 'pending', 'failed_definite')
                or copy.applied_revision != candidate.applied_revision
                or copy.channel_surface_id != candidate.surface_id):
                return None
            canonical = session.get(canonical_model, getattr(copy, field))
            surface = session.get(ChannelSurface, copy.channel_surface_id)
            connector = session.get(PlatformConnector, copy.connector_id)
            if (canonical is None or surface is None or connector is None
                or connector.connector_key != candidate.connector_key
                or surface.connector_id != copy.connector_id
                or canonical.content_revision != candidate.revision
                or canonical.current_content_json != candidate.content_json
                or (canonical.deleted_at is not None) != candidate.deleted
                or not self._current_owner(session, candidate.domain, canonical, surface, copy)
                or self._physical_target(session, candidate.domain, candidate.kind,
                                         canonical, copy, connector.connector_key) != candidate.message):
                return None
            if copy.applied_revision is not None and copy.applied_revision >= candidate.revision:
                return None
            if (copy.mutation_state == 'failed_definite'
                and copy.mutation_target_revision == candidate.revision):
                return None
            token = secrets.token_hex(16)
            copy.mutation_state = 'dispatching'
            copy.mutation_target_revision = candidate.revision
            copy.mutation_claim_token = token
            copy.mutation_lease_until = datetime.now(timezone.utc) + timedelta(seconds=120)
            copy.mutation_last_error = None
            return PhysicalMutationClaim(
                candidate.domain, candidate.kind, copy.id, token, candidate.revision,
                candidate.surface_id, candidate.message, candidate.deleted, candidate.content,
            )

    def _record_preflight_refusal(self, candidate: PhysicalMutationCandidate, reason: str) -> None:
        """Record a known-unsent refusal only if the original revision is current."""
        descriptor = next(spec for spec in _COPY_MODELS
                          if spec[0] == candidate.domain and spec[1] == candidate.kind)
        model, canonical_model, field = descriptor[2:]
        with self.database.immediate_session() as session:
            copy = session.get(model, candidate.copy_id)
            if copy is None or copy.mutation_state not in ('idle', 'pending', 'failed_definite'):
                return
            canonical = session.get(canonical_model, getattr(copy, field))
            if (canonical is None or canonical.content_revision != candidate.revision
                or copy.applied_revision != candidate.applied_revision):
                return
            copy.mutation_state = 'failed_definite'
            copy.mutation_target_revision = candidate.revision
            copy.mutation_claim_token = None
            copy.mutation_lease_until = None
            copy.mutation_last_error = reason[:512]

    def readmit_failed_revision(self, *, domain: str, kind: str, copy_id: int,
                                connector_key: str, revision: int) -> bool:
        """Readmit an exact known-unsent refusal after explicit policy review.

        This is an operator/policy revalidation boundary, not a scheduler retry.
        A new canonical revision is independently eligible without this call.
        """
        if not any((dom, typ) == (domain, kind) for dom, typ, *_ in _COPY_MODELS):
            return False
        descriptor = next(spec for spec in _COPY_MODELS
                          if spec[0] == domain and spec[1] == kind)
        _, _, model, canonical_model, field = descriptor
        with self.database.session() as session:
            copy = session.get(model, copy_id)
            if copy is None:
                return False
            surface_id = copy.channel_surface_id
        # Operator review must authorize the current policy *outside* the
        # SQLite writer; the writer then checks exact identity and revision.
        if not self.target_policy(connector_key, surface_id):
            return False
        with self.database.immediate_session() as session:
            copy = session.get(model, copy_id)
            if (copy is None or copy.channel_surface_id != surface_id
                or copy.mutation_state != 'failed_definite'
                or copy.mutation_target_revision != revision
                or copy.mutation_claim_token is not None):
                return False
            canonical = session.get(canonical_model, getattr(copy, field))
            surface = session.get(ChannelSurface, surface_id)
            connector = session.get(PlatformConnector, copy.connector_id)
            if (canonical is None or surface is None or connector is None
                or connector.connector_key != connector_key
                or surface.connector_id != connector.id
                or canonical.content_revision != revision
                or not self._current_owner(session, domain, canonical, surface, copy)):
                return False
            copy.mutation_state = 'pending'
            copy.mutation_target_revision = None
            copy.mutation_last_error = None
            return True

    def _revalidate(self, claim: PhysicalMutationClaim) -> bool:
        """Check exact attempt, copy owner and target before transport begins."""
        descriptor = next(spec for spec in _COPY_MODELS
                          if spec[0] == claim.domain and spec[1] == claim.kind)
        _, _, model, canonical_model, canonical_field = descriptor
        with self.database.immediate_session() as session:
            copy = session.get(model, claim.copy_id)
            if (copy is None or copy.mutation_state != 'dispatching'
                or copy.mutation_claim_token != claim.token
                or copy.mutation_target_revision != claim.revision):
                return False
            canonical = session.get(canonical_model, getattr(copy, canonical_field))
            surface = session.get(ChannelSurface, copy.channel_surface_id)
            if canonical is None or surface is None or not self._current_owner(session, claim.domain, canonical, surface, copy):
                return False
            connector = session.get(PlatformConnector, copy.connector_id)
            return bool(canonical.content_revision == claim.revision
                        and connector is not None and connector.connector_key == claim.message.connector_key
                        and self._physical_target(session, claim.domain, claim.kind, canonical, copy, connector.connector_key) == claim.message
                        and canonical.content_revision >= claim.revision)

    def _settle(self, claim: PhysicalMutationClaim, *, delivered: bool,
                error: str | None = None, definitely_unsent: bool = False) -> bool:
        """CAS-ack only proven completion or non-send; unknown effects stay blocked."""
        descriptor = next(spec for spec in _COPY_MODELS
                          if spec[0] == claim.domain and spec[1] == claim.kind)
        model = descriptor[2]
        with self.database.immediate_session() as session:
            copy = session.get(model, claim.copy_id)
            if (copy is None or copy.mutation_state != 'dispatching'
                or copy.mutation_claim_token != claim.token
                or copy.mutation_target_revision != claim.revision):
                return False
            if delivered:
                copy.applied_revision = claim.revision
                copy.mutation_state = 'idle'
                copy.mutation_target_revision = None
                copy.mutation_last_error = None
            else:
                # The same exclusive worker can prove it refused before any
                # adapter call; errors after crossing that boundary remain
                # unresolved even if the provider reported an HTTP failure.
                copy.mutation_state = ('failed_definite' if definitely_unsent
                                       else 'unresolved')
                copy.mutation_last_error = str(error or 'unknown physical effect')[:512]
            copy.mutation_claim_token = None
            copy.mutation_lease_until = None
            return True

    async def drain(self, *, limit: int = 32, connector_key: str | None = None) -> int:
        """Use the shared per-connector slot for both immediate and recovery work.

        A direct drain without a connector is restricted to configured runtime
        connectors, not admitted through a separate global physical sender.
        """
        if self.dispatch_gate is None:
            return await self._drain_admitted(limit=limit, connector_key=connector_key)
        if limit <= 0:
            return 0
        keys = ((connector_key,) if connector_key is not None
                else self.dispatch_gate.connector_keys)
        completed = 0
        for key in keys:
            if completed >= limit:
                break
            try:
                async with self.dispatch_gate.slot(key):
                    completed += await self._drain_admitted(
                        limit=limit-completed, connector_key=key,
                    )
            except (LookupError, DispatchAdmissionClosed):
                # Quiescing/unconfigured connectors cannot obtain dispatch
                # authority. Leave their SQLite candidates untouched.
                continue
        return completed

    async def _drain_admitted(self, *, limit: int = 32,
                              connector_key: str | None = None) -> int:
        """Preflight without claims, then claim just one copy per available slot.

        Per-sweep attempted identities prevent a low-ID refusal from starving a
        higher-ID copy. A caller's connector lane supplies exclusive capacity;
        the durable claim itself remains owned by the existing SQLite rows.
        """
        if limit <= 0:
            return 0
        self._expire_abandoned_claims()
        attempted: set[tuple[str, str, int]] = set()
        claimed = 0
        for candidate in self._discover_due(connector_key=connector_key):
            if claimed >= limit:
                break
            key = (candidate.domain, candidate.kind, candidate.copy_id)
            if key in attempted:
                continue
            attempted.add(key)
            if candidate.render_error is not None:
                self._record_preflight_refusal(candidate, candidate.render_error)
                continue
            # All lookups below may await or fail. None grants dispatch authority.
            # They happen BEFORE any durable mutation reservation.
            try:
                adapter = self.registry.get(candidate.connector_key)
                ready = (getattr(adapter, 'dispatch_ready', None)
                         or getattr(adapter, 'is_ready', None))
                if ready is not None and not ready():
                    continue
                if not self.target_policy(candidate.connector_key, candidate.surface_id):
                    self._record_preflight_refusal(candidate, 'revoked before transport')
                    continue
                view = await adapter.fetch_message(candidate.message)
                if (not isinstance(view, MessageView) or view.message != candidate.message
                    or view.author is None
                    or view.author.connector_key != candidate.message.connector_key
                    or not view.author.is_bridge_owned):
                    self._record_preflight_refusal(candidate,
                                                   'unverified physical ownership: operator review required')
                    continue
                tombstone = None
                if candidate.deleted and self.tombstone_renderer is not None:
                    tombstone = self.tombstone_renderer(candidate.domain, candidate.kind)
                    if not isinstance(tombstone, ContentDocument):
                        raise ValueError('domain tombstone renderer must return ContentDocument')
                # A concurrent policy change before the claim is still safe.
                if not self.target_policy(candidate.connector_key, candidate.surface_id):
                    self._record_preflight_refusal(candidate, 'revoked during preflight')
                    continue
            except asyncio.CancelledError:
                raise
            except (LookupError, ConnectionError, TimeoutError) as exc:
                # A transient pre-write read failure changes no durable claim
                # or mutation state. The scheduler may retry on a later tick.
                logger.warning('Physical mutation read unavailable copy=%s: %s',
                               candidate.copy_id, exc)
                continue
            except Exception as exc:
                # No claim yet. Retain a diagnosable, provably-unsent state.
                self._record_preflight_refusal(candidate, str(exc))
                logger.warning('Physical mutation preflight refused copy=%s: %s',
                               candidate.copy_id, exc)
                continue
            claim = self._claim_one(candidate)
            if claim is None:
                continue
            claimed += 1
            # Synchronous final gate before *method entry*. After entering an
            # adapter write method, errors/cancellation can imply remote effect.
            io_started = False
            try:
                if not self._revalidate(claim) or not self.target_policy(
                    claim.message.connector_key, claim.surface_id
                ):
                    self._settle(claim, delivered=False, definitely_unsent=True,
                                 error='revoked before transport')
                    continue
                if claim.deleted:
                    if tombstone is None:
                        io_started = True
                        await adapter.delete_message(claim.message)
                    else:
                        io_started = True
                        await adapter.edit_message(claim.message, tombstone)
                else:
                    assert claim.content is not None
                    io_started = True
                    await adapter.edit_message(claim.message, claim.content)
                self._settle(claim, delivered=True)
            except asyncio.CancelledError:
                if not io_started:
                    self._settle(claim, delivered=False, definitely_unsent=True,
                                 error='worker cancelled before physical write')
                raise
            except Exception as exc:
                self._settle(claim, delivered=False, error=str(exc),
                             definitely_unsent=not io_started)
                logger.warning('Physical mutation %s copy=%s/%s/%s: %s',
                               'unresolved' if io_started else 'failed before send',
                               claim.domain, claim.kind, claim.copy_id, exc)
        return claimed
