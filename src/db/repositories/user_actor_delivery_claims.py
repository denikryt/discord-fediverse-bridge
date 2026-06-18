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
                represented_actor_ids = self._eligible_actor_ids(
                    session, delivery.id, source.user_id
                )
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
