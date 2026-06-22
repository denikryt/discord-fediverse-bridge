"""Remote Follow and Undo(Follow) handling for local communities.

The follower handler owns federation policy checks, remote-subscriber
idempotency, Accept(Follow) delivery, and unfollow removal for local community
actors.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..bridge_policy import BridgePolicyService
from ..db import Database
from ..fedify_gateway_client import FedifyGatewayClient
from ..inbound_activity_outcomes import InboundActivityOutcome
from ..remote_delivery_endpoints import (
    RemoteDeliveryEndpointError,
    admit_remote_delivery_endpoint,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ..activitypub_handlers import HandlerResult


class LocalCommunityFollowerHandler:
    """Handle remote Follow/Undo(Follow) for local community actors."""

    def __init__(
        self,
        *,
        database: Database,
        fedify_gateway: FedifyGatewayClient,
        bridge_policy_service: BridgePolicyService,
    ) -> None:
        """Store the persistence, gateway, and policy dependencies for follows."""
        self.database = database
        self.fedify_gateway = fedify_gateway
        self.bridge_policy_service = bridge_policy_service

    async def handle_follow_request(
        self,
        *,
        local_community_actor_id: str,
        remote_actor_id: str,
        remote_inbox_url: str,
        follow_activity_id: str,
    ) -> HandlerResult:
        """Persist and accept one remote follow request for a local community."""
        from ..activitypub_handlers import HandlerResult as _HandlerResult

        local_community = (
            self.database.local_communities.get_local_community_by_actor_url(
                local_community_actor_id
            )
        )
        if local_community is None:
            return _HandlerResult(
                status="skipped",
                outcome=InboundActivityOutcome.IGNORED_UNKNOWN_LOCAL_COMMUNITY,
                detail="unknown local community",
            )
        decision = self.bridge_policy_service.federation_decision(
            remote_inbox_url or remote_actor_id
        )
        if not decision.allowed:
            return _HandlerResult(
                status="skipped",
                outcome=(
                    InboundActivityOutcome.IGNORED_INSTANCE_BLOCKLISTED
                    if decision.reason.value == "blocklisted"
                    else InboundActivityOutcome.IGNORED_INSTANCE_NOT_ALLOWLISTED
                ),
                detail="remote follow denied by federation policy",
            )

        try:
            admitted_inbox_url = admit_remote_delivery_endpoint(
                remote_inbox_url, local_origin=local_community_actor_id
            )
        except RemoteDeliveryEndpointError as exc:
            return _HandlerResult(
                status="skipped",
                outcome=InboundActivityOutcome.IGNORED_UNMAPPED_CONTEXT,
                detail=str(exc),
            )

        try:
            with self.database.immediate_session() as session:
                # The relationship row and Follow-ID uniqueness guard must be
                # decided together so concurrent duplicate Follows cannot leak
                # as raw IntegrityError or create duplicate fanout targets.
                transition = (
                    self.database.remote_subscribers.create_or_refresh_accepted(
                        session,
                        local_community_id=getattr(local_community, "id"),
                        remote_actor_id=remote_actor_id,
                        remote_inbox_url=admitted_inbox_url,
                        follow_activity_id=follow_activity_id,
                    )
                )
        except Exception as exc:
            from ..db.repositories.remote_subscribers import FollowActivityIdCollision

            if isinstance(exc, FollowActivityIdCollision):
                logger.warning(
                    "Rejected reused local-community Follow ID id=%s actor=%s target=%s",
                    follow_activity_id,
                    remote_actor_id,
                    local_community_actor_id,
                )
                return _HandlerResult(
                    status="skipped",
                    outcome=InboundActivityOutcome.IGNORED_ALREADY_APPLIED,
                    detail="Follow ID belongs to another remote-subscriber relationship",
                )
            raise
        detail = f"local community remote subscriber {transition.transition}"

        logger.info(
            "Accepting local-community Follow community=%s remote_actor=%s inbox=%s follow_activity=%s",
            getattr(local_community, "slug"),
            remote_actor_id,
            admitted_inbox_url,
            follow_activity_id,
        )
        await self.fedify_gateway.accept_local_community_follow(
            community_slug=getattr(local_community, "slug"),
            community_actor_url=getattr(local_community, "actor_url"),
            remote_actor_id=remote_actor_id,
            remote_inbox_url=admitted_inbox_url,
            follow_activity_id=follow_activity_id,
        )
        return _HandlerResult(
            status="processed", outcome=InboundActivityOutcome.APPLIED, detail=detail
        )

    async def handle_unfollow_request(
        self,
        *,
        local_community_actor_id: str,
        remote_actor_id: str,
        follow_activity_id: str | None,
    ) -> HandlerResult:
        """Remove one remote actor from a local community remote-subscriber set."""
        from ..activitypub_handlers import HandlerResult as _HandlerResult

        local_community = (
            self.database.local_communities.get_local_community_by_actor_url(
                local_community_actor_id
            )
        )
        if local_community is None:
            return _HandlerResult(
                status="skipped",
                outcome=InboundActivityOutcome.IGNORED_UNKNOWN_LOCAL_COMMUNITY,
                detail="unknown local community",
            )
        remote_subscriber = self.database.remote_subscribers.get_remote_subscriber(
            local_community_id=getattr(local_community, "id"),
            remote_actor_id=remote_actor_id,
        )
        if remote_subscriber is None:
            return _HandlerResult(
                status="skipped",
                outcome=InboundActivityOutcome.IGNORED_UNKNOWN_FOLLOW,
                detail="local community remote subscriber not found",
            )
        if (
            follow_activity_id is not None
            and getattr(remote_subscriber, "follow_activity_id") != follow_activity_id
        ):
            logger.info(
                "Local-community unfollow Follow ID mismatch community=%s remote_actor=%s stored=%s incoming=%s",
                getattr(local_community, "slug"),
                remote_actor_id,
                getattr(remote_subscriber, "follow_activity_id"),
                follow_activity_id,
            )
        self.database.remote_subscribers.delete_remote_subscriber(
            local_community_id=getattr(local_community, "id"),
            remote_actor_id=remote_actor_id,
        )
        return _HandlerResult(
            status="processed",
            outcome=InboundActivityOutcome.APPLIED,
            detail="local community remote subscriber removed",
        )
