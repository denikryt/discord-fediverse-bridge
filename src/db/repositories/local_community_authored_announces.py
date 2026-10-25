"""Durable Group-signed authored Announce admission and causal dispatch claims.

Only this repository may claim authored Create/Update/Delete envelopes. Inbound
remote relays have a separate source identity and must not share this ledger.
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
    LocalCommunity, LocalCommunityAuthoredAnnounceDelivery,
    PublishedActivityObject, RemoteSubscriber,
)


@dataclass(frozen=True)
class AuthoredAnnounceAttempt:
    """One frozen Group-signed action owned by an exact CAS claim generation."""

    id: int
    token: str
    generation: int
    group_actor_id: str
    remote_actor_id: str
    inbox_url: str
    activity_id: str
    activity_json: dict


class LocalCommunityAuthoredAnnounceRepository:
    """Use existing SQLite writer reservations for Group delivery state."""

    def __init__(self, session_provider, immediate_session_provider):
        """Bind the same transaction providers used by other federation services."""
        self.session = session_provider
        self.immediate_session = immediate_session_provider

    @staticmethod
    def stage_create(
        session: Session, *, published: PublishedActivityObject,
        community: LocalCommunity, activity_json: dict,
    ) -> int:
        """Freeze signed Group envelopes in the canonical Create transaction.

        The snapshot references the immutable Follow activity ID, never the
        mutable RemoteSubscriber row, so Undo can retain physical history.
        """
        if (published.community_actor_url != community.actor_url
            or published.deleted_at is not None
            or activity_json.get('id') != published.activity_id):
            raise ValueError('authored Announce source does not match canonical publication')
        subscribers = list(session.scalars(select(RemoteSubscriber).where(
            RemoteSubscriber.local_community_id == community.id,
            RemoteSubscriber.status == 'accepted',
        )))
        created = 0
        for subscriber in subscribers:
            # Length-framed semantic identity prevents re-Follow F2 from
            # adopting an F1 delivery even when actor and inbox are unchanged.
            identity = (published.activity_id, community.actor_url,
                        subscriber.remote_actor_id, subscriber.follow_activity_id,
                        'create', '0')
            digest = hashlib.sha256(b'local-authored-announce:v1\x00')
            for component in identity:
                data = component.encode()
                digest.update(len(data).to_bytes(4, 'big'))
                digest.update(data)
            announce_id = f"{community.actor_url.rstrip('/')}/activities/announce/{digest.hexdigest()}"
            document = {
                '@context': 'https://www.w3.org/ns/activitystreams',
                'id': announce_id, 'type': 'Announce',
                'actor': community.actor_url,
                'to': ['https://www.w3.org/ns/activitystreams#Public'],
                'cc': [community.followers_url],
                'object': activity_json,
            }
            session.add(LocalCommunityAuthoredAnnounceDelivery(
                published_object_id=published.id,
                local_community_id=community.id,
                target_remote_actor_id=subscriber.remote_actor_id,
                target_follow_activity_id=subscriber.follow_activity_id,
                target_remote_inbox_url=subscriber.remote_inbox_url,
                delivery_profile='threadiverse_group',
                group_actor_id=community.actor_url,
                activity_kind='create', content_revision=0,
                announce_activity_id=announce_id,
                activity_json=json.dumps(document, separators=(',', ':'), sort_keys=True),
                status='pending',
            ))
            created += 1
        return created

    @staticmethod
    def stage_mutation(
        session: Session, *, published: PublishedActivityObject,
        community: LocalCommunity, activity_kind: str,
        content_revision: int, activity_json: dict,
    ) -> int:
        """Atomically freeze a signed author mutation for an exact existing Follow.

        The caller must make the source revision and its outgoing activity
        durable in the same writer transaction.  An accepted Follow alone does
        not establish that its recipient ever received this particular Create.
        """
        if activity_kind not in ('update', 'delete') or content_revision <= 0:
            raise ValueError('invalid authored Group mutation kind/revision')
        ap_object = activity_json.get('object')
        # Update carries a full Page/Note; Delete may carry only the AP URL.
        object_identity = ap_object.get('id') if isinstance(ap_object, dict) else ap_object
        if (published.publication_context != 'community'
            or published.community_actor_url != community.actor_url
            or published.content_revision != content_revision
            or (published.deleted_at is not None) != (activity_kind == 'delete')
            or activity_json.get('type') != activity_kind.capitalize()
            or activity_json.get('actor') != published.actor_url
            or object_identity != published.object_id
            or not isinstance(activity_json.get('id'), str)
            or not activity_json['id']):
            raise ValueError('authored mutation source/revision/activity does not match canonical object')
        created = 0
        for subscriber in session.scalars(select(RemoteSubscriber).where(
            RemoteSubscriber.local_community_id == community.id,
            RemoteSubscriber.status == 'accepted',
        )):
            identity_filter = (
                LocalCommunityAuthoredAnnounceDelivery.published_object_id == published.id,
                LocalCommunityAuthoredAnnounceDelivery.local_community_id == community.id,
                LocalCommunityAuthoredAnnounceDelivery.target_remote_actor_id == subscriber.remote_actor_id,
                LocalCommunityAuthoredAnnounceDelivery.target_follow_activity_id == subscriber.follow_activity_id,
            )
            history = list(session.scalars(select(LocalCommunityAuthoredAnnounceDelivery).where(*identity_filter)))
            create_row = next((x for x in history if x.activity_kind == 'create'), None)
            if create_row is None or create_row.status == 'cancelled':
                # Never inherit Create proof from a previous Follow, even when
                # it targets the same actor/inbox. No synthetic backfill here.
                continue
            if (create_row.target_remote_inbox_url != subscriber.remote_inbox_url
                or create_row.group_actor_id != community.actor_url
                or any(x.activity_kind == 'delete' for x in history)):
                # Existing Delete is a terminal source-level fence, including
                # while its physical outcome is unknown.
                if any(x.activity_kind == 'delete' for x in history):
                    raise ValueError('authored Group sequence is already terminated by Delete')
                continue
            duplicates = [x for x in history if x.activity_kind == activity_kind
                          and x.content_revision == content_revision]
            if duplicates:
                # Same revision is replayed only if the original inner AP
                # activity matches. A new snapshot is never silently adopted.
                prior = json.loads(duplicates[0].activity_json)['object']
                if prior != activity_json:
                    raise ValueError('conflicting Group mutation replay')
                continue
            if activity_kind == 'delete':
                for earlier in history:
                    if earlier.activity_kind == 'update' and earlier.status in (
                        'pending', 'failed_proven_unsent'
                    ):
                        earlier.status = 'cancelled'
                        earlier.claim_token = None
                        earlier.lease_until = None
                if create_row.status in ('pending', 'failed_proven_unsent'):
                    create_row.status = 'cancelled'
                    continue
            else:
                # A newer fully admitted revision may supersede only requests
                # proved never sent. Already dispatching/ambiguous is a fence.
                for earlier in history:
                    if (earlier.activity_kind == 'update'
                        and earlier.content_revision < content_revision
                        and earlier.status in ('pending', 'failed_proven_unsent')):
                        earlier.status = 'cancelled'
                        earlier.claim_token = None
                        earlier.lease_until = None
            identity = (published.activity_id, community.actor_url,
                        subscriber.remote_actor_id, subscriber.follow_activity_id,
                        activity_kind, str(content_revision))
            digest = hashlib.sha256(b'local-authored-announce:v1\x00')
            for component in identity:
                data = component.encode()
                digest.update(len(data).to_bytes(4, 'big'))
                digest.update(data)
            announce_id = f"{community.actor_url.rstrip('/')}/activities/announce/{digest.hexdigest()}"
            envelope = {
                '@context': 'https://www.w3.org/ns/activitystreams',
                'id': announce_id, 'type': 'Announce',
                'actor': community.actor_url,
                'to': ['https://www.w3.org/ns/activitystreams#Public'],
                'cc': [community.followers_url],
                'object': activity_json,
            }
            session.add(LocalCommunityAuthoredAnnounceDelivery(
                published_object_id=published.id,
                local_community_id=community.id,
                target_remote_actor_id=subscriber.remote_actor_id,
                target_follow_activity_id=subscriber.follow_activity_id,
                target_remote_inbox_url=subscriber.remote_inbox_url,
                delivery_profile='threadiverse_group',
                group_actor_id=community.actor_url,
                activity_kind=activity_kind, content_revision=content_revision,
                announce_activity_id=announce_id,
                activity_json=json.dumps(envelope, separators=(',', ':'), sort_keys=True),
                status='pending',
            ))
            created += 1
        return created

    @staticmethod
    def _predecessors(session: Session, row: LocalCommunityAuthoredAnnounceDelivery):
        """Read immutable per-Follow history for strict causal send order."""
        return list(session.scalars(select(LocalCommunityAuthoredAnnounceDelivery).where(
            LocalCommunityAuthoredAnnounceDelivery.published_object_id == row.published_object_id,
            LocalCommunityAuthoredAnnounceDelivery.local_community_id == row.local_community_id,
            LocalCommunityAuthoredAnnounceDelivery.target_remote_actor_id == row.target_remote_actor_id,
            LocalCommunityAuthoredAnnounceDelivery.target_follow_activity_id == row.target_follow_activity_id,
        )))

    @classmethod
    def _claim_allowed(cls, session: Session, row: LocalCommunityAuthoredAnnounceDelivery) -> bool:
        """Block old Update after Delete and Update before confirmed Create."""
        # The current canonical state is authoritative at BOTH claim and
        # pre-network revalidation. A Follow that remains accepted cannot
        # authorize a Create/Update whose source was deleted meanwhile.
        published = session.get(PublishedActivityObject, row.published_object_id)
        community = session.get(LocalCommunity, row.local_community_id)
        if (published is None or community is None or community.status != 'active'
            or published.publication_context != 'community'
            or published.community_actor_url != community.actor_url
            or row.group_actor_id != community.actor_url):
            return False
        if row.activity_kind == 'delete':
            if (published.deleted_at is None
                or published.content_revision != row.content_revision):
                return False
        elif (published.deleted_at is not None
              or (row.activity_kind == 'update'
                  and published.content_revision != row.content_revision)):
            return False
        history = cls._predecessors(session, row)
        if row.activity_kind != 'create':
            create = next((x for x in history if x.activity_kind == 'create'), None)
            if create is None or create.status != 'delivered':
                return False
        if any(x.activity_kind == 'delete' and x.status == 'delivered'
               for x in history if x.id != row.id):
            return False
        if any(x.id != row.id and x.content_revision < row.content_revision
               and x.status in {'pending','failed_proven_unsent','dispatching','ambiguous'}
               for x in history):
            return False
        if any(x.id != row.id and x.status in {'dispatching','ambiguous'} for x in history):
            return False
        return True

    def claim_due(self, *, limit: int = 20) -> list[AuthoredAnnounceAttempt]:
        """Reserve causally eligible current Follow work, never retry unknown sends."""
        now = datetime.now(timezone.utc)
        claims: list[AuthoredAnnounceAttempt] = []
        with self.immediate_session() as session:
            candidates = list(session.scalars(select(LocalCommunityAuthoredAnnounceDelivery).where(
                LocalCommunityAuthoredAnnounceDelivery.status.in_(('pending','failed_proven_unsent')),
            ).order_by(LocalCommunityAuthoredAnnounceDelivery.published_object_id,
                       LocalCommunityAuthoredAnnounceDelivery.content_revision,
                       LocalCommunityAuthoredAnnounceDelivery.id)))
            for row in candidates:
                community = session.get(LocalCommunity, row.local_community_id)
                published = session.get(PublishedActivityObject, row.published_object_id)
                current = session.scalar(select(RemoteSubscriber).where(
                    RemoteSubscriber.local_community_id == row.local_community_id,
                    RemoteSubscriber.remote_actor_id == row.target_remote_actor_id,
                    RemoteSubscriber.status == 'accepted',
                    RemoteSubscriber.follow_activity_id == row.target_follow_activity_id,
                    RemoteSubscriber.remote_inbox_url == row.target_remote_inbox_url,
                ))
                # Revocation cancels only proven unsent attempts, and never
                # changes an immutable endpoint or signing actor.
                if (current is None or community is None or community.status != 'active'
                    or community.actor_url != row.group_actor_id or published is None):
                    row.status = 'cancelled'
                    continue
                if published.deleted_at is not None and row.activity_kind == 'create':
                    row.status = 'cancelled'
                    continue
                if not self._claim_allowed(session, row):
                    continue
                row.claim_token = secrets.token_hex(16)
                row.attempt_generation += 1
                row.attempt_count += 1
                row.status = 'dispatching'
                row.last_attempted_at = now
                row.lease_until = now + timedelta(seconds=120)
                claims.append(AuthoredAnnounceAttempt(
                    id=row.id, token=row.claim_token, generation=row.attempt_generation,
                    group_actor_id=row.group_actor_id,
                    remote_actor_id=row.target_remote_actor_id,
                    inbox_url=row.target_remote_inbox_url,
                    activity_id=row.announce_activity_id,
                    activity_json=json.loads(row.activity_json),
                ))
                if len(claims) >= limit:
                    break
        return claims

    def revalidate(self, attempt: AuthoredAnnounceAttempt) -> bool:
        """Recheck exact current Follow and causal history before gateway I/O."""
        with self.immediate_session() as session:
            row = session.get(LocalCommunityAuthoredAnnounceDelivery, attempt.id)
            if (row is None or row.status != 'dispatching'
                or row.claim_token != attempt.token
                or row.attempt_generation != attempt.generation
                or row.announce_activity_id != attempt.activity_id):
                return False
            return self._claim_allowed(session, row) and session.scalar(select(RemoteSubscriber.id).where(
                RemoteSubscriber.local_community_id == row.local_community_id,
                RemoteSubscriber.remote_actor_id == row.target_remote_actor_id,
                RemoteSubscriber.status == 'accepted',
                RemoteSubscriber.follow_activity_id == row.target_follow_activity_id,
                RemoteSubscriber.remote_inbox_url == row.target_remote_inbox_url,
            )) is not None

    def record_result(self, attempt: AuthoredAnnounceAttempt, *, delivered: bool,
                      error: str | None = None, definitely_unsent: bool = False) -> bool:
        """Settle only matching generation; uncertain HTTP is never auto-retried."""
        with self.immediate_session() as session:
            row = session.get(LocalCommunityAuthoredAnnounceDelivery, attempt.id)
            if (row is None or row.status != 'dispatching' or row.claim_token != attempt.token
                or row.attempt_generation != attempt.generation):
                return False
            if delivered:
                row.status = 'delivered'
            elif not definitely_unsent:
                row.status = 'ambiguous'
            else:
                # A Delete can commit while this claim is in flight. After
                # pre-network refusal we *know* this worker never sent its
                # old Update: cancel it in the same writer transaction so
                # the terminal Delete is not blocked by a stale retry row.
                history = self._predecessors(session, row)
                superseded = any(
                    prior.id != row.id
                    and prior.content_revision > row.content_revision
                    and prior.activity_kind in ('update', 'delete')
                    for prior in history
                )
                published = session.get(PublishedActivityObject, row.published_object_id)
                stale_create = row.activity_kind == 'create' and (
                    published is None or published.deleted_at is not None
                )
                row.status = ('cancelled' if superseded or stale_create
                              else 'failed_proven_unsent')
            row.claim_token = None
            row.lease_until = None
            row.last_error = str(error or '')[:512] or None
            return True
