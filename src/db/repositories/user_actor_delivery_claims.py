"""Claim scheduling policy for durable User Actor endpoint deliveries."""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Callable

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session

from ...models import (
    User,
    UserActorFollower,
    UserActorOutboundDelivery,
    UserActorOutboundDeliveryFollower,
    UserActorOutboundSource,
    ChannelCommunitySubscription,
    ChannelSurface,
    PublishedActivityObject,
    InboundConversationObject,
    RemoteActor,
)
from .user_actor_delivery_types import ClaimedUserActorDelivery


class UserActorDeliveryClaimRepository:
    """Claim due delivery rows under the database writer reservation."""

    def __init__(self, immediate_session_provider: Callable):
        """Bind the writer-reserved session boundary used for claim batches."""
        self._immediate_session_provider = immediate_session_provider

    def claim_due(
        self, *, limit: int, per_user_limit: int, stale_after_seconds: int
    ) -> list[ClaimedUserActorDelivery]:
        """Claim bounded oldest endpoint versions under one writer reservation."""
        now = datetime.now(timezone.utc)
        stale_before = now - timedelta(seconds=stale_after_seconds)
        claimed: list[ClaimedUserActorDelivery] = []
        seen_endpoints: set[tuple[int, str]] = set()
        claimed_per_user: dict[int, int] = {}

        with self._immediate_session_provider() as session:
            # Candidate ordering is deterministic, but every ordering and
            # eligibility invariant is rechecked under the same writer lock.
            for delivery, source in self._load_due_candidates(
                session, now=now, stale_before=stale_before
            ):
                endpoint_key = (source.user_id, delivery.target_endpoint_url)
                if not self._within_claim_limits(
                    endpoint_key=endpoint_key,
                    user_id=source.user_id,
                    seen_endpoints=seen_endpoints,
                    claimed_per_user=claimed_per_user,
                    per_user_limit=per_user_limit,
                ):
                    continue
                # A newer content Delete is a terminal barrier even while it is
                # waiting in retry backoff.  Older Create/Update rows remain
                # durable but non-claimable until that Delete succeeds; once it
                # is delivered they are finalized as superseded.
                newer_delete_status = self._newer_delete_status(
                    session, delivery=delivery, source=source
                )
                if newer_delete_status == "delivered":
                    self._mark_terminal_without_claim(delivery, status="superseded")
                    continue
                if newer_delete_status in {"pending", "failed", "sending"}:
                    continue

                # A newer acknowledged profile makes this older replacement
                # permanently obsolete; a merely pending newer row does not.
                if self._has_newer_delivered(session, delivery=delivery, source=source):
                    self._mark_terminal_without_claim(delivery, status="superseded")
                    continue
                # Older unfinished work is the endpoint ordering barrier,
                # including failed rows whose retry backoff is not yet due.
                if self._has_lower_unfinished(
                    session, delivery=delivery, source=source
                ):
                    continue

                # Historical audience rows are transportable only while an
                # exact snapshotted follower incarnation remains accepted.
                represented_actor_ids = tuple(dict.fromkeys((
                    *self._eligible_actor_ids(session, delivery.id, source.user_id),
                    *self._eligible_direct_actor_ids(session, delivery, source),
                )))
                user = session.get(User, source.user_id)
                if not represented_actor_ids or user is None:
                    self._mark_terminal_without_claim(delivery, status="ineligible")
                    continue

                claimed_delivery = self._claim_delivery(
                    delivery=delivery,
                    source=source,
                    user=user,
                    represented_actor_ids=represented_actor_ids,
                    claimed_at=now,
                )
                claimed.append(claimed_delivery)
                seen_endpoints.add(endpoint_key)
                claimed_per_user[source.user_id] = (
                    claimed_per_user.get(source.user_id, 0) + 1
                )
                if len(claimed) >= limit:
                    break

        return claimed

    def revalidate_claim(self, delivery_id: int, token: str) -> tuple[str, ...]:
        """Recheck current follower and direct grants just before signed transport.

        A revoked Follow or re-bound forum after claim must never reuse the old
        recipient snapshot to initiate a new HTTP send. An empty result denies
        this claim; unrelated endpoint claims remain independent.
        """
        with self._immediate_session_provider() as session:
            delivery = session.get(UserActorOutboundDelivery, delivery_id)
            if (delivery is None or delivery.status != 'sending'
                or delivery.claim_token != token):
                return ()
            source = session.get(UserActorOutboundSource, delivery.source_id)
            if source is None or source.state != 'ready':
                return ()
            return tuple(dict.fromkeys((
                *self._eligible_actor_ids(session, delivery.id, source.user_id),
                *self._eligible_direct_actor_ids(session, delivery, source),
            )))

    @staticmethod
    def _load_due_candidates(
        session: Session, *, now: datetime, stale_before: datetime
    ) -> list[tuple[UserActorOutboundDelivery, UserActorOutboundSource]]:
        """Load deterministic due or stale candidates for claim evaluation."""
        return list(
            session.execute(
                select(UserActorOutboundDelivery, UserActorOutboundSource)
                .join(
                    UserActorOutboundSource,
                    UserActorOutboundSource.id == UserActorOutboundDelivery.source_id,
                )
                .where(
                    UserActorOutboundSource.state == "ready",
                    or_(
                        and_(
                            UserActorOutboundDelivery.status.in_(("pending", "failed")),
                            or_(
                                UserActorOutboundDelivery.next_attempt_at.is_(None),
                                UserActorOutboundDelivery.next_attempt_at <= now,
                            ),
                        ),
                        and_(
                            UserActorOutboundDelivery.status == "sending",
                            UserActorOutboundDelivery.claimed_at <= stale_before,
                        ),
                    ),
                )
                .order_by(
                    UserActorOutboundSource.user_id,
                    UserActorOutboundDelivery.target_endpoint_url,
                    # A terminal Delete must be evaluated before older pending/failed
                    # rows for the same endpoint so its explicit bypass policy can
                    # take effect.  _has_lower_unfinished still blocks in-flight
                    # sending work, while non-Delete lifecycle rows retain FIFO.
                    case(
                        (UserActorOutboundSource.source_kind == "content_delete", 0),
                        else_=1,
                    ),
                    UserActorOutboundSource.logical_version,
                    UserActorOutboundSource.id,
                    UserActorOutboundDelivery.id,
                )
            ).all()
        )

    @staticmethod
    def _within_claim_limits(
        *,
        endpoint_key: tuple[int, str],
        user_id: int,
        seen_endpoints: set[tuple[int, str]],
        claimed_per_user: dict[int, int],
        per_user_limit: int,
    ) -> bool:
        """Allow at most one endpoint version and a bounded count per user."""
        return (
            endpoint_key not in seen_endpoints
            and claimed_per_user.get(user_id, 0) < per_user_limit
        )

    @staticmethod
    def _newer_delete_status(
        session: Session,
        *,
        delivery: UserActorOutboundDelivery,
        source: UserActorOutboundSource,
    ) -> str | None:
        """Return the nearest newer terminal Delete status for content rows."""
        if source.source_kind not in {"content_create", "content_update"}:
            return None
        return session.scalar(
            select(UserActorOutboundDelivery.status)
            .join(UserActorOutboundSource)
            .where(
                UserActorOutboundSource.ordering_key == source.ordering_key,
                UserActorOutboundDelivery.target_endpoint_url
                == delivery.target_endpoint_url,
                UserActorOutboundSource.logical_version > source.logical_version,
                UserActorOutboundSource.source_kind == "content_delete",
                UserActorOutboundDelivery.status.in_(
                    ("pending", "failed", "sending", "delivered")
                ),
            )
            .order_by(UserActorOutboundSource.logical_version.desc())
            .limit(1)
        )

    @staticmethod
    def _has_newer_delivered(
        session: Session,
        *,
        delivery: UserActorOutboundDelivery,
        source: UserActorOutboundSource,
    ) -> bool:
        """Return whether a newer complete source already reached this endpoint."""
        return (
            session.scalar(
                select(UserActorOutboundDelivery.id)
                .join(UserActorOutboundSource)
                .where(
                    UserActorOutboundSource.ordering_key == source.ordering_key,
                    UserActorOutboundDelivery.target_endpoint_url
                    == delivery.target_endpoint_url,
                    (
                        UserActorOutboundSource.source_kind == "profile_update"
                        if source.source_kind == "profile_update"
                        else UserActorOutboundSource.source_kind == "content_delete"
                    ),
                    UserActorOutboundSource.logical_version > source.logical_version,
                    UserActorOutboundDelivery.status == "delivered",
                )
                .limit(1)
            )
            is not None
        )

    @staticmethod
    def _has_lower_unfinished(
        session: Session,
        *,
        delivery: UserActorOutboundDelivery,
        source: UserActorOutboundSource,
    ) -> bool:
        """Apply FIFO except that terminal Delete may bypass unsent failures."""
        blocking_statuses = (
            ("sending",)
            if source.source_kind == "content_delete"
            else ("pending", "failed", "sending")
        )
        return (
            session.scalar(
                select(UserActorOutboundDelivery.id)
                .join(UserActorOutboundSource)
                .where(
                    UserActorOutboundSource.ordering_key == source.ordering_key,
                    UserActorOutboundDelivery.target_endpoint_url
                    == delivery.target_endpoint_url,
                    UserActorOutboundSource.logical_version < source.logical_version,
                    UserActorOutboundDelivery.status.in_(blocking_statuses),
                )
                .limit(1)
            )
            is not None
        )

    @staticmethod
    def _mark_terminal_without_claim(
        delivery: UserActorOutboundDelivery, *, status: str
    ) -> None:
        """Finalize a candidate that must not be transported."""
        delivery.status = status
        delivery.claim_token = None
        delivery.claimed_at = None

    @staticmethod
    def _claim_delivery(
        *,
        delivery: UserActorOutboundDelivery,
        source: UserActorOutboundSource,
        user: User,
        represented_actor_ids: tuple[str, ...],
        claimed_at: datetime,
    ) -> ClaimedUserActorDelivery:
        """Transition one eligible candidate to sending and return its payload."""
        token = secrets.token_hex(16)
        delivery.status = "sending"
        delivery.claim_token = token
        delivery.claimed_at = claimed_at
        return ClaimedUserActorDelivery(
            delivery_id=delivery.id,
            source_id=source.id,
            user_id=source.user_id,
            logical_version=source.logical_version,
            activity_id=source.activity_id,
            actor_username=user.activitypub_username,
            actor_url=user.actor_url,
            target_endpoint_url=delivery.target_endpoint_url,
            represented_actor_ids=represented_actor_ids,
            activity_json=json.loads(source.activity_json),
            claim_token=token,
        )

    @staticmethod
    def _eligible_direct_actor_ids(
        session: Session, delivery: UserActorOutboundDelivery,
        source: UserActorOutboundSource,
    ) -> tuple[str, ...]:
        """Revalidate remote Group subscription provenance for direct dispatch.

        A URL alone cannot grant a send. This check binds the originally
        published object, accepted Follow incarnation and currently owned
        forum to the immutable target endpoint before each claim.
        """
        if not delivery.direct_authorizations_json or source.published_object_id is None:
            return ()
        try:
            records = json.loads(delivery.direct_authorizations_json)
        except (TypeError, ValueError):
            return ()
        published = session.get(PublishedActivityObject, source.published_object_id)
        if published is None or not isinstance(records, list):
            return ()
        if source.source_kind != 'content_delete' and published.deleted_at is not None:
            return ()
        authorized: set[str] = set()
        for record in records:
            if not isinstance(record, dict) or record.get('schemaVersion') != 1:
                continue
            if record.get('publishedObjectId') != published.id:
                continue
            if record.get('contextKind') == 'personal_feed_reply':
                # An inbound parent proves direct reply context, while the
                # current actor metadata binds the actual signed target inbox.
                root = session.scalar(select(PublishedActivityObject).where(
                    PublishedActivityObject.object_id == record.get('conversationRootObjectId'),
                    PublishedActivityObject.publication_context == 'personal_feed',
                    PublishedActivityObject.deleted_at.is_(None),
                ))
                parent = session.scalar(select(InboundConversationObject).where(
                    InboundConversationObject.object_id == record.get('parentObjectId'),
                    InboundConversationObject.conversation_root_object_id == record.get('conversationRootObjectId'),
                    InboundConversationObject.actor_id == record.get('recipientActorId'),
                    InboundConversationObject.deleted_at.is_(None),
                ))
                actor = session.scalar(select(RemoteActor).where(
                    RemoteActor.actor_url == record.get('recipientActorId'),
                ))
                if (published.publication_context == 'personal_feed'
                    and published.conversation_root_object_id == record.get('conversationRootObjectId')
                    and published.in_reply_to_object_id == record.get('parentObjectId')
                    and root is not None and parent is not None and actor is not None
                    and delivery.target_endpoint_url == record.get('targetEndpointUrl')
                    and (actor.shared_inbox_url or actor.inbox_url) == delivery.target_endpoint_url):
                    authorized.add(parent.actor_id)
                continue
            if record.get('contextKind') != 'remote_community_publication':
                continue
            current = session.get(ChannelCommunitySubscription, record.get('subscriptionId'))
            surface = session.get(ChannelSurface, record.get('sourceSurfaceId'))
            if (current is None or surface is None
                or current.status != 'accepted'
                or surface.remote_subscription_id != current.id
                or current.lemmy_community_actor_id != record.get('recipientActorId')
                or published.community_actor_url != record.get('recipientActorId')
                or current.follow_activity_id != record.get('followActivityId')
                or current.community_inbox_url != record.get('targetEndpointUrl')
                or delivery.target_endpoint_url != record.get('targetEndpointUrl')
                or published.source_channel_surface_id != surface.id):
                continue
            authorized.add(current.lemmy_community_actor_id)
        return tuple(sorted(authorized))

    @staticmethod
    def _eligible_actor_ids(
        session: Session, delivery_id: int, user_id: int
    ) -> tuple[str, ...]:
        """Return exact still-accepted actor incarnations for policy revalidation."""
        child = UserActorOutboundDeliveryFollower
        follower = UserActorFollower
        endpoint = func.coalesce(
            follower.remote_shared_inbox_url, follower.remote_inbox_url
        )
        rows = session.scalars(
            select(child.remote_actor_id)
            .join(follower, follower.id == child.relationship_id_snapshot)
            .where(
                child.delivery_id == delivery_id,
                follower.user_id == user_id,
                follower.status == "accepted",
                follower.remote_actor_id == child.remote_actor_id,
                endpoint == child.snapshotted_endpoint_url,
            )
            .order_by(child.relationship_id_snapshot)
        )
        return tuple(rows)
