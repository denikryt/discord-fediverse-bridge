from __future__ import annotations


from dataclasses import dataclass
from urllib.parse import urlsplit
from uuid import uuid4

from sqlalchemy import select

from ...models import (
    LocalCommunityRelayDelivery,
    LocalCommunityRelaySourceActivity,
    RemoteSubscriber,
    utcnow,
)
from ...activitypub_identity_urls import (
    build_community_announce_comment_activity_id,
    build_community_announce_post_activity_id,
)
from ...remote_delivery_endpoints import (
    RemoteDeliveryEndpointError,
    admit_remote_delivery_endpoint,
)
from .base import BaseRepository


@dataclass(frozen=True, slots=True)
class LocalCommunityRelayAttempt:
    """One immutable route/identity/generation admitted before a physical POST."""

    delivery_id: int
    target_remote_actor_id: str
    attempted_inbox_url: str
    relay_activity_id: str
    delivery_profile: str
    claimed_attempt_count: int



"""Local-community federation relay source and delivery persistence."""


class LocalCommunityRelayRepository(BaseRepository):
    """Persist community relay source/target state under SQLite writer locks."""

    def __init__(self, session_provider, immediate_session_provider):
        """Use writer-reserved transactions for source identity and send outcomes."""
        super().__init__(session_provider)
        self._immediate_session_provider = immediate_session_provider

    def _immediate_session(self):
        """Acquire the owning Database's serialized SQLite write transaction."""
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
    ) -> LocalCommunityRelaySourceActivity:
        """Load or create the immutable source activity row for relay fanout.

        Duplicate inbound deliveries may arrive after Discord mapping has
        already been persisted. Reusing the source row lets the fanout helper
        resume missing target rows without mutating the semantic source payload.
        """
        with self._immediate_session() as session:
            source = session.scalar(
                select(LocalCommunityRelaySourceActivity).where(
                    LocalCommunityRelaySourceActivity.local_community_id
                    == local_community_id,
                    LocalCommunityRelaySourceActivity.operation == operation,
                    LocalCommunityRelaySourceActivity.source_object_ap_id
                    == source_object_ap_id,
                    LocalCommunityRelaySourceActivity.source_activity_id
                    == source_activity_id,
                )
            )
            if source is not None:
                # A reused source key cannot acquire a new author, payload, or
                # profile-independent canonical meaning from a forged duplicate.
                if any((
                    source.object_kind != object_kind,
                    source.source_announce_id != source_announce_id,
                    source.origin_remote_actor_id != origin_remote_actor_id,
                    source.source_activity_json != source_activity_json,
                )):
                    raise ValueError("relay source identity conflict")
                return source
            source = LocalCommunityRelaySourceActivity(
                local_community_id=local_community_id,
                object_kind=object_kind,
                operation=operation,
                source_object_ap_id=source_object_ap_id,
                source_activity_id=source_activity_id,
                source_announce_id=source_announce_id,
                origin_remote_actor_id=origin_remote_actor_id,
                source_activity_json=source_activity_json,
            )
            session.add(source)
            session.flush()
            return source

    def list_local_community_relay_deliveries_for_source(
        self, source_activity_row_id: int
    ) -> list[LocalCommunityRelayDelivery]:
        """Return all target delivery rows attached to one source activity."""
        with self.session() as session:
            return list(
                session.scalars(
                    select(LocalCommunityRelayDelivery)
                    .where(
                        LocalCommunityRelayDelivery.source_activity_row_id
                        == source_activity_row_id
                    )
                    .order_by(
                        LocalCommunityRelayDelivery.created_at,
                        LocalCommunityRelayDelivery.id,
                    )
                )
            )

    def create_missing_local_community_relay_deliveries(
        self,
        *,
        source_activity: LocalCommunityRelaySourceActivity,
        targets: list[dict[str, str]],
    ) -> list[LocalCommunityRelayDelivery]:
        """Create pending delivery rows for targets that do not already exist.

        Existing delivered rows are intentionally returned alongside new rows so
        the fanout helper can decide whether to skip or retry each target
        without losing idempotency information.
        """
        with self._immediate_session() as session:
            persisted_source = session.get(LocalCommunityRelaySourceActivity, source_activity.id)
            if persisted_source is None:
                raise ValueError("relay source not persisted")
            existing = list(
                session.scalars(
                    select(LocalCommunityRelayDelivery).where(
                        LocalCommunityRelayDelivery.source_activity_row_id
                        == source_activity.id
                    )
                )
            )
            by_actor = {row.target_remote_actor_id: row for row in existing}
            for target in targets:
                remote_actor_id = target["remote_actor_id"]
                if remote_actor_id in by_actor:
                    existing_row = by_actor[remote_actor_id]
                    if existing_row.delivery_profile != target.get("delivery_profile", "threadiverse_group"):
                        # A repeated Follow does not reinterpret an already
                        # announced source under a different delivery profile.
                        raise ValueError("relay target delivery profile conflict")
                    continue
                row = LocalCommunityRelayDelivery(
                    source_activity_row_id=source_activity.id,
                    local_community_id=source_activity.local_community_id,
                    object_kind=source_activity.object_kind,
                    operation=source_activity.operation,
                    source_object_ap_id=source_activity.source_object_ap_id,
                    source_activity_id=source_activity.source_activity_id,
                    origin_remote_actor_id=source_activity.origin_remote_actor_id,
                    target_remote_actor_id=remote_actor_id,
                    target_inbox_url=target["remote_inbox_url"],
                    delivery_profile=target.get(
                        "delivery_profile", "threadiverse_group"
                    ),
                    status="pending",
                )
                session.add(row)
                session.flush()
                by_actor[remote_actor_id] = row
            return list(by_actor.values())

    def list_delivered_local_community_create_relay_targets(
        self,
        *,
        local_community_id: int,
        source_object_ap_id: str,
    ) -> list[LocalCommunityRelayDelivery]:
        """Return followers that successfully received the original create."""
        with self.session() as session:
            return list(
                session.scalars(
                    select(LocalCommunityRelayDelivery)
                    .where(
                        LocalCommunityRelayDelivery.local_community_id
                        == local_community_id,
                        LocalCommunityRelayDelivery.operation == "create",
                        LocalCommunityRelayDelivery.source_object_ap_id
                        == source_object_ap_id,
                        LocalCommunityRelayDelivery.status == "delivered",
                    )
                    .order_by(
                        LocalCommunityRelayDelivery.created_at,
                        LocalCommunityRelayDelivery.id,
                    )
                )
            )

    def prepare_local_community_relay_attempt(
        self,
        *,
        delivery_id: int,
        community_actor_url: str,
        community_slug: str,
        policy_service: object,
    ) -> LocalCommunityRelayAttempt | None:
        """Admit a current recipient route and reserve a source-shared Announce ID.

        No network request or rendering occurs while the writer reservation is
        held. An admitted generation is immutable until result completion.
        """
        with self._immediate_session() as session:
            delivery = session.get(LocalCommunityRelayDelivery, delivery_id)
            if delivery is None or delivery.status == "delivered":
                return None
            source = session.get(LocalCommunityRelaySourceActivity, delivery.source_activity_row_id)
            if source is None:
                raise ValueError("relay delivery source missing")
            if (source.local_community_id != delivery.local_community_id
                or source.operation != delivery.operation
                or source.source_object_ap_id != delivery.source_object_ap_id
                or source.source_activity_id != delivery.source_activity_id
                or source.origin_remote_actor_id != delivery.origin_remote_actor_id):
                raise ValueError("relay delivery source identity conflict")

            # Read *all* siblings, including delivered and currently ineligible
            # rows: they already own any externally published source identity.
            siblings = list(session.scalars(select(LocalCommunityRelayDelivery).where(
                LocalCommunityRelayDelivery.source_activity_row_id == source.id
            )))
            ids = {sibling.relay_activity_id for sibling in siblings if sibling.relay_activity_id}
            if len(ids) > 1:
                # MULTIPLATFORM_DISCORD_ADAPTER_TEMP_LEGACY_RELAY_IDS: Previously published recipient-specific Announce IDs cannot be silently unified; block new sends for this source until verified historical reconciliation preserves remote activity identity.
                delivery.status = "failed"
                delivery.last_error = "R7_F1_LEGACY_ID_CONFLICT"
                return None
            if ids:
                relay_id = next(iter(ids))
                # Historical grammars remain eligible, but cannot switch to
                # another community actor or to a different kind of source.
                if not relay_id.startswith(f"{community_actor_url.rstrip('/')}/activities/announce/"):
                    raise ValueError("relay activity identity is outside community namespace")
            else:
                parts = urlsplit(community_actor_url)
                origin = f"{parts.scheme}://{parts.netloc}"
                token = uuid4().hex
                if delivery.object_kind == "comment":
                    relay_id = build_community_announce_comment_activity_id(
                        origin=origin, slug=community_slug, activity_id=token
                    )
                else:
                    relay_id = build_community_announce_post_activity_id(
                        origin=origin, slug=community_slug, activity_id=token
                    )
            # Never rewrite a previously published identity, even for targets
            # that are no longer subscribed. NULL pending siblings can share it.
            for sibling in siblings:
                if sibling.relay_activity_id is None and sibling.status != "delivered":
                    sibling.relay_activity_id = relay_id
            if delivery.relay_activity_id is None:
                delivery.relay_activity_id = relay_id

            subscriber = session.scalar(select(RemoteSubscriber).where(
                RemoteSubscriber.local_community_id == delivery.local_community_id,
                RemoteSubscriber.remote_actor_id == delivery.target_remote_actor_id,
            ))
            if (subscriber is None or subscriber.status != "accepted"
                or (delivery.operation == "create" and delivery.target_remote_actor_id == source.origin_remote_actor_id)):
                delivery.status = "failed"
                delivery.last_error = "relay recipient is not currently accepted"
                return None
            try:
                inbox = admit_remote_delivery_endpoint(
                    subscriber.remote_inbox_url, local_origin=community_actor_url
                )
            except RemoteDeliveryEndpointError:
                delivery.status = "failed"
                delivery.last_error = "relay recipient endpoint rejected"
                return None
            snapshot = policy_service.snapshot_in_session(session)
            if not (snapshot.federation_decision(inbox).allowed
                    and snapshot.federation_decision(subscriber.remote_actor_id).allowed):
                delivery.status = "failed"
                delivery.last_error = "relay recipient federation policy rejected"
                return None
            delivery.target_inbox_url = inbox
            delivery.attempt_count += 1
            delivery.last_attempted_at = utcnow()
            delivery.last_error = None
            return LocalCommunityRelayAttempt(
                delivery_id=delivery.id,
                target_remote_actor_id=delivery.target_remote_actor_id,
                attempted_inbox_url=inbox,
                relay_activity_id=relay_id,
                delivery_profile=delivery.delivery_profile,
                claimed_attempt_count=delivery.attempt_count,
            )

    def mark_local_community_relay_delivery_result(
        self,
        *,
        delivery_id: int,
        status: str,
        relay_activity_id: str,
        attempted_inbox_url: str,
        claimed_attempt_count: int,
        policy_service: object | None = None,
        community_actor_url: str | None = None,
        error: str | None = None,
    ) -> str:
        """Commit only the outcome belonging to the current admitted attempt."""
        if status not in {"delivered", "failed"}:
            raise ValueError("relay result must be delivered or failed")
        with self._immediate_session() as session:
            row = session.get(LocalCommunityRelayDelivery, delivery_id)
            if row is None:
                return "ignored_missing"
            if row.status == "delivered":
                return "ignored_already_delivered"
            if row.relay_activity_id != relay_activity_id:
                raise ValueError("relay activity ID cannot be replaced by a gateway outcome")
            if (row.attempt_count != claimed_attempt_count
                or row.target_inbox_url != attempted_inbox_url):
                return "ignored_stale"
            subscriber = session.scalar(select(RemoteSubscriber).where(
                RemoteSubscriber.local_community_id == row.local_community_id,
                RemoteSubscriber.remote_actor_id == row.target_remote_actor_id,
            ))
            if subscriber is None or subscriber.status != "accepted":
                row.status = "failed"
                row.last_error = "relay recipient is not currently accepted"
                return row.status
            try:
                current_inbox = admit_remote_delivery_endpoint(
                    subscriber.remote_inbox_url, local_origin=community_actor_url
                )
            except RemoteDeliveryEndpointError:
                row.status = "failed"
                row.last_error = "relay recipient endpoint rejected"
                return row.status
            if policy_service is not None:
                snapshot = policy_service.snapshot_in_session(session)
                if not (snapshot.federation_decision(current_inbox).allowed
                        and snapshot.federation_decision(subscriber.remote_actor_id).allowed):
                    row.status = "failed"
                    row.last_error = "relay recipient federation policy rejected"
                    return row.status
            if current_inbox != attempted_inbox_url:
                # A response to route A is not proof of receipt at route B.
                row.target_inbox_url = current_inbox
                row.status = "pending"
                row.last_error = None
                return row.status
            row.status = status
            row.last_error = error if status == "failed" else None
            return row.status

    def get_local_community_relay_source_activity(
        self,
        *,
        local_community_id: int,
        operation: str,
        source_object_ap_id: str,
        source_activity_id: str,
    ) -> LocalCommunityRelaySourceActivity | None:
        """Load one source activity row by its idempotency identity."""
        with self.session() as session:
            return session.scalar(
                select(LocalCommunityRelaySourceActivity).where(
                    LocalCommunityRelaySourceActivity.local_community_id
                    == local_community_id,
                    LocalCommunityRelaySourceActivity.operation == operation,
                    LocalCommunityRelaySourceActivity.source_object_ap_id
                    == source_object_ap_id,
                    LocalCommunityRelaySourceActivity.source_activity_id
                    == source_activity_id,
                )
            )
