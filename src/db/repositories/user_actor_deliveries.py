"""Persistence operations for durable User Actor outbound delivery."""

from __future__ import annotations

import json
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from sqlalchemy import and_, case, delete, func, or_, select, update
from sqlalchemy.orm import Session

from ...models import (
    User,
    UserActorFollower,
    UserActorOutboundDelivery,
    UserActorOutboundDeliveryFollower,
    UserActorOutboundSource,
    UserActorOutboundSourceMedia,
)
from .base import BaseRepository


@dataclass(frozen=True, slots=True)
class ClaimedUserActorDelivery:
    """Describe one exclusively claimed endpoint delivery."""

    delivery_id: int
    source_id: int
    user_id: int
    logical_version: int
    activity_id: str
    actor_username: str
    actor_url: str
    target_endpoint_url: str
    represented_actor_ids: tuple[str, ...]
    activity_json: dict
    claim_token: str


@dataclass(frozen=True, slots=True)
class DeliveryDiagnostic:
    """Expose bounded non-secret delivery state for local operators."""

    delivery_id: int
    source_id: int
    username: str
    source_kind: str
    logical_version: int
    activity_id: str
    target_endpoint_url: str
    represented_actor_ids: tuple[str, ...]
    status: str
    attempt_count: int
    last_attempted_at: datetime | None
    next_attempt_at: datetime | None
    last_error: str | None


@dataclass(frozen=True, slots=True)
class DeliveryRetryResult:
    """Summarize one compare-and-set retry reset operation."""

    matched: int
    reset: int
    skipped: int
    remaining: int
    outcome: str = "success"


class UserActorDeliveryRepository(BaseRepository):
    """Own immutable sources, endpoint claims, retries, and outcomes."""

    ERROR_LIMIT = 500

    def __init__(self, session_provider, immediate_session_provider) -> None:
        """Bind ordinary and writer-reserved transaction providers."""
        super().__init__(session_provider)
        self._immediate_session_provider = immediate_session_provider

    @staticmethod
    def validate_endpoint(endpoint: str) -> str:
        """Accept only normalized absolute HTTPS inbox endpoints."""
        parsed = urlparse(endpoint)
        if parsed.scheme != "https" or not parsed.netloc or parsed.fragment:
            raise ValueError("invalid follower delivery endpoint")
        return endpoint

    @staticmethod
    def insert_source_graph(
        session: Session,
        *,
        user_id: int,
        logical_version: int,
        activity_id: str,
        profile_snapshot_json: str,
        followers: list[UserActorFollower],
        media_refs: list[tuple[str, int]],
    ) -> UserActorOutboundSource:
        """Insert one source and its fixed audience inside the mutation transaction."""
        source = UserActorOutboundSource(
            user_id=user_id,
            source_kind="profile_update",
            logical_version=logical_version,
            activity_id=activity_id,
            ordering_key=f"profile:{user_id}",
            snapshot_json=profile_snapshot_json,
            state="render_pending",
        )
        session.add(source)
        session.flush()

        UserActorDeliveryRepository._insert_audience(session, source.id, followers)
        for slot, asset_id in media_refs:
            session.add(
                UserActorOutboundSourceMedia(
                    source_id=source.id, media_asset_id=asset_id, slot_kind=slot
                )
            )
        session.flush()
        return source

    @staticmethod
    def insert_ready_content_source(
        session: Session,
        *,
        user_id: int,
        source_kind: str,
        logical_version: int,
        activity_id: str,
        published_object_id: int,
        object_id: str,
        activity_json: dict,
        snapshot: dict,
        followers: list[UserActorFollower],
        direct_target_inbox_urls: list[str] | None = None,
    ) -> UserActorOutboundSource:
        """Insert one already-rendered content source and immutable audience."""
        source = UserActorOutboundSource(
            user_id=user_id,
            source_kind=source_kind,
            ordering_key=f"content:{object_id}",
            logical_version=logical_version,
            activity_id=activity_id,
            published_object_id=published_object_id,
            snapshot_json=json.dumps(snapshot, separators=(",", ":"), sort_keys=True),
            activity_json=json.dumps(
                activity_json, separators=(",", ":"), sort_keys=True
            ),
            state="ready",
        )
        session.add(source)
        session.flush()
        UserActorDeliveryRepository._insert_audience(session, source.id, followers)
        existing_endpoints = {
            UserActorDeliveryRepository.validate_endpoint(
                follower.remote_shared_inbox_url or follower.remote_inbox_url
            )
            for follower in followers
        }
        for endpoint_url in sorted(set(direct_target_inbox_urls or [])):
            endpoint = UserActorDeliveryRepository.validate_endpoint(endpoint_url)
            if endpoint not in existing_endpoints:
                session.add(
                    UserActorOutboundDelivery(
                        source_id=source.id,
                        target_endpoint_url=endpoint,
                        status="pending",
                    )
                )
                existing_endpoints.add(endpoint)
        return source

    @staticmethod
    def _insert_audience(
        session: Session, source_id: int, followers: list[UserActorFollower]
    ) -> None:
        """Snapshot accepted follower incarnations with shared-inbox deduplication."""
        grouped: dict[str, list[UserActorFollower]] = {}
        for follower in followers:
            endpoint = UserActorDeliveryRepository.validate_endpoint(
                follower.remote_shared_inbox_url or follower.remote_inbox_url
            )
            grouped.setdefault(endpoint, []).append(follower)
        for endpoint in sorted(grouped):
            delivery = UserActorOutboundDelivery(
                source_id=source_id, target_endpoint_url=endpoint, status="pending"
            )
            session.add(delivery)
            session.flush()
            for follower in sorted(grouped[endpoint], key=lambda item: item.id):
                session.add(
                    UserActorOutboundDeliveryFollower(
                        delivery_id=delivery.id,
                        relationship_id_snapshot=follower.id,
                        remote_actor_id=follower.remote_actor_id,
                        snapshotted_endpoint_url=endpoint,
                    )
                )

    def get_source(self, source_id: int) -> UserActorOutboundSource | None:
        """Load one durable source without consulting mutable profile state."""
        with self.session() as session:
            return session.get(UserActorOutboundSource, source_id)

    def due_source_ids(self, limit: int) -> list[int]:
        """Return bounded render work in deterministic version order."""
        now = datetime.now(timezone.utc)
        with self.session() as session:
            return list(
                session.scalars(
                    select(UserActorOutboundSource.id)
                    .where(
                        UserActorOutboundSource.state.in_(
                            ("render_pending", "render_failed")
                        ),
                        or_(
                            UserActorOutboundSource.next_attempt_at.is_(None),
                            UserActorOutboundSource.next_attempt_at <= now,
                        ),
                    )
                    .order_by(
                        UserActorOutboundSource.user_id,
                        UserActorOutboundSource.logical_version,
                        UserActorOutboundSource.id,
                    )
                    .limit(limit)
                )
            )

    def record_rendered(self, source_id: int, activity_json: dict) -> bool:
        """Write canonical activity JSON exactly once through compare-and-set."""
        encoded = json.dumps(activity_json, separators=(",", ":"), sort_keys=True)
        now = datetime.now(timezone.utc)
        with self.session() as session:
            result = session.execute(
                update(UserActorOutboundSource)
                .where(
                    UserActorOutboundSource.id == source_id,
                    UserActorOutboundSource.activity_json.is_(None),
                    UserActorOutboundSource.state.in_(
                        ("render_pending", "render_failed")
                    ),
                )
                .values(
                    activity_json=encoded,
                    state="ready",
                    last_error=None,
                    next_attempt_at=None,
                    last_render_attempted_at=now,
                    render_attempt_count=UserActorOutboundSource.render_attempt_count
                    + 1,
                )
            )
            return result.rowcount == 1

    def record_render_failure(self, source_id: int, error: str) -> None:
        """Keep render intent durable with bounded exponential backoff."""
        now = datetime.now(timezone.utc)
        with self.session() as session:
            source = session.get(UserActorOutboundSource, source_id)
            if source is None or source.state == "ready":
                return
            source.render_attempt_count += 1
            source.last_render_attempted_at = now
            source.state = "render_failed"
            source.last_error = self._bounded_error(error)
            source.next_attempt_at = now + timedelta(
                seconds=min(300, 2 ** min(source.render_attempt_count, 8))
            )

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
        """Return whether a newer complete profile already reached this endpoint."""
        return (
            session.scalar(
                select(UserActorOutboundDelivery.id)
                .join(UserActorOutboundSource)
                .where(
                    UserActorOutboundSource.ordering_key == source.ordering_key,
                    UserActorOutboundDelivery.target_endpoint_url
                    == delivery.target_endpoint_url,
                    UserActorOutboundSource.logical_version > source.logical_version,
                    (
                        UserActorOutboundSource.source_kind == "profile_update"
                        if source.source_kind == "profile_update"
                        else UserActorOutboundSource.source_kind == "content_delete"
                    ),
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

    def mark_ineligible(self, delivery_id: int, claim_token: str, reason: str) -> None:
        """Terminally suppress one currently claimed endpoint without transport."""
        with self.session() as session:
            delivery = session.scalar(
                select(UserActorOutboundDelivery).where(
                    UserActorOutboundDelivery.id == delivery_id,
                    UserActorOutboundDelivery.status == "sending",
                    UserActorOutboundDelivery.claim_token == claim_token,
                )
            )
            if delivery is None:
                return
            delivery.status = "ineligible"
            delivery.claim_token = None
            delivery.claimed_at = None
            delivery.last_error = self._bounded_error(reason)
            delivery.next_attempt_at = None

    def release_unsubmitted_claim(
        self, delivery_id: int, claim_token: str, error: str
    ) -> None:
        """Return a pre-transport claim to retryable failure without an attempt."""
        now = datetime.now(timezone.utc)
        with self.session() as session:
            delivery = session.scalar(
                select(UserActorOutboundDelivery).where(
                    UserActorOutboundDelivery.id == delivery_id,
                    UserActorOutboundDelivery.status == "sending",
                    UserActorOutboundDelivery.claim_token == claim_token,
                )
            )
            if delivery is None:
                return
            delivery.status = "failed"
            delivery.claim_token = None
            delivery.claimed_at = None
            delivery.last_error = self._bounded_error(error)
            delivery.next_attempt_at = now + timedelta(seconds=15)

    def mark_submitted(self, delivery_id: int, claim_token: str) -> bool:
        """Count one actual gateway submission for the currently owned claim."""
        now = datetime.now(timezone.utc)
        with self.session() as session:
            result = session.execute(
                update(UserActorOutboundDelivery)
                .where(
                    UserActorOutboundDelivery.id == delivery_id,
                    UserActorOutboundDelivery.status == "sending",
                    UserActorOutboundDelivery.claim_token == claim_token,
                )
                .values(
                    attempt_count=UserActorOutboundDelivery.attempt_count + 1,
                    last_attempted_at=now,
                )
            )
            return result.rowcount == 1

    def record_outcome(
        self, delivery_id: int, claim_token: str, *, ok: bool, error: str | None = None
    ) -> None:
        """Complete only the currently owned claim and preserve retryability."""
        now = datetime.now(timezone.utc)
        with self.session() as session:
            delivery = session.scalar(
                select(UserActorOutboundDelivery).where(
                    UserActorOutboundDelivery.id == delivery_id,
                    UserActorOutboundDelivery.status == "sending",
                    UserActorOutboundDelivery.claim_token == claim_token,
                )
            )
            if delivery is None:
                return
            delivery.claim_token = None
            delivery.claimed_at = None
            if ok:
                delivery.status = "delivered"
                delivery.delivered_at = now
                delivery.last_error = None
                delivery.next_attempt_at = None
                source = session.get(UserActorOutboundSource, delivery.source_id)
                if source is not None and source.source_kind == "content_delete":
                    older_ids = session.scalars(
                        select(UserActorOutboundDelivery.id)
                        .join(UserActorOutboundSource)
                        .where(
                            UserActorOutboundSource.ordering_key == source.ordering_key,
                            UserActorOutboundSource.logical_version
                            < source.logical_version,
                            UserActorOutboundDelivery.target_endpoint_url
                            == delivery.target_endpoint_url,
                            UserActorOutboundDelivery.status.in_(("pending", "failed")),
                        )
                    ).all()
                    if older_ids:
                        session.execute(
                            update(UserActorOutboundDelivery)
                            .where(UserActorOutboundDelivery.id.in_(older_ids))
                            .values(
                                status="superseded",
                                claim_token=None,
                                claimed_at=None,
                                next_attempt_at=None,
                                last_error=None,
                            )
                        )
            else:
                delivery.status = "failed"
                delivery.last_error = self._bounded_error(error or "delivery failed")
                delivery.next_attempt_at = now + timedelta(
                    seconds=min(300, 2 ** min(delivery.attempt_count, 8))
                )

    def release_terminal_source_media(self, source_id: int) -> int:
        """Release source media only after rendering and all targets are terminal."""
        with self.session() as session:
            source = session.get(UserActorOutboundSource, source_id)
            if source is None or source.state != "ready":
                return 0
            unfinished = session.scalar(
                select(UserActorOutboundDelivery.id)
                .where(
                    UserActorOutboundDelivery.source_id == source_id,
                    UserActorOutboundDelivery.status.in_(
                        ("pending", "sending", "failed")
                    ),
                )
                .limit(1)
            )
            if unfinished is not None:
                return 0
            result = session.execute(
                delete(UserActorOutboundSourceMedia).where(
                    UserActorOutboundSourceMedia.source_id == source_id
                )
            )
            return int(result.rowcount or 0)

    def list_diagnostics(
        self, *, username: str | None, status: str | None, limit: int
    ) -> list[DeliveryDiagnostic]:
        """List bounded non-secret endpoint and audience diagnostics for operators."""
        if limit < 1 or limit > 500:
            raise ValueError("limit must be between 1 and 500")
        with self.session() as session:
            stmt = (
                select(UserActorOutboundDelivery, UserActorOutboundSource, User)
                .join(
                    UserActorOutboundSource,
                    UserActorOutboundSource.id == UserActorOutboundDelivery.source_id,
                )
                .join(User, User.id == UserActorOutboundSource.user_id)
                .order_by(UserActorOutboundDelivery.id)
                .limit(limit)
            )
            if username is not None:
                stmt = stmt.where(User.activitypub_username == username)
            if status is not None:
                stmt = stmt.where(UserActorOutboundDelivery.status == status)
            rows = session.execute(stmt).all()
            diagnostics: list[DeliveryDiagnostic] = []
            for delivery, source, user in rows:
                actor_ids = tuple(
                    session.scalars(
                        select(UserActorOutboundDeliveryFollower.remote_actor_id)
                        .where(
                            UserActorOutboundDeliveryFollower.delivery_id == delivery.id
                        )
                        .order_by(UserActorOutboundDeliveryFollower.id)
                    )
                )
                diagnostics.append(
                    DeliveryDiagnostic(
                        delivery_id=delivery.id,
                        source_id=source.id,
                        username=user.activitypub_username,
                        source_kind=source.source_kind,
                        logical_version=source.logical_version,
                        activity_id=source.activity_id,
                        target_endpoint_url=delivery.target_endpoint_url,
                        represented_actor_ids=actor_ids,
                        status=delivery.status,
                        attempt_count=delivery.attempt_count,
                        last_attempted_at=delivery.last_attempted_at,
                        next_attempt_at=delivery.next_attempt_at,
                        last_error=self._bounded_error(delivery.last_error)
                        if delivery.last_error
                        else None,
                    )
                )
            return diagnostics

    def retry_failed_by_delivery_id(
        self, delivery_id: int, username: str | None = None
    ) -> DeliveryRetryResult:
        """Reset one failed delivery when its optional owner matches."""
        return self._retry_failed(
            delivery_id=delivery_id, source_id=None, username=username
        )

    def retry_failed_by_source_id(
        self, source_id: int, username: str | None = None
    ) -> DeliveryRetryResult:
        """Reset all failed endpoints for one exact source identity."""
        return self._retry_failed(
            delivery_id=None, source_id=source_id, username=username
        )

    def _retry_failed(
        self, *, delivery_id: int | None, source_id: int | None, username: str | None
    ) -> DeliveryRetryResult:
        """Reset failed rows while distinguishing lookup and ownership failures."""
        with self._immediate_session_provider() as session:
            base = (
                select(UserActorOutboundDelivery.id, User.activitypub_username)
                .select_from(UserActorOutboundDelivery)
                .join(
                    UserActorOutboundSource,
                    UserActorOutboundSource.id == UserActorOutboundDelivery.source_id,
                )
                .join(User, User.id == UserActorOutboundSource.user_id)
            )
            if delivery_id is not None:
                base = base.where(UserActorOutboundDelivery.id == delivery_id)
            if source_id is not None:
                base = base.where(UserActorOutboundDelivery.source_id == source_id)
            rows = list(session.execute(base.order_by(UserActorOutboundDelivery.id)))
            if not rows:
                return DeliveryRetryResult(0, 0, 0, 0, "not_found")
            if username is not None and any(owner != username for _id, owner in rows):
                return DeliveryRetryResult(
                    len(rows), 0, len(rows), 0, "ownership_mismatch"
                )
            ids = [row_id for row_id, _owner in rows]
            result = session.execute(
                update(UserActorOutboundDelivery)
                .where(
                    UserActorOutboundDelivery.id.in_(ids),
                    UserActorOutboundDelivery.status == "failed",
                )
                .values(
                    status="pending",
                    claim_token=None,
                    claimed_at=None,
                    next_attempt_at=None,
                    last_error=None,
                )
            )
            reset = int(result.rowcount or 0)
            if reset == 0:
                return DeliveryRetryResult(len(ids), 0, len(ids), 0, "not_retryable")
            return DeliveryRetryResult(len(ids), reset, len(ids) - reset, 0, "success")

    def retry_failed_batch(self, *, username: str, limit: int) -> DeliveryRetryResult:
        """Reset one deterministic bounded batch of failed deliveries."""
        if limit < 1 or limit > 100:
            raise ValueError("limit must be between 1 and 100")
        with self._immediate_session_provider() as session:
            base = (
                select(UserActorOutboundDelivery.id)
                .join(UserActorOutboundSource)
                .join(User)
                .where(
                    User.activitypub_username == username,
                    UserActorOutboundDelivery.status == "failed",
                )
                .order_by(UserActorOutboundDelivery.id)
            )
            ids = list(session.scalars(base.limit(limit)))
            matched = int(
                session.scalar(select(func.count()).select_from(base.subquery())) or 0
            )
            if ids:
                session.execute(
                    update(UserActorOutboundDelivery)
                    .where(
                        UserActorOutboundDelivery.id.in_(ids),
                        UserActorOutboundDelivery.status == "failed",
                    )
                    .values(
                        status="pending",
                        claim_token=None,
                        claimed_at=None,
                        next_attempt_at=None,
                        last_error=None,
                    )
                )
            reset = len(ids)
            # Rows beyond this bounded batch remain failed; they are not skipped.
            return DeliveryRetryResult(
                matched=matched,
                reset=reset,
                skipped=0,
                remaining=max(0, matched - reset),
                outcome="success",
            )

    def source_summary(self, source_id: int) -> tuple[int, int, int]:
        """Return delivered, failed/pending, and total endpoint counts."""
        with self.session() as session:
            rows = session.execute(
                select(UserActorOutboundDelivery.status, func.count())
                .where(UserActorOutboundDelivery.source_id == source_id)
                .group_by(UserActorOutboundDelivery.status)
            ).all()
        counts = {status: count for status, count in rows}
        total = sum(counts.values())
        delivered = counts.get("delivered", 0)
        outstanding = sum(
            counts.get(status, 0) for status in ("pending", "sending", "failed")
        )
        return delivered, outstanding, total

    @classmethod
    def _bounded_error(cls, error: str) -> str:
        """Normalize and redact diagnostics before bounded persistence."""
        redacted = re.sub(
            r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----",
            "[redacted private key]",
            error,
            flags=re.DOTALL | re.IGNORECASE,
        )
        redacted = re.sub(r"(?i)bearer\s+[^\s,;]+", "Bearer [redacted]", redacted)
        return " ".join(redacted.split())[: cls.ERROR_LIMIT]
