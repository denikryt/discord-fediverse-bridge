from __future__ import annotations

from datetime import datetime

from sqlalchemy import select

from ...models import (
    LocalCommunity,
    LocalSubscriber,
    LocalCommunityMessage,
    LocalCommunityMessageSurface,
    LocalCommunityRelayDelivery,
    LocalCommunityRelaySourceActivity,
    LocalCommunityThread,
    LocalCommunityThreadSurface,
    RemoteSubscriber,
    utcnow,
)
from .base import BaseRepository


"""Local-community federation relay source and delivery persistence."""


class LocalCommunityRelayRepository(BaseRepository):
    """Persist the local community relay domain."""

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
            with self.session() as session:
                source = session.scalar(
                    select(LocalCommunityRelaySourceActivity).where(
                        LocalCommunityRelaySourceActivity.local_community_id == local_community_id,
                        LocalCommunityRelaySourceActivity.operation == operation,
                        LocalCommunityRelaySourceActivity.source_object_ap_id == source_object_ap_id,
                        LocalCommunityRelaySourceActivity.source_activity_id == source_activity_id,
                    )
                )
                if source is not None:
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
                        select(LocalCommunityRelayDelivery).where(
                            LocalCommunityRelayDelivery.source_activity_row_id == source_activity_row_id
                        ).order_by(LocalCommunityRelayDelivery.created_at, LocalCommunityRelayDelivery.id)
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
            with self.session() as session:
                existing = list(
                    session.scalars(
                        select(LocalCommunityRelayDelivery).where(
                            LocalCommunityRelayDelivery.source_activity_row_id == source_activity.id
                        )
                    )
                )
                by_actor = {row.target_remote_actor_id: row for row in existing}
                for target in targets:
                    remote_actor_id = target["remote_actor_id"]
                    if remote_actor_id in by_actor:
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
                        delivery_profile=target.get("delivery_profile", "threadiverse_group"),
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
                        select(LocalCommunityRelayDelivery).where(
                            LocalCommunityRelayDelivery.local_community_id == local_community_id,
                            LocalCommunityRelayDelivery.operation == "create",
                            LocalCommunityRelayDelivery.source_object_ap_id == source_object_ap_id,
                            LocalCommunityRelayDelivery.status == "delivered",
                        ).order_by(LocalCommunityRelayDelivery.created_at, LocalCommunityRelayDelivery.id)
                    )
                )

    def mark_local_community_relay_delivery_result(
            self,
            *,
            delivery_id: int,
            status: str,
            relay_activity_id: str | None = None,
            error: str | None = None,
        ) -> LocalCommunityRelayDelivery | None:
            """Persist one per-target relay outcome returned by the gateway."""
            with self.session() as session:
                delivery = session.get(LocalCommunityRelayDelivery, delivery_id)
                if delivery is None:
                    return None
                delivery.status = status
                delivery.relay_activity_id = relay_activity_id or delivery.relay_activity_id
                delivery.last_error = error
                delivery.last_attempted_at = utcnow()
                delivery.attempt_count += 1
                session.flush()
                return delivery

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
                        LocalCommunityRelaySourceActivity.local_community_id == local_community_id,
                        LocalCommunityRelaySourceActivity.operation == operation,
                        LocalCommunityRelaySourceActivity.source_object_ap_id == source_object_ap_id,
                        LocalCommunityRelaySourceActivity.source_activity_id == source_activity_id,
                    )
                )
