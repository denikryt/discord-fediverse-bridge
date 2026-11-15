"""Persist immutable local Group relay sources and fenced delivery attempts.

A Create is identified by its tenant and original AP object, not by whichever
Announce delivered it. One committed target-attempt claim precedes any HTTP
call; unknown HTTP outcomes are never considered safe automatic retries.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy import select

from ...activitypub_models import activity_actor_id
from ...models import (
    LocalCommunityRelayDelivery,
    LocalCommunityRelaySourceActivity,
    utcnow,
)
from .base import BaseRepository


class RelayIdentityConflict(ValueError):
    """Reject an envelope contradicting the first verified relay source."""


@dataclass(frozen=True, slots=True)
class RelayTargetAttempt:
    """A committed, exclusive target attempt with a fixed ActivityPub ID."""

    delivery: LocalCommunityRelayDelivery
    token: str


class LocalCommunityRelayRepository(BaseRepository):
    """Own one per-tenant Create source and CAS-claimed target delivery state."""

    def __init__(self, session_provider, immediate_session_provider=None) -> None:
        """Use the Database writer transaction to serialize canonical and target claims."""
        super().__init__(session_provider)
        self._immediate_session_provider = immediate_session_provider or session_provider

    def immediate_session(self):
        """Return one writer-reserved transaction before any external side effects."""
        return self._immediate_session_provider()

    def get_or_create_local_community_relay_source_activity(
        self,
        *,
        local_community_id: int,
        object_kind: str,
        operation: str,
        source_object_ap_id: str,
        source_activity_id: str,
        source_announce_id: str | None,
        origin_remote_actor_id: str,
        source_activity_json: dict,
        canonical_relay_projection_json: dict | None = None,
    ) -> LocalCommunityRelaySourceActivity:
        """Freeze first Create payload/owner/origin across differing Announce IDs.

        Updates and Deletes are separate revisions keyed by their *signed*
        activity ID. For Create, only the original object and tenant matter;
        duplicate envelopes cannot overwrite that first durable source.
        """
        if operation not in ("create", "update", "delete"):
            raise RelayIdentityConflict("unknown relay operation")
        with self.immediate_session() as session:
            predicate = (
                LocalCommunityRelaySourceActivity.local_community_id == local_community_id,
                LocalCommunityRelaySourceActivity.operation == operation,
                LocalCommunityRelaySourceActivity.source_object_ap_id == source_object_ap_id,
            )
            if operation != "create":
                predicate += (
                    LocalCommunityRelaySourceActivity.source_activity_id == source_activity_id,
                )
            source = session.scalar(select(LocalCommunityRelaySourceActivity).where(*predicate))
            if source is not None:
                # Every variant of one Create must preserve the verified owner,
                # original Service and object kind; a later body never replaces
                # the frozen source or the first normalized relay projection.
                original_actor = activity_actor_id(source.source_activity_json.get("actor"))
                new_actor = activity_actor_id(source_activity_json.get("actor"))
                if (
                    source.object_kind != object_kind
                    or source.origin_remote_actor_id != origin_remote_actor_id
                    or not original_actor
                    or original_actor != new_actor
                ):
                    raise RelayIdentityConflict("immutable relay source owner/origin mismatch")
                return source
            actor = activity_actor_id(source_activity_json.get("actor"))
            if not actor:
                raise RelayIdentityConflict("relay source must contain verified author actor")
            source_object = source_activity_json.get("object")
            first_object_id = (
                source_object.get("id") if isinstance(source_object, dict)
                else source_object if isinstance(source_object, str) else None
            )
            if first_object_id != source_object_ap_id:
                # The signed source, not an unrelated normalized URL, must
                # establish exactly which AP object the relay is publishing.
                raise RelayIdentityConflict("relay source object differs from canonical identity")
            if (operation == "create" and isinstance(source_object, dict)
                    and source_object.get("attributedTo") != actor):
                raise RelayIdentityConflict("Create attributedTo differs from verified owner")
            source = LocalCommunityRelaySourceActivity(
                local_community_id=local_community_id,
                object_kind=object_kind,
                operation=operation,
                source_object_ap_id=source_object_ap_id,
                source_activity_id=source_activity_id,
                source_announce_id=source_announce_id,
                origin_remote_actor_id=origin_remote_actor_id,
                source_activity_json=source_activity_json,
                canonical_relay_projection_json=canonical_relay_projection_json,
            )
            session.add(source)
            session.flush()
            return source

    def list_local_community_relay_deliveries_for_source(
        self, source_activity_row_id: int
    ) -> list[LocalCommunityRelayDelivery]:
        """Return committed target records for one immutable relay source."""
        with self.session() as session:
            return list(session.scalars(
                select(LocalCommunityRelayDelivery).where(
                    LocalCommunityRelayDelivery.source_activity_row_id == source_activity_row_id
                ).order_by(LocalCommunityRelayDelivery.created_at, LocalCommunityRelayDelivery.id)
            ))

    def create_missing_local_community_relay_deliveries(
        self,
        *,
        source_activity: LocalCommunityRelaySourceActivity,
        targets: list[dict[str, str]],
    ) -> list[LocalCommunityRelayDelivery]:
        """Reserve target rows once, without changing original inbox/profile evidence."""
        with self.immediate_session() as session:
            existing = list(session.scalars(select(LocalCommunityRelayDelivery).where(
                LocalCommunityRelayDelivery.source_activity_row_id == source_activity.id
            )))
            by_actor = {row.target_remote_actor_id: row for row in existing}
            for target in targets:
                actor_id = target["remote_actor_id"]
                if actor_id in by_actor:
                    continue
                row = LocalCommunityRelayDelivery(
                    source_activity_row_id=source_activity.id,
                    local_community_id=source_activity.local_community_id,
                    object_kind=source_activity.object_kind,
                    operation=source_activity.operation,
                    source_object_ap_id=source_activity.source_object_ap_id,
                    source_activity_id=source_activity.source_activity_id,
                    origin_remote_actor_id=source_activity.origin_remote_actor_id,
                    target_remote_actor_id=actor_id,
                    target_inbox_url=target["remote_inbox_url"],
                    delivery_profile=target.get("delivery_profile", "threadiverse_group"),
                    status="pending",
                )
                session.add(row)
                session.flush()
                by_actor[actor_id] = row
            return list(by_actor.values())

    def claim_local_community_relay_target(
        self, *, delivery_id: int, relay_activity_id: str,
    ) -> RelayTargetAttempt | None:
        """Atomically claim pending/proven-unsent delivery before HTTP.

        A `sending` attempt might have reached the network, including across
        restart, so only pending or explicitly safe `failed` may be retried.
        """
        with self.immediate_session() as session:
            row = session.get(LocalCommunityRelayDelivery, delivery_id)
            if row is None or row.status not in ("pending", "failed"):
                return None
            if row.relay_activity_id is not None and row.relay_activity_id != relay_activity_id:
                raise RelayIdentityConflict("relay target activity ID changed across retry")
            token = uuid4().hex
            row.status = "sending"
            row.relay_activity_id = relay_activity_id
            row.attempt_token = token
            row.attempt_started_at = utcnow()
            row.last_attempted_at = row.attempt_started_at
            row.attempt_count += 1
            row.last_error = None
            session.flush()
            return RelayTargetAttempt(row, token)

    def finish_local_community_relay_target(
        self, *, delivery_id: int, token: str, status: str,
        error: str | None = None,
    ) -> bool:
        """Fence stale callbacks; ambiguous sends stay non-retryable `unknown`."""
        if status not in ("delivered", "failed", "unknown"):
            raise ValueError("invalid relay delivery completion")
        with self.immediate_session() as session:
            row = session.get(LocalCommunityRelayDelivery, delivery_id)
            if row is None or row.status != "sending" or row.attempt_token != token:
                return False
            row.status = status
            row.last_error = (error or None)[:1024] if error else None
            # Keep the original token/ID for audit and stale callback fencing.
            session.flush()
            return True

    def list_delivered_local_community_create_relay_targets(
        self, *, local_community_id: int, source_object_ap_id: str,
    ) -> list[LocalCommunityRelayDelivery]:
        """Target only followers known to have received the original Create."""
        with self.session() as session:
            return list(session.scalars(select(LocalCommunityRelayDelivery).where(
                LocalCommunityRelayDelivery.local_community_id == local_community_id,
                LocalCommunityRelayDelivery.operation == "create",
                LocalCommunityRelayDelivery.source_object_ap_id == source_object_ap_id,
                LocalCommunityRelayDelivery.status == "delivered",
            ).order_by(LocalCommunityRelayDelivery.created_at, LocalCommunityRelayDelivery.id)))

    def get_local_community_relay_source_activity(
        self, *, local_community_id: int, operation: str,
        source_object_ap_id: str, source_activity_id: str,
    ) -> LocalCommunityRelaySourceActivity | None:
        """Find the original immutable revision or Create by its canonical key."""
        with self.session() as session:
            predicate = (
                LocalCommunityRelaySourceActivity.local_community_id == local_community_id,
                LocalCommunityRelaySourceActivity.operation == operation,
                LocalCommunityRelaySourceActivity.source_object_ap_id == source_object_ap_id,
            )
            if operation != "create":
                predicate += (LocalCommunityRelaySourceActivity.source_activity_id == source_activity_id,)
            return session.scalar(select(LocalCommunityRelaySourceActivity).where(*predicate))
