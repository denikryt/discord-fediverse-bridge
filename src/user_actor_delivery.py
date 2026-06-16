"""Durable rendering and transport orchestration for User Actor activities."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Protocol

from .fedify_gateway_client import DeleteContentRequest, UpdateContentRequest

from .db.database import Database
from .fedify_gateway_client import FedifyGatewayClient


class FederationDecision(Protocol):
    """Minimal non-secret policy result consumed by delivery orchestration."""

    allowed: bool


class DiscordMessageStateReader(Protocol):
    async def fetch_message_state(self, channel_id: int, message_id: int) -> tuple[str, object | None]:
        """Return (present|missing, message-or-None), raising on transient failure."""


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
        discord_message_reader: DiscordMessageStateReader | None = None,
    ) -> None:
        """Bind persistence, the minimal gateway adapter, and current policy."""
        self.database = database
        self.gateway = gateway
        self.policy_service = policy_service
        self.discord_message_reader = discord_message_reader

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
        """Recover pre-source mutations, then render and transport bounded work."""
        processed = await self.drain_reconciliations(limit=source_limit)
        processed += await self.drain_mutations(limit=source_limit)
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


    async def drain_reconciliations(self, *, limit: int = 10) -> int:
        """Resolve durable partial Discord edits before mutation reservation scan."""
        if self.discord_message_reader is None:
            return 0
        rows = self.database.content_mutations.due_reconciliations(limit=limit)
        for channel_id, message_id, object_id, title in rows:
            try:
                status, message = await self.discord_message_reader.fetch_message_state(channel_id, message_id)
                if status == "missing":
                    outcome = self.database.content_mutations.record_desired_delete(
                        object_id=object_id
                    )
                else:
                    edited_at = getattr(message, "edited_at", None)
                    content = getattr(message, "content", None)
                    if edited_at is None or content is None:
                        raise ValueError("Discord message state lacks edited timestamp or content")
                    target_inbox_urls = self._replay_target_inboxes(object_id)
                    outcome = (
                        self.database.content_mutations
                        .reconcile_authoritative_update_and_reserve(
                            object_id=object_id,
                            title=title,
                            body_markdown=content,
                            edited_timestamp=edited_at,
                            target_inbox_urls=target_inbox_urls,
                        )
                    )
                    if outcome not in {"accepted", "duplicate", "deleted", "not_needed"}:
                        raise ValueError(
                            f"Discord reconciliation unresolved: {outcome}"
                        )
                    continue
                if outcome not in {"accepted", "duplicate", "deleted"}:
                    raise ValueError(f"Discord reconciliation unresolved: {outcome}")
                target_inbox_urls = self._replay_target_inboxes(object_id)
                self.database.content_mutations.reserve_next(
                    object_id=object_id,
                    target_inbox_urls=target_inbox_urls,
                )
                self.database.content_mutations.clear_reconciliation(object_id=object_id)
            except Exception as exc:
                self.database.content_mutations.record_reconciliation_failure(object_id=object_id, error=str(exc))
        return len(rows)


    def _replay_target_inboxes(self, object_id: str) -> list[str] | None:
        """Recompute the mode-specific safe gateway target snapshot for recovery."""
        published = (
            self.database.activitypub_objects
            .get_published_activity_object_by_object_id(object_id)
        )
        if published is None:
            return []
        local_community = self.database.local_communities.get_local_community_by_actor_url(
            published.community_actor_url
        )
        if local_community is None:
            return None
        # Recompute through the same accepted-subscriber and policy boundary as
        # the normal local-community path.  None means unrestricted fanout in the
        # gateway, so recovery must return an explicit list for local communities.
        return [
            str(row.remote_inbox_url)
            for row in self.database.remote_subscribers.list_remote_subscribers(
                local_community.id, status="accepted"
            )
            if self.policy_service.federation_decision(
                str(row.remote_inbox_url)
            ).allowed
        ]

    async def submit_update(
        self,
        *,
        object_id: str,
        title: str | None,
        body_markdown: str,
        edited_timestamp,
        target_inbox_urls: list[str] | None = None,
    ) -> str:
        """Durably admit one Discord edit and wake replayable orchestration."""
        outcome = self.database.content_mutations.record_desired_update(
            object_id=object_id,
            title=title,
            body_markdown=body_markdown,
            edited_timestamp=edited_timestamp,
        )
        if outcome == "accepted":
            self.database.content_mutations.reserve_next(
                object_id=object_id, target_inbox_urls=target_inbox_urls
            )
            await self.drain_mutations(limit=1)
        return outcome

    async def submit_delete(
        self, *, object_id: str, target_inbox_urls: list[str] | None = None
    ) -> str:
        """Durably admit terminal deletion and start replayable orchestration."""
        outcome = self.database.content_mutations.record_desired_delete(object_id=object_id)
        if outcome == "accepted":
            self.database.content_mutations.reserve_next(
                object_id=object_id, target_inbox_urls=target_inbox_urls
            )
            await self.drain_mutations(limit=1)
        return outcome

    async def drain_mutations(self, *, limit: int = 10) -> int:
        """Recover a bounded batch of pre-source mutation reservations."""
        claims = self.database.content_mutations.claim_due(
            limit=limit, stale_after_seconds=self.STALE_CLAIM_SECONDS
        )
        for claim in claims:
            try:
                canonical = self.database.content_mutations.load_canonical_ready(
                    claim.mutation_id, claim.claim_token
                )
                if canonical is None:
                    request = claim.request
                    self._validate_mutation_request(claim)
                    if not self.database.content_mutations.mark_send_possible(claim.mutation_id, claim.claim_token):
                        continue
                    if claim.kind == "content_delete":
                        result = await self.gateway.delete_content(
                            DeleteContentRequest(
                                actor_username=request["actorUsername"],
                                community_actor_url=request["communityActorUrl"],
                                ap_object_id=request["apObjectId"],
                                target_inbox_urls=request.get("targetInboxUrls"),
                                operation_id=claim.operation_id,
                                activity_id=claim.activity_id,
                                mutation_timestamp=request.get("mutationTimestamp"),
                                expected_revision=claim.expected_revision,
                                next_revision=claim.next_revision,
                            )
                        )
                    else:
                        result = await self.gateway.update_content(
                            UpdateContentRequest(
                                actor_username=request["actorUsername"],
                                community_actor_url=request["communityActorUrl"],
                                ap_object_id=request["apObjectId"],
                                kind=request["kind"],
                                title=request.get("title"),
                                body_markdown=request.get("bodyMarkdown") or "",
                                in_reply_to_object_id=request.get("inReplyToObjectId"),
                                target_inbox_urls=request.get("targetInboxUrls"),
                                operation_id=claim.operation_id,
                                activity_id=claim.activity_id,
                                mutation_timestamp=request.get("mutationTimestamp"),
                                expected_revision=claim.expected_revision,
                                next_revision=claim.next_revision,
                            )
                        )
                    canonical = result.activity_json
                    if not self.database.content_mutations.record_canonical_result(
                        claim.mutation_id, claim.claim_token, canonical
                    ):
                        continue
                committed = self.database.content_mutations.commit_canonical(
                    mutation_id=claim.mutation_id,
                    claim_token=claim.claim_token,
                    canonical_activity_json=canonical,
                    delivery_repository=self.database.user_actor_deliveries,
                )
                if committed is not None:
                    _source_id, object_id = committed
                    self.database.content_mutations.reserve_next(
                        object_id=object_id,
                        target_inbox_urls=claim.request.get("targetInboxUrls"),
                    )
            except ValueError as exc:
                self.database.content_mutations.abort_local_validation(
                    claim.mutation_id, claim.claim_token, str(exc)
                )
                self.database.content_mutations.reserve_next(
                    object_id=claim.request["apObjectId"],
                    target_inbox_urls=claim.request.get("targetInboxUrls"),
                )
            except asyncio.CancelledError:
                self.database.content_mutations.record_retryable_failure(
                    claim.mutation_id, claim.claim_token, "worker cancelled after dispatch boundary"
                )
                raise
            except Exception as exc:
                self.database.content_mutations.record_retryable_failure(
                    claim.mutation_id, claim.claim_token, str(exc)
                )
        return len(claims)

    @staticmethod
    def _validate_mutation_request(claim) -> None:
        """Reject malformed local reservations before any gateway side effect."""
        request = claim.request
        if request.get("apObjectId") is None or request.get("actorUsername") is None:
            raise ValueError("mutation reservation is missing object ownership data")
        if claim.kind == "content_update" and request.get("kind") not in {"post", "comment"}:
            raise ValueError("mutation reservation has an unsupported object kind")
        if request.get("operationId") != claim.operation_id:
            raise ValueError("mutation operation identity does not match its reservation")
        if request.get("activityId") != claim.activity_id:
            raise ValueError("mutation activity identity does not match its reservation")

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
