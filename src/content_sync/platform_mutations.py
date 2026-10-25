"""Durable physical mutation orchestration over the existing four copy tables.

An inbound ActivityPub Update or Delete changes its owning canonical row
before any physical side effect. Copies from released v0 lack Create intents:
copy.applied_revision, not the Create ledger, determines mutation recovery.
"""

from __future__ import annotations

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
from ..platform_types import ContentDocument, MessageRef

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
    ) -> None:
        """Bind the durable database, connector registry and authorization edges."""
        self.database = database
        self.registry = registry
        self.target_policy = target_policy
        self.content_renderer = content_renderer
        self.tombstone_renderer = tombstone_renderer

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
            if owning_thread is None or owning_thread.deleted_at is not None:
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

    def _claim_due(self, limit: int) -> list[PhysicalMutationClaim]:
        """Exclusively reserve settled, lagging physical copies in one writer."""
        now = datetime.now(timezone.utc)
        claimed: list[PhysicalMutationClaim] = []
        with self.database.immediate_session() as session:
            for domain, kind, copy_model, canonical_model, canonical_field in _COPY_MODELS:
                # An expired dispatch could have reached the provider: never
                # reclaim it as if it were a provably unsent retry.
                session.execute(update(copy_model).where(
                    copy_model.mutation_state == 'dispatching',
                    copy_model.mutation_lease_until < now,
                ).values(mutation_state='unresolved',
                         mutation_last_error='expired mutation claim: outcome unknown'))
                copies = session.scalars(select(copy_model).where(
                    copy_model.mutation_state.in_(('idle', 'pending', 'failed_definite'))
                ).order_by(copy_model.id).limit(limit * 8)).all()
                for copy in copies:
                    canonical = session.get(canonical_model, getattr(copy, canonical_field))
                    surface = session.get(ChannelSurface, copy.channel_surface_id)
                    connector = session.get(PlatformConnector, copy.connector_id)
                    if (canonical is None or surface is None or connector is None
                        or surface.connector_id != copy.connector_id
                        or not self._current_owner(session, domain, canonical, surface, copy)):
                        continue
                    revision = canonical.content_revision
                    if revision == 0 and canonical.deleted_at is None:
                        # Released-v0 history does not prove which old body was
                        # applied. Wait for a verified new content revision.
                        continue
                    if copy.applied_revision is not None and copy.applied_revision >= revision:
                        continue
                    message = self._physical_target(session, domain, kind, canonical, copy, connector.connector_key)
                    if message is None:
                        continue
                    content = None
                    if canonical.deleted_at is None:
                        if self.content_renderer is None or not canonical.current_content_json:
                            continue
                        try:
                            snapshot = json.loads(canonical.current_content_json)
                        except (TypeError, ValueError):
                            continue
                        if (not isinstance(snapshot, dict) or snapshot.get('schemaVersion') != 1
                            or snapshot.get('kind') != kind
                            or snapshot.get('apObjectId') != canonical.ap_object_id
                            or not isinstance(snapshot.get('bodyMarkdown'), str)):
                            continue
                        content = self.content_renderer(domain, kind, snapshot)
                        if not isinstance(content, ContentDocument):
                            raise ValueError('domain mutation renderer must return ContentDocument')
                    copy.mutation_state = 'dispatching'
                    copy.mutation_target_revision = revision
                    copy.mutation_claim_token = secrets.token_hex(16)
                    copy.mutation_lease_until = now + timedelta(seconds=120)
                    copy.mutation_last_error = None
                    claimed.append(PhysicalMutationClaim(
                        domain, kind, copy.id, copy.mutation_claim_token,
                        revision, copy.channel_surface_id, message,
                        canonical.deleted_at is not None, content,
                    ))
                    if len(claimed) >= limit:
                        return claimed
        return claimed

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

    async def drain(self, *, limit: int = 32) -> int:
        """Send only newly claimed updates/deletes; never retry unknown writes."""
        claims = self._claim_due(limit)
        for claim in claims:
            try:
                if not self._revalidate(claim) or not self.target_policy(
                    claim.message.connector_key, claim.surface_id
                ):
                    # This worker refused before calling the adapter, so
                    # it can safely classify this *generation* as unsent.
                    self._settle(claim, delivered=False,
                                 definitely_unsent=True, error='revoked before transport')
                    continue
                adapter = self.registry.get(claim.message.connector_key)
                if claim.deleted:
                    # Deletion of forum starters would destroy the discussion
                    # thread. Preserve pre-Stage04 tombstone-edit semantics.
                    if self.tombstone_renderer is None:
                        await adapter.delete_message(claim.message)
                    else:
                        tombstone = self.tombstone_renderer(claim.domain, claim.kind)
                        if not isinstance(tombstone, ContentDocument):
                            raise ValueError('domain tombstone renderer must return ContentDocument')
                        await adapter.edit_message(claim.message, tombstone)
                else:
                    assert claim.content is not None
                    await adapter.edit_message(claim.message, claim.content)
                self._settle(claim, delivered=True)
            except Exception as exc:
                # A missing/failed HTTP response cannot prove non-application.
                self._settle(claim, delivered=False, error=str(exc))
                logger.warning('Physical mutation unresolved copy=%s/%s/%s: %s',
                               claim.domain, claim.kind, claim.copy_id, exc)
        return len(claims)
