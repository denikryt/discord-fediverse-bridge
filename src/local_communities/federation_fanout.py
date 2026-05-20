"""Federation fanout for inbound remote content in local communities.

The fanout helper owns local-community relay policy: target selection,
durable source/delivery state, profile-based rendering, and gateway transport
calls. It deliberately leaves Discord mirroring to `LocalCommunityRuntime` and
HTTP-signature delivery to the gateway.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..db import Database
from ..fedify_gateway_client import SendLocalCommunityRelayDelivery
from .activitypub_renderers import render_local_community_relay_activity

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class RelayFanoutSummary:
    """Report how many target deliveries were attempted by one fanout call."""

    attempted: int
    delivered: int
    failed: int


class LocalCommunityFederationFanout:
    """Coordinate durable federation relay for local-community inbound events."""

    def __init__(self, *, database: Database, fedify_gateway: object) -> None:
        """Initialize the fanout helper with persistence and gateway boundaries."""
        self.database = database
        self.fedify_gateway = fedify_gateway

    async def relay_create(self, *, event: object, local_community: object, object_kind: str) -> RelayFanoutSummary:
        """Relay one inbound remote create to other accepted followers."""
        return await self._relay_to_accepted_followers(
            event=event,
            local_community=local_community,
            object_kind=object_kind,
            operation="create",
        )

    async def relay_update_or_delete(self, *, event: object, local_community: object, object_kind: str, operation: str) -> RelayFanoutSummary:
        """Relay update/delete only to targets that received the original create."""
        delivered_create_targets = self.database.list_delivered_local_community_create_relay_targets(
            local_community_id=getattr(local_community, "id"),
            source_object_ap_id=getattr(getattr(event, "object"), "ap_id"),
        )
        targets = [
            {
                "remote_actor_id": row.target_remote_actor_id,
                "remote_inbox_url": row.target_inbox_url,
                "delivery_profile": row.delivery_profile,
            }
            for row in delivered_create_targets
        ]
        return await self._relay_to_targets(
            event=event,
            local_community=local_community,
            object_kind=object_kind,
            operation=operation,
            targets=targets,
        )

    async def _relay_to_accepted_followers(self, *, event: object, local_community: object, object_kind: str, operation: str) -> RelayFanoutSummary:
        """Select accepted targets excluding the origin actor, then relay."""
        followers = self.database.list_local_community_followers(getattr(local_community, "id"), status="accepted")
        targets = []
        for follower in followers:
            # Never send the relayed activity back to the actor that authored it;
            # this avoids loops and duplicate UI entries on the origin instance.
            if follower.remote_actor_id == getattr(event, "actor_id"):
                continue
            targets.append(
                {
                    "remote_actor_id": follower.remote_actor_id,
                    "remote_inbox_url": follower.remote_inbox_url,
                    "delivery_profile": getattr(follower, "delivery_profile", "threadiverse_group"),
                }
            )
        return await self._relay_to_targets(
            event=event,
            local_community=local_community,
            object_kind=object_kind,
            operation=operation,
            targets=targets,
        )

    async def _relay_to_targets(self, *, event: object, local_community: object, object_kind: str, operation: str, targets: list[dict[str, str]]) -> RelayFanoutSummary:
        """Persist source/target rows, render payloads, and call the gateway."""
        source_json = getattr(event, "source_activity_json", None)
        if not source_json:
            # Older tests and non-upgraded gateway events lack source JSON. The
            # relay contract requires it, so there is nothing safe to federate.
            return RelayFanoutSummary(attempted=0, delivered=0, failed=0)

        source_activity_id = getattr(event, "source_activity_id", None) or getattr(event, "delivery_id")
        source = self.database.get_or_create_local_community_relay_source_activity(
            local_community_id=getattr(local_community, "id"),
            object_kind=object_kind,
            operation=operation,
            source_object_ap_id=getattr(getattr(event, "object"), "ap_id"),
            source_activity_id=source_activity_id,
            source_announce_id=getattr(event, "source_announce_id", None),
            origin_remote_actor_id=getattr(event, "actor_id"),
            source_activity_json=source_json,
        )
        deliveries = self.database.create_missing_local_community_relay_deliveries(
            source_activity=source,
            targets=targets,
        )
        pending = [row for row in deliveries if row.status in {"pending", "failed"}]
        if not pending:
            return RelayFanoutSummary(attempted=0, delivered=0, failed=0)

        outbound = []
        for row in pending:
            activity_json = render_local_community_relay_activity(
                source_activity_json=source.source_activity_json,
                community_actor_url=getattr(local_community, "actor_url"),
                community_slug=getattr(local_community, "slug"),
                delivery_profile=row.delivery_profile,
            )
            outbound.append(
                SendLocalCommunityRelayDelivery(
                    delivery_id=row.id,
                    target_remote_actor_id=row.target_remote_actor_id,
                    target_inbox_url=row.target_inbox_url,
                    activity_json=activity_json,
                )
            )

        result = await self.fedify_gateway.send_local_community_relay(
            signing_actor_url=getattr(local_community, "actor_url"),
            deliveries=outbound,
        )
        delivered = 0
        failed = 0
        for outcome in result.outcomes:
            if outcome.ok:
                delivered += 1
                self.database.mark_local_community_relay_delivery_result(
                    delivery_id=outcome.delivery_id,
                    status="delivered",
                    relay_activity_id=outcome.activity_id,
                )
            else:
                failed += 1
                self.database.mark_local_community_relay_delivery_result(
                    delivery_id=outcome.delivery_id,
                    status="failed",
                    relay_activity_id=outcome.activity_id,
                    error=outcome.error,
                )
        return RelayFanoutSummary(attempted=len(outbound), delivered=delivered, failed=failed)
