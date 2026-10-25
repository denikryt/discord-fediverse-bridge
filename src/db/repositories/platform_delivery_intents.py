"""Crash-durable, target-scoped physical Create claims and verified receipt commits.

Only the coordinator may perform network writes. SQLite records possible sends
before invoking an adapter, and an expired dispatch never becomes retryable.
"""
from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import (
    PlatformDeliveryIntent, PlatformConnector, ChannelSurface,
    LocalCommunity, LocalCommunityThread, LocalCommunityMessage, CommunityThreadGroup,
    CommunityMessageGroup, LocalCommunityThreadSurface, LocalCommunityMessageSurface,
    CommunityThreadGroupDelivery, CommunityMessageGroupDelivery, LocalSubscriber,
    ChannelCommunitySubscription,
)
from ...platform_types import (
    ChannelRef, ThreadRef, MessageRef, ContentDocument, DeliveryPresentation,
    CreateAttempt, CreatedThread, CreatedMessage, validate_created_receipt,
    validate_message_reply,
)

_CANONICAL = {
    ('local','post'): LocalCommunityThread, ('local','comment'): LocalCommunityMessage,
    ('remote','post'): CommunityThreadGroup, ('remote','comment'): CommunityMessageGroup,
}
_OWNER = {'local_host': 'local_community_id', 'local_subscriber': 'local_subscriber_id',
          'remote_subscription': 'remote_subscription_id'}


def _json(value: object) -> str:
    """Use one strict stable transport serialization for equality and hashing."""
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def _channel(channel: ChannelRef) -> dict:
    """Serialize a physical channel, including trusted placement metadata."""
    return {'connector': channel.connector_key, 'id': channel.external_channel_id,
            'kind': channel.kind, 'guild': channel.external_guild_id}


def _destination(value: ChannelRef | ThreadRef | MessageRef) -> dict:
    """Preserve the exact immediate parent identity for every create operation."""
    if isinstance(value, ChannelRef):
        return {'type':'channel','channel':_channel(value)}
    if isinstance(value, ThreadRef):
        return {'type':'thread','channel':_channel(value.channel),'thread':value.external_thread_id}
    return {'type':'message','connector':value.connector_key,'container':value.external_container_id,
            'message':value.external_message_id}


def _decode_destination(data: dict) -> ChannelRef | ThreadRef | MessageRef:
    """Rehydrate only known, validated immutable DTO shapes."""
    if data.get('type')=='message':
        return MessageRef(data['connector'],data['container'],data['message'])
    c=data['channel']
    channel=ChannelRef(c['connector'],c['id'],c['kind'],c['guild'])
    if data['type']=='channel': return channel
    if data['type']=='thread': return ThreadRef(channel,data['thread'])
    raise ValueError('unknown physical create destination')


def _serialize_request(attempt: CreateAttempt, content: ContentDocument | None,
                       presentation: DeliveryPresentation | None, title: str | None,
                       reply_to: MessageRef | None) -> str:
    """Freeze a complete request, never a partially reconstructed SDK object."""
    if attempt.operation in ('forum_thread','reply_thread') and not title:
        raise ValueError('thread create requires title')
    if attempt.operation=='message':
        if content is None or presentation is None:
            raise ValueError('message create requires content and presentation')
        validate_message_reply(attempt.destination, reply_to)
    if attempt.operation=='forum_thread' and (content is None or presentation is None):
        raise ValueError('forum create requires content and presentation')
    if content is not None and content.attachments:
        raise ValueError('Stage 04 physical attachments are unsupported')
    data={'version':1,'attempt':{'key':attempt.key,'connector':attempt.connector_key,
           'operation':attempt.operation,'destination':_destination(attempt.destination)},
          'content':({'body':content.body,'format':content.format,'title':content.title,
                      'source_url':content.source_url} if content is not None else None),
          'presentation':({'mode':presentation.mode,'display_name':presentation.display_name,
                           'avatar_url':presentation.avatar_url} if presentation is not None else None),
          'title':title, 'reply_to':_destination(reply_to) if reply_to is not None else None}
    result=_json(data)
    if len(result.encode())>131072: raise ValueError('physical DTO exceeds bounded storage')
    return result


def decode_request(request: str) -> tuple[CreateAttempt, ContentDocument | None,
                                           DeliveryPresentation | None, str | None, MessageRef | None]:
    """Rebuild only whitelisted DTO fields from committed SQLite bytes."""
    data=json.loads(request)
    if data['version']!=1: raise ValueError('unsupported physical request version')
    a=data['attempt']
    attempt=CreateAttempt(a['key'],a['connector'],a['operation'],_decode_destination(a['destination']))
    content=ContentDocument(**data['content']) if data['content'] is not None else None
    presentation=DeliveryPresentation(**data['presentation']) if data['presentation'] is not None else None
    reply_to=_decode_destination(data['reply_to']) if data['reply_to'] is not None else None
    if reply_to is not None and not isinstance(reply_to,MessageRef):
        raise ValueError('invalid physical reply parent')
    return attempt,content,presentation,data['title'],reply_to


@dataclass(frozen=True,slots=True)
class ClaimedCreate:
    """Snapshot of a committed one-shot claim, not authorization to retry."""
    intent_id: int
    token: str
    request_json: str
    fingerprint: str


def cancel_deleted_source_intents(session: Session, *, domain: str, kind: str,
                                  canonical_id: int, revision: int) -> None:
    """Atomically fence all physical Creates after a canonical source Delete.

    Definitely-unsent requests are terminally cancelled. A possibly sent
    request cannot be retried or declared unsent merely because it expired.
    The caller already owns the SQLite writer transaction and canonical row.
    """
    rows = session.scalars(select(PlatformDeliveryIntent).where(
        PlatformDeliveryIntent.domain == domain,
        PlatformDeliveryIntent.object_kind == kind,
        PlatformDeliveryIntent.canonical_row_id == canonical_id,
    )).all()
    for row in rows:
        row.desired_deleted = True
        row.desired_revision = max(row.desired_revision, revision)
        if row.state in ('blocked', 'pending', 'failed_definite'):
            # No physical Create could have started from these states.
            row.state = 'cancelled'
            row.dispatch_token = None
            row.lease_until = None
        elif row.state == 'dispatching':
            # Committed claims may have transmitted a POST. Keep the token
            # and frozen request for exact receipt reconciliation only.
            row.state = 'unresolved'
        # delivered, unresolved and orphan receipts remain durable evidence.


class PlatformDeliveryIntentRepository:
    """Validate canonical/surface ownership and persist every Create transition."""

    def __init__(self, session_provider, immediate_session_provider):
        """Use shared database sessions with BEGIN IMMEDIATE for all writers."""
        self._session_provider=session_provider
        self._immediate_session_provider=immediate_session_provider

    @staticmethod
    def _scope(session: Session, row: PlatformDeliveryIntent):
        """Verify exact source and target ownership before any new POST."""
        canonical=session.get(_CANONICAL[(row.domain,row.object_kind)],row.canonical_row_id)
        surface=session.get(ChannelSurface,row.channel_surface_id)
        if canonical is None or surface is None or surface.connector_id != row.connector_id:
            return None
        expected=_OWNER.get(row.target_owner_kind)
        if expected is None or getattr(surface,expected)!=row.target_owner_id or surface.kind!='forum':
            return None
        if row.request_json:
            # The frozen request may outlive a mutable forum claim. Never
            # transfer the right to POST to a different channel or guild.
            attempt,_,_,_,_=decode_request(row.request_json)
            target=attempt.destination
            forum=(target if isinstance(target,ChannelRef) else
                   target.channel if isinstance(target,ThreadRef) else None)
            if (forum is None or forum.connector_key!=session.get(PlatformConnector,row.connector_id).connector_key
                or forum.external_channel_id!=surface.external_channel_id
                or forum.kind!='forum' or forum.external_guild_id!=surface.external_guild_id):
                return None
        if row.domain=='local':
            # Comments own only a canonical thread FK. Resolve its community
            # before checking that this forum is its current lawful owner.
            if row.object_kind=='comment':
                parent=session.get(LocalCommunityThread,canonical.local_community_thread_id)
                if parent is None or parent.deleted_at is not None:return None
                community_id=parent.local_community_id
            else:
                community_id=canonical.local_community_id
            # A valid surface claim is not enough after a moderator disables
            # the canonical Local Community. This guard is shared by reserve,
            # claim, restart recovery and the final pre-POST authorization.
            community = session.get(LocalCommunity, community_id)
            if community is None or community.status == 'disabled':
                return None
            if row.target_owner_kind=='local_host' and surface.local_community_id!=community_id:
                return None
            if row.target_owner_kind=='local_subscriber':
                subscriber=session.get(LocalSubscriber,row.target_owner_id)
                if subscriber is None or subscriber.local_community_id!=community_id or subscriber.status!='active':
                    return None
            if row.target_owner_kind not in ('local_host','local_subscriber'):return None
        else:
            if row.target_owner_kind!='remote_subscription':return None
            subscription=session.get(ChannelCommunitySubscription,row.target_owner_id)
            if subscription is None or subscription.lemmy_community_actor_id!=canonical.community_actor_id:
                return None
            if row.object_kind=='comment':
                parent=session.get(CommunityThreadGroup,canonical.thread_group_id)
                if parent is None or parent.community_actor_id!=canonical.community_actor_id or parent.deleted_at is not None:
                    return None
            if getattr(subscription,'status',None)!='accepted':return None
        return canonical,surface

    @staticmethod
    def _canonical_nested_parent(session: Session,row: PlatformDeliveryIntent):
        """Require a child→parent relation in one immutable canonical thread."""
        if row.object_kind!='comment' or row.blocked_parent_canonical_id is None:
            return None
        model=_CANONICAL[(row.domain,'comment')]
        child=session.get(model,row.canonical_row_id)
        parent=session.get(model,row.blocked_parent_canonical_id)
        if child is None or parent is None or parent.deleted_at is not None:
            return None
        if row.domain=='local':
            if (child.local_community_thread_id!=parent.local_community_thread_id
                or child.parent_ap_object_id!=parent.ap_object_id):
                return None
        else:
            if (child.thread_group_id!=parent.thread_group_id
                or child.parent_message_group_id!=parent.id):
                return None
        return parent

    @staticmethod
    def _verified_nested_parent(session: Session, row: PlatformDeliveryIntent,
                                destination: ThreadRef) -> MessageRef | None:
        """Reuse the exact typed canonical-parent resolver in both domains."""
        from ...content_sync.parent_mapping import MissingParent, resolve_target_parent

        parent = PlatformDeliveryIntentRepository._canonical_nested_parent(session, row)
        if parent is None:
            return None
        child = session.get(_CANONICAL[(row.domain, 'comment')], row.canonical_row_id)
        thread_id = (child.local_community_thread_id if row.domain == 'local'
                     else child.thread_group_id)
        try:
            physical = resolve_target_parent(
                session, domain=row.domain, canonical_thread_id=thread_id,
                target=destination, target_surface_id=row.channel_surface_id,
                canonical_parent_message_id=parent.id,
            )
        except ValueError:
            return None
        return physical if isinstance(physical, MessageRef) else None

    def reserve(self, *, domain: str,kind: str,canonical_id: int, forum: ChannelRef,
                channel_surface_id: int,owner_kind: str,owner_id: int,
                attempt: CreateAttempt | None=None, content: ContentDocument | None=None,
                presentation: DeliveryPresentation | None=None,title: str | None=None,
                reply_to: MessageRef | None=None, blocked_parent_canonical_id: int | None=None):
        """Create one target intent before POST, blocked or fully immutable pending.

        A ready target must freeze its full request in this same transaction;
        there is no sendable intermediate row with unfrozen bytes.
        """
        if (domain,kind) not in _CANONICAL or owner_kind not in _OWNER:
            raise ValueError('unsupported canonical/owner scope')
        if forum.kind!='forum':raise ValueError('physical copy target must be a forum')
        if (attempt is None)==(blocked_parent_canonical_id is None):
            raise ValueError('supply either a complete create request or a known blocked parent')
        with self._immediate_session_provider() as session:
            connector=session.scalar(select(PlatformConnector).where(PlatformConnector.connector_key==forum.connector_key))
            if connector is None:raise ValueError('unknown connector')
            surface=session.get(ChannelSurface,channel_surface_id)
            if (surface is None or surface.connector_id!=connector.id
                or surface.external_channel_id!=forum.external_channel_id
                or surface.external_guild_id!=forum.external_guild_id):
                raise ValueError('forum target does not match registered surface')
            key=f'fanout:v1:{domain}:{kind}:{canonical_id}:{forum.connector_key}:{channel_surface_id}'
            existing=session.scalar(select(PlatformDeliveryIntent).where(
                PlatformDeliveryIntent.domain==domain,PlatformDeliveryIntent.object_kind==kind,
                PlatformDeliveryIntent.canonical_row_id==canonical_id,
                PlatformDeliveryIntent.channel_surface_id==channel_surface_id))
            if existing is not None:
                if (existing.attempt_key,existing.connector_id,existing.target_owner_kind,
                    existing.target_owner_id)!=(key,connector.id,owner_kind,owner_id):
                    raise ValueError('previous target scope conflicts with current claim')
                if attempt is not None and existing.request_json is not None:
                    payload=_serialize_request(attempt,content,presentation,title,reply_to)
                    if payload!=existing.request_json:raise ValueError('frozen create request conflict')
                return existing
            canonical=session.get(_CANONICAL[(domain,kind)],canonical_id)
            revision=canonical.content_revision if canonical is not None else 0
            row=PlatformDeliveryIntent(domain=domain,object_kind=kind,canonical_row_id=canonical_id,
                connector_id=connector.id,channel_surface_id=channel_surface_id,operation='create',
                attempt_key=key,initial_revision=revision,desired_revision=revision,
                target_owner_kind=owner_kind,target_owner_id=owner_id,
                state='blocked' if attempt is None else 'pending',
                blocked_parent_canonical_id=blocked_parent_canonical_id)
            if self._scope(session,row) is None or canonical.deleted_at is not None:
                raise ValueError('canonical/target no longer authorized')
            if blocked_parent_canonical_id is not None and self._canonical_nested_parent(session,row) is None:
                raise ValueError('blocked parent must be a known canonical nested comment')
            if attempt is not None:
                if attempt.key!=key or attempt.connector_key!=forum.connector_key:
                    raise ValueError('create attempt is not bound to durable target')
                if kind == 'comment':
                    # Even an immediately ready nested reply needs SQLite
                    # proof of its precise parent copy in this destination.
                    # Passing a plausible MessageRef is not authorization.
                    nested_id = None
                    if domain == 'remote':
                        nested_id = canonical.parent_message_group_id
                    elif canonical.parent_ap_object_id:
                        nested = session.scalar(select(LocalCommunityMessage).where(
                            LocalCommunityMessage.ap_object_id == canonical.parent_ap_object_id,
                            LocalCommunityMessage.local_community_thread_id == canonical.local_community_thread_id,
                        ))
                        nested_id = nested.id if nested is not None else None
                        parent_thread = session.get(LocalCommunityThread, canonical.local_community_thread_id)
                        if nested is None and (parent_thread is None or
                                canonical.parent_ap_object_id != parent_thread.ap_object_id):
                            raise ValueError('identified nested AP parent is not canonical')
                    if nested_id is not None:
                        row.blocked_parent_canonical_id = nested_id
                        proven = (self._verified_nested_parent(session, row, attempt.destination)
                                  if isinstance(attempt.destination, ThreadRef) else None)
                        if proven is None or proven != reply_to:
                            raise ValueError('nested physical parent lacks exact proven receipt')
                        row.blocked_parent_canonical_id = None
                payload=_serialize_request(attempt,content,presentation,title,reply_to)
                row.request_json=payload
                row.request_fingerprint=hashlib.sha256(payload.encode()).hexdigest()
                row.request_revision=revision
            session.add(row);session.flush()
            return row

    def materialize(self, intent_id:int, *, attempt:CreateAttempt, content:ContentDocument | None,
                    presentation:DeliveryPresentation | None,title:str | None, reply_to:MessageRef | None=None) -> None:
        """Freeze a previously blocked request exactly once after verified parent."""
        payload=_serialize_request(attempt,content,presentation,title,reply_to)
        with self._immediate_session_provider() as session:
            row=session.get(PlatformDeliveryIntent,intent_id)
            if row is None:raise ValueError('unknown intent')
            if attempt.key!=row.attempt_key:raise ValueError('wrong create attempt')
            if row.request_json is not None:
                if row.request_json!=payload:raise ValueError('frozen create request conflict')
                return
            if row.state!='blocked':
                raise ValueError('only blocked requests can first materialize')
            scoped=self._scope(session,row)
            if scoped is None or scoped[0].deleted_at is not None or row.desired_deleted:
                row.state='cancelled';row.dispatch_token=None;return
            # A caller-provided success flag cannot substitute for a stored
            # destination parent. Check canonical identity and physical thread.
            if not isinstance(attempt.destination,ThreadRef):
                raise ValueError('nested physical reply requires a thread')
            proven_parent=self._verified_nested_parent(session,row,attempt.destination)
            if proven_parent is None or proven_parent!=reply_to:
                raise ValueError('blocked parent lacks exact verified destination receipt')
            row.request_json=payload
            row.request_fingerprint=hashlib.sha256(payload.encode()).hexdigest()
            row.request_revision=scoped[0].content_revision
            row.state='pending';row.blocked_parent_canonical_id=None

    def claim(self, intent_id:int) -> ClaimedCreate | None:
        """Reserve one POST; committed dispatching is uncertain after a crash."""
        with self._immediate_session_provider() as session:
            row=session.get(PlatformDeliveryIntent,intent_id)
            if row is None or row.state!='pending' or row.desired_deleted:return None
            scoped=self._scope(session,row)
            if scoped is None or scoped[0].deleted_at is not None:
                row.state='cancelled';return None
            if not row.request_json or not row.request_fingerprint:return None
            token=secrets.token_hex(20)
            row.state='dispatching';row.dispatch_token=token
            row.lease_until=datetime.now(timezone.utc)+timedelta(seconds=90)
            return ClaimedCreate(row.id,token,row.request_json,row.request_fingerprint)

    def dispatch_still_authorized(self, claim: ClaimedCreate) -> bool:
        """Recheck an exact claim and canonical/target liveness before POST.

        The check cannot make SQLite and Discord atomic. It does prevent a
        worker that observes a revoked source between claim and transport
        from initiating a fresh request. The claim remains sticky uncertain.
        """
        with self._session_provider() as session:
            row=session.get(PlatformDeliveryIntent, claim.intent_id)
            if (row is None or row.state != 'dispatching'
                or row.dispatch_token != claim.token or row.desired_deleted
                or row.request_fingerprint != claim.fingerprint
                or row.request_json != claim.request_json):
                return False
            scoped=self._scope(session,row)
            return scoped is not None and scoped[0].deleted_at is None

    def committed_dispatch_proof(self, claim: ClaimedCreate):
        """Grant a single SDK dispatch only for a current committed live claim.

        The repository, rather than the caller's key or adapter, confirms
        SQLite state before issuing process-local, non-serializable evidence.
        """
        from ...platform_types import _issue_committed_create_dispatch_proof
        if not self.dispatch_still_authorized(claim):
            return None
        attempt, _, _, _, _ = decode_request(claim.request_json)
        return _issue_committed_create_dispatch_proof(
            claim.intent_id, attempt.key, claim.fingerprint, claim.token,
        )

    def mark_unresolved(self, intent_id:int, *, token:str,reason:str) -> bool:
        """Persist possibly sent/unknown outcome without granting another POST."""
        with self._immediate_session_provider() as session:
            row=session.get(PlatformDeliveryIntent,intent_id)
            if row is None or row.state!='dispatching' or row.dispatch_token!=token:return False
            row.state='unresolved';row.last_error_kind=reason[:64]
            return True

    def materialize_ready_blocked(self) -> list[int]:
        """Freeze nested children only when their exact target parent now exists.

        All data is already in canonical SQLite rows; the wake never infers
        content from a newly received event or guesses an absent MessageRef.
        This writer transaction makes a complete immutable DTO before a claim.
        """
        from ...content_sync.parent_mapping import MissingParent, resolve_target_parent
        from ...formatting import format_lemmy_comment_for_discord, normalize_text

        ready: list[int] = []
        with self._immediate_session_provider() as session:
            children = session.scalars(select(PlatformDeliveryIntent).where(
                PlatformDeliveryIntent.state == 'blocked',
                PlatformDeliveryIntent.object_kind == 'comment',
                PlatformDeliveryIntent.blocked_parent_canonical_id.is_not(None),
            ).order_by(PlatformDeliveryIntent.id)).all()
            for row in children:
                scoped = self._scope(session, row)
                if scoped is None or scoped[0].deleted_at is not None or row.desired_deleted:
                    row.state = 'cancelled'
                    row.dispatch_token = None
                    continue
                canonical, surface = scoped
                parent = self._canonical_nested_parent(session, row)
                if parent is None:
                    # A deleted/foreign canonical parent cannot awaken a child.
                    row.state = 'cancelled'
                    continue
                if not canonical.current_content_json:
                    # A released-v0 row with no provable normalized body may not
                    # be turned into a made-up physical Create.
                    continue
                try:
                    snapshot = json.loads(canonical.current_content_json)
                except (TypeError, ValueError):
                    continue
                if (not isinstance(snapshot, dict) or snapshot.get('schemaVersion') != 1
                    or snapshot.get('kind') != 'comment'
                    or snapshot.get('apObjectId') != canonical.ap_object_id
                    or not isinstance(snapshot.get('bodyMarkdown'), str)
                    or not isinstance(snapshot.get('authorActorId'), str)
                    or not isinstance(snapshot.get('authorName'), str)):
                    continue
                connector = session.get(PlatformConnector, row.connector_id)
                if connector is None or not surface.external_guild_id:
                    continue
                forum = ChannelRef(connector.connector_key, surface.external_channel_id,
                                   'forum', surface.external_guild_id)
                thread_id = (canonical.local_community_thread_id if row.domain == 'local'
                             else canonical.thread_group_id)
                physical_model = (LocalCommunityThreadSurface if row.domain == 'local'
                                  else CommunityThreadGroupDelivery)
                root_key = ('local_community_thread_id' if row.domain == 'local'
                            else 'thread_group_id')
                root = session.scalar(select(physical_model).where(
                    getattr(physical_model, root_key) == thread_id,
                    physical_model.channel_surface_id == row.channel_surface_id))
                if root is None or root.connector_id != row.connector_id:
                    continue
                destination = ThreadRef(forum, root.external_thread_id)
                try:
                    reply_to = resolve_target_parent(
                        session, domain=row.domain, canonical_thread_id=thread_id,
                        target=destination, target_surface_id=row.channel_surface_id,
                        canonical_parent_message_id=parent.id,
                    )
                except ValueError:
                    continue
                if isinstance(reply_to, MissingParent):
                    continue
                content = ContentDocument(format_lemmy_comment_for_discord(
                    snapshot['authorName'], normalize_text(snapshot['bodyMarkdown']),
                    snapshot.get('url') or '', actor_id=snapshot['authorActorId'],
                ))
                attempt = CreateAttempt(row.attempt_key, connector.connector_key,
                                        'message', destination)
                try:
                    request = _serialize_request(
                        attempt, content, DeliveryPresentation(), None, reply_to)
                except (ValueError, TypeError):
                    continue
                row.request_json = request
                row.request_fingerprint = hashlib.sha256(request.encode()).hexdigest()
                row.request_revision = canonical.content_revision
                row.blocked_parent_canonical_id = None
                row.state = 'pending'
                ready.append(row.id)
        return ready

    def recoverable_pending_ids(self) -> list[int]:
        """Find only definitely unsent, fully materialized authorized targets.

        Unknown/expired dispatch is never included: restarting a process
        cannot prove that an earlier Discord POST did not succeed remotely.
        """
        with self._session_provider() as session:
            rows = session.scalars(select(PlatformDeliveryIntent).where(
                PlatformDeliveryIntent.state == 'pending').order_by(PlatformDeliveryIntent.id))
            recoverable: list[int] = []
            for row in rows:
                if (not row.request_json or not row.request_fingerprint
                    or row.desired_deleted):
                    continue
                scoped = self._scope(session, row)
                if scoped is not None and scoped[0].deleted_at is None:
                    recoverable.append(row.id)
            return recoverable

    def expire_claims(self) -> int:
        """Fail closed on expired dispatch claims, including after restart."""
        with self._immediate_session_provider() as session:
            now=datetime.now(timezone.utc)
            changed=0
            for row in session.scalars(select(PlatformDeliveryIntent).where(PlatformDeliveryIntent.state=='dispatching')):
                lease=row.lease_until
                if lease is not None and (lease.replace(tzinfo=timezone.utc) if lease.tzinfo is None else lease)<=now:
                    row.state='unresolved';row.last_error_kind='expired_dispatch';changed+=1
            return changed

    def inspect_orphans(self) -> list[dict]:
        """Read persistent verified orphans without creating deletion authority.

        Inventory is restricted to work requiring an operator, including an
        uncertain previous cleanup. Never infer removal from missing mappings
        or treat the new current owner of a surface as the old receipt owner.
        """
        with self._session_provider() as session:
            rows = session.scalars(select(PlatformDeliveryIntent).where(
                PlatformDeliveryIntent.state == 'cancelled_after_send',
                PlatformDeliveryIntent.orphan_cleanup_state.in_((
                    'review_required', 'cleanup_unresolved',
                )),
            ).order_by(PlatformDeliveryIntent.id)).all()
            results = []
            for row in rows:
                if not row.orphan_receipt_json or row.orphan_receipt_verified_at is None:
                    # A malformed/corrupt row cannot be a deletion target.
                    continue
                results.append({
                    'intent_id': row.id,
                    'attempt_key': row.attempt_key,
                    'fingerprint': row.request_fingerprint,
                    'original_owner_kind': row.target_owner_kind,
                    'original_owner_id': row.target_owner_id,
                    'original_surface_id': row.channel_surface_id,
                    'original_connector_id': row.connector_id,
                    'receipt': json.loads(row.orphan_receipt_json),
                    'cleanup_state': row.orphan_cleanup_state,
                })
            return results

    def record_orphan_retention(
        self, intent_id: int, *, operator_id: str, note: str,
        cleanup_state: str = 'retained',
    ) -> bool:
        """Record a fail-closed operator decision without deleting remote data.

        The current PlatformDeliveryPort cannot delete a forum thread, so
        retention/manual platform administration is the only safe terminal
        outcome this repository itself can authorize. Never expose an API to
        mark a physical orphan removed without fresh independent proof.
        """
        if cleanup_state != 'retained':
            raise ValueError('only audited retention is supported without verified remote cleanup')
        if not isinstance(operator_id, str) or not operator_id.strip() or len(operator_id) > 200:
            raise ValueError('nonempty bounded operator identity is required')
        if not isinstance(note, str) or not note.strip() or len(note) > 1000:
            raise ValueError('nonempty bounded operator reason is required')
        with self._immediate_session_provider() as session:
            row = session.get(PlatformDeliveryIntent, intent_id)
            if (row is None or row.state != 'cancelled_after_send'
                or row.orphan_cleanup_state != 'review_required'
                or row.orphan_cleanup_claim_token is not None
                or not row.orphan_receipt_json or row.orphan_receipt_verified_at is None):
                return False
            evidence = {'version': 1, 'operator': operator_id.strip(),
                        'reason': note.strip(),
                        'recorded_at': datetime.now(timezone.utc).isoformat(),
                        'decision': 'retained'}
            row.orphan_cleanup_evidence_json = _json(evidence)
            row.orphan_cleanup_state = 'retained'
            return True

    def unresolved_claim(self, intent_id: int) -> ClaimedCreate | None:
        """Expose immutable unresolved source proof for read-only reconciliation.

        This does not grant a new POST. The same token and DTO are checked
        again under the writer lock before a verified receipt can be adopted.
        """
        with self._session_provider() as session:
            row = session.get(PlatformDeliveryIntent, intent_id)
            if (row is None or row.state != 'unresolved' or not row.dispatch_token
                or not row.request_json or not row.request_fingerprint):
                return None
            return ClaimedCreate(row.id, row.dispatch_token, row.request_json,
                                 row.request_fingerprint)

    def unresolved_ids(self) -> list[int]:
        """List uncertain attempts for read-only startup reconciliation only."""
        with self._session_provider() as session:
            return list(session.scalars(select(PlatformDeliveryIntent.id).where(
                PlatformDeliveryIntent.state == 'unresolved').order_by(
                    PlatformDeliveryIntent.id)).all())

    def committed_ack_kind(self, claim: ClaimedCreate) -> str | None:
        """Prove a matching durable terminal receipt/unknown after writer commit.

        This is the coordinator-side SQLite authority for cache eviction. The
        SDK adapter must not read the database or treat an error as this proof.
        """
        with self._session_provider() as session:
            row = session.get(PlatformDeliveryIntent, claim.intent_id)
            if (row is None or row.dispatch_token != claim.token
                or row.request_fingerprint != claim.fingerprint
                or row.request_json != claim.request_json):
                return None
            if row.state in ('delivered', 'cancelled_after_send'):
                return 'delivered'
            if row.state == 'unresolved':
                return 'unresolved'
            return None

    def committed_ack_proof(self, claim: ClaimedCreate):
        """Mint an opaque ACK capability only from verified terminal SQLite state."""
        from ...platform_types import _issue_committed_create_ack_proof
        kind = self.committed_ack_kind(claim)
        if kind is None:
            return None
        return _issue_committed_create_ack_proof(
            claim.intent_id, decode_request(claim.request_json)[0].key,
            claim.fingerprint, claim.token, kind,
        )

    def commit_receipt(self, intent_id:int, *,token:str,receipt:CreatedThread | CreatedMessage,
                       reconciliation_proof=None) -> bool:
        """Commit verified initial receipt, or adopt a proven unresolved result.

        A naked physical ID cannot turn an unknown Create into delivered. For
        `unresolved` the adapter must attest to this *same* completed attempt,
        and the proof is validated inside the same SQLite writer transaction.
        """
        from ...platform_types import _verify_verified_create_reconciliation_proof
        with self._immediate_session_provider() as session:
            row=session.get(PlatformDeliveryIntent,intent_id)
            if row is None or row.dispatch_token!=token:return False
            if row.state == 'dispatching':
                if reconciliation_proof is not None:return False
            elif row.state == 'unresolved':
                if not _verify_verified_create_reconciliation_proof(
                    reconciliation_proof, intent_id, row.attempt_key,
                    row.request_fingerprint, token, receipt,
                ):
                    return False
            else:
                return False
            attempt,_,presentation,_,reply_to=decode_request(row.request_json)
            validate_created_receipt(attempt,receipt,presentation=presentation)
            if isinstance(receipt,CreatedThread):
                requested=attempt.destination
                if (receipt.thread.channel.kind!='forum' or requested.kind!='forum'
                    or requested.external_guild_id is None
                    or receipt.thread.channel.external_guild_id != requested.external_guild_id):
                    raise ValueError('unverified created forum placement/guild')
            scoped=self._scope(session,row)
            if scoped is None:
                # A verified result after owner change belongs only to the
                # original target, never to its next community owner.
                physical=({'thread':_destination(receipt.thread),'starter':_destination(receipt.starter)}
                          if isinstance(receipt,CreatedThread)
                          else {'message':_destination(receipt.message)})
                row.orphan_receipt_json=_json({'version':1,'attempt_key':row.attempt_key,
                    'fingerprint':row.request_fingerprint,'mode':receipt.delivery_mode,
                    'receipt':physical})
                row.orphan_receipt_verified_at=datetime.now(timezone.utc)
                row.orphan_cleanup_state='review_required'
                row.state='cancelled_after_send'
                return True
            model={('local','post'):LocalCommunityThreadSurface,
                   ('local','comment'):LocalCommunityMessageSurface,
                   ('remote','post'):CommunityThreadGroupDelivery,
                   ('remote','comment'):CommunityMessageGroupDelivery}[(row.domain,row.object_kind)]
            key={('local','post'):'local_community_thread_id',('local','comment'):'local_community_message_id',
                 ('remote','post'):'thread_group_id',('remote','comment'):'message_group_id'}[(row.domain,row.object_kind)]
            existing=session.scalar(select(model).where(getattr(model,key)==row.canonical_row_id,
                                                     model.channel_surface_id==row.channel_surface_id))
            if existing is not None:raise ValueError('conflicting or duplicate physical receipt')
            channel=scoped[1].external_channel_id
            # Discord-specific historical columns are projections only. Typed
            # identity is authoritative, and nonnumeric connector IDs remain valid.
            discord=attempt.connector_key=='discord-primary'
            def legacy(value):
                return int(value) if discord and value is not None and value.isdecimal() else None
            base=dict(connector_id=row.connector_id,channel_surface_id=row.channel_surface_id,
                      applied_revision=row.request_revision,role=('host' if row.target_owner_kind=='local_host' else 'local_subscriber'
                          if row.target_owner_kind=='local_subscriber' else 'inbound'
                          if getattr(scoped[0], 'source_channel_id', None) is None else 'mirror'))
            if row.object_kind=='post':
                if not isinstance(receipt,CreatedThread):raise ValueError('post requires forum receipt')
                base.update(external_thread_id=receipt.thread.external_thread_id,
                            external_starter_message_id=receipt.starter.external_message_id,
                            discord_thread_id=legacy(receipt.thread.external_thread_id),
                            discord_starter_message_id=legacy(receipt.starter.external_message_id))
            else:
                if not isinstance(receipt,CreatedMessage):raise ValueError('comment requires message receipt')
                dest=attempt.destination
                if not isinstance(dest,ThreadRef):raise ValueError('comment requires physical thread')
                base.update(external_message_id=receipt.message.external_message_id,
                            external_parent_message_id=reply_to.external_message_id if reply_to else None,
                            discord_message_id=legacy(receipt.message.external_message_id))
                if row.domain=='remote':
                    base['external_thread_id']=dest.external_thread_id
            if row.domain=='remote':
                base.update(discord_channel_id=legacy(channel))
                if row.object_kind=='comment':base.update(discord_thread_id=legacy(dest.external_thread_id))
            else:
                base.update(discord_forum_channel_id=legacy(channel),
                            local_subscriber_id=row.target_owner_id if row.target_owner_kind=='local_subscriber' else None)
                if row.object_kind=='comment':
                    parent=session.scalar(select(LocalCommunityThreadSurface).where(
                        LocalCommunityThreadSurface.local_community_thread_id==scoped[0].local_community_thread_id,
                        LocalCommunityThreadSurface.channel_surface_id==row.channel_surface_id,
                        LocalCommunityThreadSurface.external_thread_id==dest.external_thread_id))
                    if parent is None:raise ValueError('canonical reply has no verified target thread surface')
                    base.update(local_community_thread_surface_id=parent.id,
                                parent_discord_message_id=legacy(reply_to.external_message_id) if reply_to else None)
            copy=model(**{key:row.canonical_row_id},**base)
            session.add(copy);session.flush()
            row.state='delivered';row.applied_revision=row.request_revision
            return True


def validate_authenticated_update(*, current_content_json: str | None,
                                  new_content_json: str, ap_object_id: str,
                                  actor_id: str, updated_at: datetime | None,
                                  source_activity_id: str | None,
                                  previous_order_key: str | None,
                                  previous_identity: str | None) -> tuple[str, str, str, bool]:
    """Verify an ordered AP Update without substituting its receive timestamp.

    This is an admission-time pure check shared by the two owning canonical
    domains. The caller must reserve the writer and atomically save its result.
    """
    if updated_at is None or updated_at.tzinfo is None or updated_at.utcoffset() is None:
        raise ValueError('Update requires a verified timezone-aware source revision')
    if not isinstance(source_activity_id, str) or not source_activity_id.strip():
        raise ValueError('Update requires a stable source activity identity')
    if len(source_activity_id) > 512:
        raise ValueError('Update source activity identity is unbounded')
    if not isinstance(new_content_json, str) or len(new_content_json.encode()) > 131072:
        raise ValueError('Update canonical payload exceeds size limit')
    try:
        current = json.loads(current_content_json or '')
        desired = json.loads(new_content_json)
    except (TypeError, ValueError) as exc:
        raise ValueError('Update has no verified existing and desired AP content') from exc
    if (not isinstance(current, dict) or not isinstance(desired, dict)
        or current.get('authorActorId') != actor_id or not actor_id
        or current.get('apObjectId') != ap_object_id
        or desired.get('authorActorId') != actor_id
        or desired.get('apObjectId') != ap_object_id
        or current.get('kind') != desired.get('kind')
        or desired.get('schemaVersion') != 1
        or not isinstance(desired.get('bodyMarkdown'), str)):
        raise ValueError('Update changes authenticated AP object ownership')
    normalized = _json(desired)
    clock = updated_at.astimezone(timezone.utc).isoformat(timespec='microseconds')
    # Keep both identity and bytes to reject same-ID conflicting content.
    identity = _json({'sourceActivityId': source_activity_id,
                      'payloadSha256': hashlib.sha256(normalized.encode()).hexdigest()})
    if previous_order_key is not None:
        if clock < previous_order_key:
            raise ValueError('stale AP Update would overwrite newer content')
        if clock == previous_order_key:
            if previous_identity != identity:
                raise ValueError('conflicting AP Update with identical source clock')
            return normalized, clock, identity, False
        if previous_identity is not None and json.loads(previous_identity).get('sourceActivityId') == source_activity_id:
            raise ValueError('reused AP activity ID for a different revision')
    return normalized, clock, identity, True
