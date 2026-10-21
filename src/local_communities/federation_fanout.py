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
from ..bridge_policy import BridgePolicyService
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

    def __init__(
        self,
        *,
        database: Database,
        fedify_gateway: object,
        policy_service: BridgePolicyService,
    ) -> None:
        """Initialize the fanout helper with persistence and gateway boundaries."""
        self.database = database
        self.fedify_gateway = fedify_gateway
        self.policy_service = policy_service

    async def relay_create(
        self, *, event: object, local_community: object, object_kind: str
    ) -> RelayFanoutSummary:
        """Relay one inbound remote create to other accepted remote subscribers."""
        return await self._relay_to_accepted_remote_subscribers(
            event=event,
            local_community=local_community,
            object_kind=object_kind,
            operation="create",
        )

    async def relay_update_or_delete(
        self,
        *,
        event: object,
        local_community: object,
        object_kind: str,
        operation: str,
    ) -> RelayFanoutSummary:
        """Relay update/delete only to targets that received the original create."""
        delivered_create_targets = self.database.local_community_relay.list_delivered_local_community_create_relay_targets(
            local_community_id=getattr(local_community, "id"),
            source_object_ap_id=getattr(getattr(event, "object"), "ap_id"),
        )
        accepted_remote_subscribers = {
            remote_subscriber.remote_actor_id: remote_subscriber
            for remote_subscriber in self.database.remote_subscribers.list_remote_subscribers(
                getattr(local_community, "id"), status="accepted"
            )
        }
        targets = []
        for row in delivered_create_targets:
            # Historical delivery rows prove the original create reached this
            # actor. Current remote-subscriber rows decide whether update/delete relay is
            # still allowed after an unfollow or re-follow.
            current = accepted_remote_subscribers.get(row.target_remote_actor_id)
            if current is None:
                continue
            targets.append(
                {
                    "remote_actor_id": row.target_remote_actor_id,
                    "remote_inbox_url": current.remote_inbox_url,
                    "delivery_profile": getattr(
                        current, "delivery_profile", "threadiverse_group"
                    ),
                }
            )
        return await self._relay_to_targets(
            event=event,
            local_community=local_community,
            object_kind=object_kind,
            operation=operation,
            targets=targets,
        )

    async def _relay_to_accepted_remote_subscribers(
        self,
        *,
        event: object,
        local_community: object,
        object_kind: str,
        operation: str,
    ) -> RelayFanoutSummary:
        """Select accepted remote-subscriber targets excluding the origin actor, then relay."""
        remote_subscribers = self.database.remote_subscribers.list_remote_subscribers(
            getattr(local_community, "id"), status="accepted"
        )
        targets = []
        for remote_subscriber in remote_subscribers:
            # Never send the relayed activity back to the actor that authored it;
            # this avoids loops and duplicate UI entries on the origin instance.
            if remote_subscriber.remote_actor_id == getattr(event, "actor_id"):
                continue
            targets.append(
                {
                    "remote_actor_id": remote_subscriber.remote_actor_id,
                    "remote_inbox_url": remote_subscriber.remote_inbox_url,
                    "delivery_profile": getattr(
                        remote_subscriber, "delivery_profile", "threadiverse_group"
                    ),
                }
            )
        return await self._relay_to_targets(
            event=event,
            local_community=local_community,
            object_kind=object_kind,
            operation=operation,
            targets=targets,
        )

    async def _relay_to_targets(
        self,
        *,
        event: object,
        local_community: object,
        object_kind: str,
        operation: str,
        targets: list[dict[str, str]],
    ) -> RelayFanoutSummary:
        """Persist source/target rows, render payloads, and call the gateway."""
        snapshot = self.policy_service.snapshot()
        targets = [
            target
            for target in targets
            if snapshot.federation_decision(
                target.get("remote_inbox_url") or target.get("remote_actor_id") or ""
            ).allowed
        ]
        source_json = getattr(event, "source_activity_json", None)
        if not source_json:
            # Older tests and non-upgraded gateway events lack source JSON. The
            # relay contract requires it, so there is nothing safe to federate.
            return RelayFanoutSummary(attempted=0, delivered=0, failed=0)

        source_activity_id = getattr(event, "source_activity_id", None) or getattr(
            event, "delivery_id"
        )
        source = self.database.local_community_relay.get_or_create_local_community_relay_source_activity(
            local_community_id=getattr(local_community, "id"),
            object_kind=object_kind,
            operation=operation,
            source_object_ap_id=getattr(getattr(event, "object"), "ap_id"),
            source_activity_id=source_activity_id,
            source_announce_id=getattr(event, "source_announce_id", None),
            origin_remote_actor_id=getattr(event, "actor_id"),
            source_activity_json=source_json,
        )
        deliveries = self.database.local_community_relay.create_missing_local_community_relay_deliveries(
            source_activity=source,
            targets=targets,
        )
        eligible_actor_ids = {target["remote_actor_id"] for target in targets}
        pending = [
            row
            for row in deliveries
            if row.status in {"pending", "failed"}
            and row.target_remote_actor_id in eligible_actor_ids
        ]
        if not pending:
            return RelayFanoutSummary(attempted=0, delivered=0, failed=0)

        outbound = []
        attempts = {}
        for row in pending:
            attempt = self.database.local_community_relay.prepare_local_community_relay_attempt(
                delivery_id=row.id,
                community_actor_url=getattr(local_community, "actor_url"),
                community_slug=getattr(local_community, "slug"),
                policy_service=self.policy_service,
            )
            if attempt is None:
                continue
            # A recipient's address and attempt generation are frozen after
            # the writer transaction commits; no DB lock spans gateway HTTP.
            attempts[attempt.delivery_id] = attempt
            try:
                activity_json = render_local_community_relay_activity(
                    source_activity_json=source.source_activity_json,
                    normalized_event=event,
                    community_actor_url=getattr(local_community, "actor_url"),
                    community_slug=getattr(local_community, "slug"),
                    delivery_profile=attempt.delivery_profile,
                    object_kind=row.object_kind,
                    relay_activity_id=attempt.relay_activity_id,
                )
                if activity_json["id"] != attempt.relay_activity_id:
                    raise ValueError("rendered relay activity ID differs from reservation")
            except Exception as exc:
                # A renderer error never permits an untracked transport send.
                self._record_outcome(attempt, local_community=local_community, ok=False, error=f"relay render failed: {exc}")
                attempts.pop(attempt.delivery_id)
                logger.exception("Relay rendering failed for delivery %s", attempt.delivery_id)
                continue
            outbound.append(
                SendLocalCommunityRelayDelivery(
                    delivery_id=attempt.delivery_id,
                    target_remote_actor_id=attempt.target_remote_actor_id,
                    target_inbox_url=attempt.attempted_inbox_url,
                    activity_json=activity_json,
                )
            )

        if not outbound:
            return RelayFanoutSummary(attempted=0, delivered=0, failed=0)
        # Gateway failure and omitted outcomes use the same generation-guarded
        # completion path as individual responses. No stale result may override
        # a later successful send of this source-level activity.
        try:
            result = await self.fedify_gateway.send_local_community_relay(
                signing_actor_url=getattr(local_community, "actor_url"),
                deliveries=outbound,
            )
            outcomes = result.outcomes
        except Exception as exc:
            logger.exception("ActivityPub relay gateway call failed")
            outcomes = []
            transport_error = str(exc)
        else:
            transport_error = "gateway omitted relay outcome"
        delivered = 0
        failed = 0
        seen = set()
        for outcome in outcomes:
            attempt = attempts.get(outcome.delivery_id)
            if attempt is None or outcome.delivery_id in seen:
                logger.error("Unrecognized or duplicate gateway relay outcome: %s", outcome.delivery_id)
                continue
            seen.add(outcome.delivery_id)
            matched = (
                outcome.activity_id == attempt.relay_activity_id
                and outcome.target_remote_actor_id == attempt.target_remote_actor_id
            )
            ok = bool(outcome.ok and matched)
            self._record_outcome(
                attempt,
                local_community=local_community,
                ok=ok,
                error=outcome.error if matched else "gateway relay outcome identity mismatch",
            )
            if ok:
                delivered += 1
            else:
                failed += 1
        for delivery_id, attempt in attempts.items():
            if delivery_id in seen:
                continue
            self._record_outcome(attempt, local_community=local_community, ok=False, error=transport_error)
            failed += 1
        return RelayFanoutSummary(
            attempted=len(outbound), delivered=delivered, failed=failed
        )

    def _record_outcome(
        self, attempt: object, *, local_community: object, ok: bool, error: str | None
    ) -> None:
        """Write an HTTP outcome only against its own route and generation."""
        self.database.local_community_relay.mark_local_community_relay_delivery_result(
            delivery_id=attempt.delivery_id,
            status="delivered" if ok else "failed",
            relay_activity_id=attempt.relay_activity_id,
            attempted_inbox_url=attempt.attempted_inbox_url,
            claimed_attempt_count=attempt.claimed_attempt_count,
            policy_service=self.policy_service,
            community_actor_url=getattr(local_community, "actor_url"),
            error=error,
        )
