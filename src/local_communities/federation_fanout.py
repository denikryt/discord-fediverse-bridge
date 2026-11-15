"""Tenant-scoped, immutable relay and fenced HTTP fanout for hosted Group content.

The canonical Create owns the author, original Service and first verified body;
its Announce wrapper ID is receipt evidence, never a second relay source. Each
target holds one stable ActivityPub ID and one committed CAS attempt before I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import logging
from types import SimpleNamespace
from urllib.parse import urlsplit

from ..activitypub_identity_urls import (
    build_community_announce_comment_activity_id,
    build_community_announce_post_activity_id,
)
from ..db import Database
from ..activitypub_models import activity_actor_id
from ..db.repositories.local_community_relay import RelayIdentityConflict
from ..bridge_policy import BridgePolicyService
from ..fedify_gateway_client import SendLocalCommunityRelayDelivery
from .activitypub_renderers import render_local_community_relay_activity

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class RelayFanoutSummary:
    """Count attempted, confirmed delivered and unsuccessful target deliveries."""

    attempted: int
    delivered: int
    failed: int


def _first_verified_projection(event: object) -> dict:
    """Keep exactly the normalized fields needed for stable Note projection.

    The first verified source's raw JSON is immutable, but a Mastodon Note may
    require the normalized markdown/reply data that lived only in the event.
    Persist it once rather than reconstruct it from an untrusted later replay.
    """
    obj = getattr(event, "object")
    published = getattr(obj, "published_at", None)
    return {
        "event_type": getattr(event, "event_type", None),
        "actor_id": getattr(event, "actor_id", None),
        "delivery_id": getattr(event, "delivery_id", None),
        "object": {
            "ap_id": getattr(obj, "ap_id", None),
            "kind": getattr(obj, "kind", None),
            "body_markdown": getattr(obj, "body_markdown", None),
            "parent_ap_id": getattr(obj, "parent_ap_id", None),
            "post_ap_id": getattr(obj, "post_ap_id", None),
            "published_at": published.isoformat() if hasattr(published, "isoformat") else published,
            "url": getattr(obj, "url", None),
        },
    }


def _frozen_source_event(source: object) -> object:
    """Expose the first persisted projection in the renderer's narrow event form."""
    saved = getattr(source, "canonical_relay_projection_json", None)
    if not isinstance(saved, dict) or not isinstance(saved.get("object"), dict):
        # Legacy source JSON does not prove first normalized markdown. The
        # renderer may preserve a directly compatible Create, but must not
        # build a new sanitized Note from a later, conflicting envelope.
        saved = {"event_type": None, "object": {}}
    return SimpleNamespace(
        event_type=saved.get("event_type"),
        actor_id=saved.get("actor_id"),
        delivery_id=saved.get("delivery_id"),
        object=SimpleNamespace(**saved["object"]),
    )


def _stable_relay_id(*, source: object, target_actor_id: str, community: object) -> str:
    """Derive one permanent Announce ID from the first source and target."""
    parts = urlsplit(getattr(community, "actor_url"))
    origin = f"{parts.scheme}://{parts.netloc}"
    identity = [
        getattr(community, "actor_url"), getattr(source, "operation"),
        getattr(source, "source_object_ap_id"), getattr(source, "source_activity_id"),
        getattr(source, "object_kind"), target_actor_id,
    ]
    token = sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()
    builder = (
        build_community_announce_comment_activity_id
        if getattr(source, "object_kind") == "comment"
        else build_community_announce_post_activity_id
    )
    return builder(origin=origin, slug=getattr(community, "slug"), activity_id=token)


class LocalCommunityFederationFanout:
    """Authorize, reserve and deliver local Group relay targets through Fedify."""

    def __init__(
        self, *, database: Database, fedify_gateway: object,
        policy_service: BridgePolicyService,
    ) -> None:
        """Keep repository, policy and HTTP transport responsibilities separate."""
        self.database = database
        self.fedify_gateway = fedify_gateway
        self.policy_service = policy_service

    async def relay_create(
        self, *, event: object, local_community: object, object_kind: str,
        origin_subscriber_actor_id: str | None = None,
        source_owner_actor_id: str | None = None,
    ) -> RelayFanoutSummary:
        """Relay one verified Create, excluding its original Service subscription."""
        if source_owner_actor_id is not None and (
            getattr(event, "actor_id", None) != source_owner_actor_id
            or not isinstance(getattr(event, "source_activity_json", None), dict)
            or activity_actor_id(event.source_activity_json.get("actor")) != source_owner_actor_id
        ):
            raise RelayIdentityConflict("relay Create does not match original canonical owner")
        original_origin = origin_subscriber_actor_id or getattr(event, "actor_id")
        return await self._relay_to_accepted_remote_subscribers(
            event=event, local_community=local_community,
            object_kind=object_kind, operation="create",
            origin_subscriber_actor_id=original_origin,
        )

    async def relay_update_or_delete(
        self, *, event: object, local_community: object,
        object_kind: str, operation: str,
        origin_subscriber_actor_id: str | None = None,
        source_owner_actor_id: str | None = None,
    ) -> RelayFanoutSummary:
        """Send an authorized revision only to confirmed original Create targets."""
        if source_owner_actor_id is not None and (
            getattr(event, "actor_id", None) != source_owner_actor_id
            or not isinstance(getattr(event, "source_activity_json", None), dict)
            or activity_actor_id(event.source_activity_json.get("actor")) != source_owner_actor_id
        ):
            raise RelayIdentityConflict("relay mutation does not match original canonical owner")
        create_targets = self.database.local_community_relay.list_delivered_local_community_create_relay_targets(
            local_community_id=getattr(local_community, "id"),
            source_object_ap_id=getattr(getattr(event, "object"), "ap_id"),
        )
        if not create_targets:
            return RelayFanoutSummary(0, 0, 0)
        original_origins = {row.origin_remote_actor_id for row in create_targets}
        if len(original_origins) != 1:
            raise RelayIdentityConflict("conflicting original relay origins")
        original_origin = next(iter(original_origins))
        if origin_subscriber_actor_id is not None and original_origin != origin_subscriber_actor_id:
            raise RelayIdentityConflict("mutation cannot replace first relay Service")
        current_subscribers = {
            row.remote_actor_id: row
            for row in self.database.remote_subscribers.list_remote_subscribers(
                getattr(local_community, "id"), status="accepted",
            )
        }
        targets = []
        for row in create_targets:
            current = current_subscribers.get(row.target_remote_actor_id)
            if current is None or row.target_remote_actor_id == original_origin:
                continue
            # Neither an Undo/re-Follow nor a changed inbox can silently retarget
            # a delivered Create's provenance to a different remote endpoint.
            if current.remote_inbox_url != row.target_inbox_url:
                continue
            targets.append({
                "remote_actor_id": row.target_remote_actor_id,
                "remote_inbox_url": row.target_inbox_url,
                "delivery_profile": row.delivery_profile,
            })
        return await self._relay_to_targets(
            event=event, local_community=local_community,
            object_kind=object_kind, operation=operation,
            origin_subscriber_actor_id=original_origin, targets=targets,
        )

    async def _relay_to_accepted_remote_subscribers(
        self, *, event: object, local_community: object,
        object_kind: str, operation: str,
        origin_subscriber_actor_id: str,
    ) -> RelayFanoutSummary:
        """Exclude the immutable accepted Service, not just the author Person."""
        rows = self.database.remote_subscribers.list_remote_subscribers(
            getattr(local_community, "id"), status="accepted",
        )
        targets = [{
            "remote_actor_id": row.remote_actor_id,
            "remote_inbox_url": row.remote_inbox_url,
            "delivery_profile": getattr(row, "delivery_profile", "threadiverse_group"),
        } for row in rows if row.remote_actor_id != origin_subscriber_actor_id]
        return await self._relay_to_targets(
            event=event, local_community=local_community,
            object_kind=object_kind, operation=operation,
            origin_subscriber_actor_id=origin_subscriber_actor_id, targets=targets,
        )

    async def _relay_to_targets(
        self, *, event: object, local_community: object,
        object_kind: str, operation: str,
        origin_subscriber_actor_id: str, targets: list[dict[str, str]],
    ) -> RelayFanoutSummary:
        """Persist verified immutable source, claim each eligible target, then send."""
        snapshot = self.policy_service.snapshot()
        targets = [target for target in targets
                   if snapshot.federation_decision(target["remote_actor_id"]).allowed
                   and snapshot.federation_decision(target["remote_inbox_url"]).allowed
                   and target["remote_actor_id"] != origin_subscriber_actor_id]
        if not targets:
            return RelayFanoutSummary(0, 0, 0)
        source_json = getattr(event, "source_activity_json", None)
        if not isinstance(source_json, dict) or not source_json:
            # No trusted source snapshot: never manufacture a relay from an AP
            # ID or a later normalized envelope without owner provenance.
            return RelayFanoutSummary(0, 0, 0)
        source_activity_id = getattr(event, "source_activity_id", None) or getattr(event, "delivery_id")
        source = self.database.local_community_relay.get_or_create_local_community_relay_source_activity(
            local_community_id=getattr(local_community, "id"),
            object_kind=object_kind, operation=operation,
            source_object_ap_id=getattr(getattr(event, "object"), "ap_id"),
            source_activity_id=source_activity_id,
            source_announce_id=getattr(event, "source_announce_id", None),
            origin_remote_actor_id=origin_subscriber_actor_id,
            source_activity_json=source_json,
            canonical_relay_projection_json=_first_verified_projection(event),
        )
        deliveries = self.database.local_community_relay.create_missing_local_community_relay_deliveries(
            source_activity=source, targets=targets,
        )
        eligible = {item["remote_actor_id"]: item for item in targets}
        outbound = []
        claims = {}
        for row in deliveries:
            target = eligible.get(row.target_remote_actor_id)
            if target is None or target["remote_inbox_url"] != row.target_inbox_url:
                continue
            relay_id = _stable_relay_id(
                source=source, target_actor_id=row.target_remote_actor_id,
                community=local_community,
            )
            claim = self.database.local_community_relay.claim_local_community_relay_target(
                delivery_id=row.id, relay_activity_id=relay_id,
            )
            if claim is None:
                continue
            claims[row.id] = (claim.token, relay_id, row.target_remote_actor_id)
            try:
                activity = render_local_community_relay_activity(
                    source_activity_json=source.source_activity_json,
                    normalized_event=_frozen_source_event(source),
                    community_actor_url=getattr(local_community, "actor_url"),
                    community_slug=getattr(local_community, "slug"),
                    delivery_profile=row.delivery_profile,
                    object_kind=row.object_kind,
                    relay_activity_id=relay_id,
                )
            except Exception:
                # Rendering happened before HTTP; this failure is provably
                # unsent and may be retried with the same stable AP ID.
                self.database.local_community_relay.finish_local_community_relay_target(
                    delivery_id=row.id, token=claim.token, status="failed",
                    error="relay render failed before network",
                )
                raise
            outbound.append(SendLocalCommunityRelayDelivery(
                delivery_id=row.id, target_remote_actor_id=row.target_remote_actor_id,
                target_inbox_url=row.target_inbox_url, activity_json=activity,
            ))
        if not outbound:
            return RelayFanoutSummary(0, 0, 0)

        # Batch transport can fail after sending some or all targets. Never
        # interpret a thrown exception or missing outcome as an unsent request.
        try:
            result = await self.fedify_gateway.send_local_community_relay(
                signing_actor_url=getattr(local_community, "actor_url"),
                deliveries=outbound,
            )
            results = {outcome.delivery_id: outcome for outcome in result.outcomes}
        except Exception:
            logger.warning(
                "Unresolved local Group relay HTTP outcome tenant=%s source=%s targets=%s",
                getattr(local_community, "id"), source.id, list(claims),
                exc_info=True,
            )
            results = {}
        delivered = 0
        failed = 0
        for row in outbound:
            token, expected_id, expected_actor = claims[row.delivery_id]
            outcome = results.get(row.delivery_id)
            if (
                outcome is None or outcome.activity_id != expected_id
                or outcome.target_remote_actor_id != expected_actor
            ):
                status = "unknown"
                error = "missing or mismatched gateway outcome after possible send"
            elif outcome.ok:
                status, error = "delivered", None
            elif outcome.safe_to_retry:
                # The gateway must assert a *pre-network* failure. An HTTP 4xx,
                # timeout or transport exception is never such evidence.
                status, error = "failed", outcome.error
            else:
                status, error = "unknown", outcome.error
            if self.database.local_community_relay.finish_local_community_relay_target(
                delivery_id=row.delivery_id, token=token, status=status, error=error,
            ):
                delivered += status == "delivered"
                failed += status != "delivered"
        return RelayFanoutSummary(len(outbound), delivered, failed)
