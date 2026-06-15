"""Durable rendering and transport orchestration for User Actor activities."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol

from .db.database import Database
from .fedify_gateway_client import FedifyGatewayClient


class FederationDecision(Protocol):
    """Minimal non-secret policy result consumed by delivery orchestration."""

    allowed: bool


class FederationPolicyEvaluator(Protocol):
    """Narrow policy boundary; delivery never receives bridge settings."""

    def federation_decision(self, target: str) -> FederationDecision:
        """Return the current federation decision for an actor or endpoint."""


@dataclass(frozen=True, slots=True)
class ProfileFanoutSummary:
    """Expose command-safe local/remote profile update state."""

    source_id: int
    total: int
    delivered: int
    outstanding: int
    render_pending: bool = False


class UserActorDeliveryService:
    """Render immutable sources, claim ordered targets, and persist outcomes."""

    CLAIM_BATCH_SIZE = 20
    PER_USER_CLAIM_LIMIT = 5
    STALE_CLAIM_SECONDS = 300

    def __init__(
        self,
        database: Database,
        gateway: FedifyGatewayClient,
        policy_service: FederationPolicyEvaluator,
    ) -> None:
        """Bind persistence, the minimal gateway adapter, and current policy."""
        self.database = database
        self.gateway = gateway
        self.policy_service = policy_service

    async def process_source(self, source_id: int) -> ProfileFanoutSummary:
        """Make a bounded immediate attempt for one committed source."""
        source = self.database.user_actor_deliveries.get_source(source_id)
        if source is None:
            raise LookupError("outbound source not found")
        if source.state != "ready":
            try:
                activity = await self.gateway.render_user_actor_profile_update(
                    activity_id=source.activity_id,
                    profile_snapshot=json.loads(source.profile_snapshot_json),
                )
                self.database.user_actor_deliveries.record_rendered(source_id, activity)
                self.database.user_actor_deliveries.release_terminal_source_media(source_id)
            except Exception as exc:
                self.database.user_actor_deliveries.record_render_failure(source_id, str(exc))
                delivered, outstanding, total = self.database.user_actor_deliveries.source_summary(source_id)
                return ProfileFanoutSummary(source_id, total, delivered, outstanding, True)
        await self.drain_deliveries()
        delivered, outstanding, total = self.database.user_actor_deliveries.source_summary(source_id)
        return ProfileFanoutSummary(source_id, total, delivered, outstanding)

    async def drain_once(self, *, source_limit: int = 10, delivery_limit: int = CLAIM_BATCH_SIZE) -> int:
        """Process bounded due render and transport work without failing the runtime."""
        processed = 0
        for source_id in self.database.user_actor_deliveries.due_source_ids(source_limit):
            source = self.database.user_actor_deliveries.get_source(source_id)
            if source is None:
                continue
            try:
                activity = await self.gateway.render_user_actor_profile_update(
                    activity_id=source.activity_id,
                    profile_snapshot=json.loads(source.profile_snapshot_json),
                )
                self.database.user_actor_deliveries.record_rendered(source_id, activity)
                self.database.user_actor_deliveries.release_terminal_source_media(source_id)
            except Exception as exc:
                self.database.user_actor_deliveries.record_render_failure(source_id, str(exc))
            processed += 1
        processed += await self.drain_deliveries(limit=delivery_limit)
        return processed

    async def drain_deliveries(self, *, limit: int = CLAIM_BATCH_SIZE) -> int:
        """Claim and submit a bounded batch with isolated per-target outcomes."""
        claims = self.database.user_actor_deliveries.claim_due(
            limit=limit,
            per_user_limit=self.PER_USER_CLAIM_LIMIT,
            stale_after_seconds=self.STALE_CLAIM_SECONDS,
        )
        if not claims:
            return 0
        # Claims guarantee one actor/endpoint version at a time. Grouping by
        # actor keeps gateway identity selection narrow and deterministic.
        groups: dict[tuple[str, str], list] = {}
        for claim in claims:
            try:
                endpoint_allowed = self.policy_service.federation_decision(
                    claim.target_endpoint_url
                ).allowed
                represented_actor_allowed = any(
                    self.policy_service.federation_decision(actor_id).allowed
                    for actor_id in claim.represented_actor_ids
                )
            except Exception as exc:
                # Policy persistence is outside the remote transport boundary.
                # Release only this claim so unrelated endpoints still proceed.
                self.database.user_actor_deliveries.release_unsubmitted_claim(
                    claim.delivery_id, claim.claim_token, str(exc)
                )
                continue
            if not endpoint_allowed or not represented_actor_allowed:
                self.database.user_actor_deliveries.mark_ineligible(
                    claim.delivery_id,
                    claim.claim_token,
                    "federation policy denied endpoint",
                )
                self.database.user_actor_deliveries.release_terminal_source_media(
                    claim.source_id
                )
                continue
            if not self.database.user_actor_deliveries.mark_submitted(
                claim.delivery_id, claim.claim_token
            ):
                continue
            groups.setdefault((claim.actor_url, claim.actor_username), []).append(
                claim
            )
        for (actor_url, username), group in groups.items():
            payload = [
                {
                    "deliveryId": claim.delivery_id,
                    "targetEndpointUrl": claim.target_endpoint_url,
                    "activityJson": claim.activity_json,
                }
                for claim in group
            ]
            try:
                outcomes = await self.gateway.send_user_actor_deliveries(
                    signing_actor_url=actor_url,
                    actor_username=username,
                    deliveries=payload,
                )
                by_id = {outcome.delivery_id: outcome for outcome in outcomes}
                for claim in group:
                    outcome = by_id.get(claim.delivery_id)
                    self.database.user_actor_deliveries.record_outcome(
                        claim.delivery_id,
                        claim.claim_token,
                        ok=bool(outcome and outcome.ok),
                        error=(
                            None
                            if outcome and outcome.ok
                            else (
                                outcome.error
                                if outcome
                                else "gateway omitted outcome"
                            )
                        ),
                    )
                    self.database.user_actor_deliveries.release_terminal_source_media(claim.source_id)
            except Exception as exc:
                for claim in group:
                    self.database.user_actor_deliveries.record_outcome(
                        claim.delivery_id, claim.claim_token, ok=False, error=str(exc)
                    )
                    self.database.user_actor_deliveries.release_terminal_source_media(claim.source_id)
        return len(claims)
