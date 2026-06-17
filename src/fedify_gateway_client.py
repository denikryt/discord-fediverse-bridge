"""Internal HTTP client for bridge-to-gateway federation operations."""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx

from .config import Settings


@dataclass(slots=True)
class FollowCommunityResult:
    # The follow result captures the exact remote metadata that must be stored
    # on the local subscription row so later Accept matching has one source of
    # truth and moderator-visible state stays explainable.
    community_actor_url: str
    community_inbox_url: str
    follow_activity_id: str


@dataclass(slots=True)
class UnfollowCommunityResult:
    """Describe whether the gateway accepted one Undo(Follow) cleanup request.

    Python-side unsubscribe logic needs an explicit success/failure contract so
    it can preserve `bridge_actor_follows` rows until remote cleanup succeeds.
    """

    accepted: bool
    error: str | None = None


@dataclass(slots=True)
class PublishContentRequest:
    # The Python bridge decides actor ownership and reply context, then passes
    # the normalized publish intent to the gateway for signed AP delivery.
    actor_username: str
    community_actor_url: str
    kind: str
    title: str | None
    body_markdown: str
    in_reply_to_object_id: str | None
    target_inbox_urls: list[str] | None = None


@dataclass(slots=True)
class PublishContentResult:
    """Return the exact canonical Create rendered and sent by the gateway."""

    activity_id: str
    object_id: str
    community_actor_url: str
    activity_json: dict = field(default_factory=dict)


@dataclass(slots=True)
class PublishLocalCommunityContentResult:
    """Describe one local-community publish fanout outcome.

    Local-community publishes still produce one canonical AP activity/object id
    pair, but the gateway also reports how many accepted remote subscribers received the
    delivery so Python can distinguish this path from ordinary remote-community
    publishes.
    """

    activity_id: str
    object_id: str
    community_actor_url: str
    delivered_follower_count: int
    failed_follower_count: int
    activity_json: dict = field(default_factory=dict)


@dataclass(slots=True)
class RenderPersonalFeedRequest:
    """Carry one deterministic render-only personal-feed publication request."""

    actor_username: str
    operation_id: str
    activity_id: str
    object_id: str
    body_markdown: str
    published_at: str


@dataclass(slots=True)
class RenderPersonalFeedResult:
    """Return one canonical render-only Create(Note) result."""

    activity_id: str
    object_id: str
    published_at: str
    activity_json: dict = field(default_factory=dict)


@dataclass(slots=True)
class CanonicalMutationResult:
    """Return one stable canonical Update/Delete rendered by the gateway."""

    activity_id: str
    object_id: str
    activity_json: dict


@dataclass(slots=True)
class UpdateContentRequest:
    # actorUsername must match the original object attributedTo — Lemmy enforces
    # this ownership check and rejects Updates from any other actor.
    actor_username: str
    community_actor_url: str
    ap_object_id: str
    kind: str
    body_markdown: str
    # title is required for posts; None for comments.
    title: str | None = None
    # in_reply_to_object_id is required for comments to identify the parent post.
    # Lemmy will not process comment updates without this field.
    in_reply_to_object_id: str | None = None
    target_inbox_urls: list[str] | None = None
    operation_id: str | None = None
    activity_id: str | None = None
    mutation_timestamp: str | None = None
    expected_revision: int | None = None
    next_revision: int | None = None


@dataclass(slots=True)
class DeleteContentRequest:
    # actorUsername must match the original attributedTo — same ownership rule.
    actor_username: str
    community_actor_url: str
    ap_object_id: str
    target_inbox_urls: list[str] | None = None
    operation_id: str | None = None
    activity_id: str | None = None
    mutation_timestamp: str | None = None
    expected_revision: int | None = None
    next_revision: int | None = None


@dataclass(slots=True)
class SendLocalCommunityRelayDelivery:
    """Carry one already-rendered relay activity to one remote inbox."""

    delivery_id: int
    target_remote_actor_id: str
    target_inbox_url: str
    activity_json: dict


@dataclass(slots=True)
class SendLocalCommunityRelayOutcome:
    """Describe one gateway delivery result for a relay target."""

    delivery_id: int
    ok: bool
    target_remote_actor_id: str
    activity_id: str | None = None
    error: str | None = None


@dataclass(slots=True)
class SendLocalCommunityRelayResult:
    """Return all per-target outcomes for one relay request."""

    outcomes: list[SendLocalCommunityRelayOutcome]


@dataclass(slots=True)
class UserActorDeliveryOutcome:
    """Describe one per-endpoint User Actor transport outcome."""

    delivery_id: int
    ok: bool
    error: str | None = None


@dataclass(slots=True)
class AcceptUserActorFollowRequest:
    """Carry one signed Accept(Follow) request for a registered User Actor."""

    actor_username: str
    actor_url: str
    remote_actor_id: str
    remote_inbox_url: str
    follow_activity_id: str


@dataclass(slots=True)
class AcceptLocalCommunityFollowRequest:
    """Carry one signed Accept(Follow) request for a local community actor."""

    community_slug: str
    community_actor_url: str
    remote_actor_id: str
    remote_inbox_url: str
    follow_activity_id: str


class FedifyGatewayClient:
    """Call the local Fedify gateway for follow and publish operations."""

    # The Python bridge owns moderator-facing subscription policy, but the
    # actual Follow delivery is delegated to the gateway because it owns the
    # bridge actor identity and HTTP-signature-capable Fedify context.
    def __init__(self, settings: Settings) -> None:
        self._base_url = str(settings.fedify_gateway_url).rstrip("/")
        self._shared_secret = settings.fedify_shared_secret
        self._client = httpx.AsyncClient(base_url=self._base_url, timeout=15.0)

    async def close(self) -> None:
        """Close the underlying HTTP client when the runtime shuts down."""
        await self._client.aclose()

    async def accept_user_actor_follow(
        self,
        *,
        actor_username: str,
        actor_url: str,
        remote_actor_id: str,
        remote_inbox_url: str,
        follow_activity_id: str,
    ) -> None:
        """Ask the gateway to sign and deliver Accept as the owning User Actor."""
        response = await self._client.post(
            "/accept-user-actor-follow",
            headers={"Authorization": f"Bearer {self._shared_secret}"},
            json={
                "actorUsername": actor_username,
                "actorUrl": actor_url,
                "remoteActorId": remote_actor_id,
                "remoteInboxUrl": remote_inbox_url,
                "followActivityId": follow_activity_id,
            },
        )
        response.raise_for_status()

    async def follow_community(self, community_actor_url: str) -> FollowCommunityResult:
        """Trigger one gateway-side Follow and return the stored metadata."""
        # The request is authenticated because this endpoint is a trusted
        # bridge-to-gateway contract, not a public federation surface.
        response = await self._client.post(
            "/follow-community",
            headers={"Authorization": f"Bearer {self._shared_secret}"},
            json={"communityActorUrl": community_actor_url},
        )
        response.raise_for_status()
        payload = response.json()
        return FollowCommunityResult(
            community_actor_url=payload["communityActorUrl"],
            community_inbox_url=payload["communityInboxUrl"],
            follow_activity_id=payload["followActivityId"],
        )

    async def render_personal_feed(
        self, request: RenderPersonalFeedRequest
    ) -> RenderPersonalFeedResult:
        """Render one canonical personal-feed Create without delivery or signing."""
        response = await self._client.post(
            "/render-personal-feed",
            headers={"Authorization": f"Bearer {self._shared_secret}"},
            json={
                "actorUsername": request.actor_username,
                "operationId": request.operation_id,
                "activityId": request.activity_id,
                "objectId": request.object_id,
                "bodyMarkdown": request.body_markdown,
                "publishedAt": request.published_at,
            },
        )
        response.raise_for_status()
        payload = response.json()
        return RenderPersonalFeedResult(
            activity_id=payload["activityId"],
            object_id=payload["objectId"],
            published_at=payload["publishedAt"],
            activity_json=payload["activityJson"],
        )

    async def publish_content(
        self, request: PublishContentRequest
    ) -> PublishContentResult:
        """Trigger one gateway-side user-authored Create delivery."""
        response = await self._client.post(
            "/publish",
            headers={"Authorization": f"Bearer {self._shared_secret}"},
            json={
                "actorUsername": request.actor_username,
                "communityActorUrl": request.community_actor_url,
                "kind": request.kind,
                "title": request.title,
                "bodyMarkdown": request.body_markdown,
                "inReplyToObjectId": request.in_reply_to_object_id,
            },
        )
        response.raise_for_status()
        payload = response.json()
        return PublishContentResult(
            activity_id=payload["activityId"],
            object_id=payload["objectId"],
            community_actor_url=payload["communityActorUrl"],
            activity_json=payload["activityJson"],
        )

    async def publish_local_community_content(
        self, request: PublishContentRequest
    ) -> PublishLocalCommunityContentResult:
        """Trigger one gateway-side Create fanout for a Discord-backed community.

        Unlike ordinary publish delivery, this path targets every accepted
        follower inbox of one local community instead of one remote community
        actor inbox.
        """
        response = await self._client.post(
            "/publish-local-community",
            headers={"Authorization": f"Bearer {self._shared_secret}"},
            json={
                "actorUsername": request.actor_username,
                "communityActorUrl": request.community_actor_url,
                "kind": request.kind,
                "title": request.title,
                "bodyMarkdown": request.body_markdown,
                "inReplyToObjectId": request.in_reply_to_object_id,
                "targetInboxUrls": request.target_inbox_urls,
                "operationId": request.operation_id,
                "activityId": request.activity_id,
                "mutationTimestamp": request.mutation_timestamp,
            },
        )
        response.raise_for_status()
        payload = response.json()
        return PublishLocalCommunityContentResult(
            activity_id=payload["activityId"],
            object_id=payload["objectId"],
            community_actor_url=payload["communityActorUrl"],
            delivered_follower_count=int(payload["deliveredFollowerCount"]),
            failed_follower_count=int(payload["failedFollowerCount"]),
            activity_json=payload["activityJson"],
        )

    async def unfollow_community(
        self, community_actor_url: str, follow_activity_id: str
    ) -> UnfollowCommunityResult:
        """Trigger one gateway-side Undo(Follow) to remove a community follow.

        Called by the unsubscribe flow when the last channel subscription for a
        community is deleted. The gateway signs and delivers the Undo activity.
        """
        try:
            response = await self._client.post(
                "/unfollow-community",
                headers={"Authorization": f"Bearer {self._shared_secret}"},
                json={
                    "communityActorUrl": community_actor_url,
                    "followActivityId": follow_activity_id,
                },
            )
        except httpx.HTTPError as exc:
            # Transport failures are surfaced as an explicit failed result so
            # unsubscribe can keep retry state without relying on exceptions.
            return UnfollowCommunityResult(accepted=False, error=str(exc))

        if not response.is_success:
            return UnfollowCommunityResult(
                accepted=False,
                error=_extract_error_message(response),
            )

        payload = response.json()
        if payload.get("ok") is not True:
            return UnfollowCommunityResult(
                accepted=False,
                error="gateway did not confirm Undo(Follow) delivery",
            )
        return UnfollowCommunityResult(accepted=True)

    async def update_content(
        self, request: UpdateContentRequest
    ) -> CanonicalMutationResult:
        """Trigger and return one canonical gateway-side Update activity."""
        response = await self._client.post(
            "/update",
            headers={"Authorization": f"Bearer {self._shared_secret}"},
            json={
                "actorUsername": request.actor_username,
                "communityActorUrl": request.community_actor_url,
                "apObjectId": request.ap_object_id,
                "kind": request.kind,
                "bodyMarkdown": request.body_markdown,
                "title": request.title,
                "inReplyToObjectId": request.in_reply_to_object_id,
                "targetInboxUrls": request.target_inbox_urls,
                "operationId": request.operation_id,
                "activityId": request.activity_id,
                "mutationTimestamp": request.mutation_timestamp,
                "expectedRevision": request.expected_revision,
                "nextRevision": request.next_revision,
            },
        )
        response.raise_for_status()
        payload = response.json()
        return CanonicalMutationResult(
            activity_id=payload["activityId"],
            object_id=payload["objectId"],
            activity_json=payload["activityJson"],
        )

    async def delete_content(
        self, request: DeleteContentRequest
    ) -> CanonicalMutationResult:
        """Trigger and return one canonical gateway-side Delete activity."""
        response = await self._client.post(
            "/delete",
            headers={"Authorization": f"Bearer {self._shared_secret}"},
            json={
                "actorUsername": request.actor_username,
                "communityActorUrl": request.community_actor_url,
                "apObjectId": request.ap_object_id,
                "targetInboxUrls": request.target_inbox_urls,
                "operationId": request.operation_id,
                "activityId": request.activity_id,
                "mutationTimestamp": request.mutation_timestamp,
                "expectedRevision": request.expected_revision,
                "nextRevision": request.next_revision,
            },
        )
        response.raise_for_status()
        payload = response.json()
        return CanonicalMutationResult(
            activity_id=payload["activityId"],
            object_id=payload["objectId"],
            activity_json=payload["activityJson"],
        )

    async def send_local_community_relay(
        self,
        *,
        signing_actor_url: str,
        deliveries: list[SendLocalCommunityRelayDelivery],
    ) -> SendLocalCommunityRelayResult:
        """Ask the gateway to sign and send already-rendered relay activities."""
        response = await self._client.post(
            "/send-local-community-relay",
            headers={"Authorization": f"Bearer {self._shared_secret}"},
            json={
                "signingActorUrl": signing_actor_url,
                "deliveries": [
                    {
                        "deliveryId": delivery.delivery_id,
                        "targetRemoteActorId": delivery.target_remote_actor_id,
                        "targetInboxUrl": delivery.target_inbox_url,
                        "activityJson": delivery.activity_json,
                    }
                    for delivery in deliveries
                ],
            },
        )
        response.raise_for_status()
        payload = response.json()
        return SendLocalCommunityRelayResult(
            outcomes=[
                SendLocalCommunityRelayOutcome(
                    delivery_id=int(item["deliveryId"]),
                    ok=bool(item["ok"]),
                    target_remote_actor_id=str(item["targetRemoteActorId"]),
                    activity_id=item.get("activityId"),
                    error=item.get("error"),
                )
                for item in payload.get("outcomes", [])
            ]
        )

    async def render_user_actor_profile_update(
        self, *, activity_id: str, profile_snapshot: dict
    ) -> dict:
        """Render one complete Update(Person) from immutable non-secret input."""
        response = await self._client.post(
            "/render-user-actor-profile-update",
            headers={"Authorization": f"Bearer {self._shared_secret}"},
            json={"activityId": activity_id, "profileSnapshot": profile_snapshot},
        )
        response.raise_for_status()
        return response.json()["activityJson"]

    async def send_user_actor_deliveries(
        self,
        *,
        signing_actor_url: str,
        actor_username: str,
        deliveries: list[dict],
    ) -> list[UserActorDeliveryOutcome]:
        """Send already-rendered activities and retain independent target outcomes."""
        response = await self._client.post(
            "/send-user-actor-deliveries",
            headers={"Authorization": f"Bearer {self._shared_secret}"},
            json={
                "signingActorUrl": signing_actor_url,
                "actorUsername": actor_username,
                "deliveries": deliveries,
            },
        )
        response.raise_for_status()
        return [
            UserActorDeliveryOutcome(
                delivery_id=int(item["deliveryId"]),
                ok=bool(item["ok"]),
                error=item.get("error"),
            )
            for item in response.json()["outcomes"]
        ]

    async def accept_local_community_follow(
        self,
        *,
        community_slug: str,
        community_actor_url: str,
        remote_actor_id: str,
        remote_inbox_url: str,
        follow_activity_id: str,
    ) -> None:
        """Trigger one gateway-side Accept(Follow) for a local community actor."""
        response = await self._client.post(
            "/accept-local-community-follow",
            headers={"Authorization": f"Bearer {self._shared_secret}"},
            json={
                "communitySlug": community_slug,
                "communityActorUrl": community_actor_url,
                "remoteActorId": remote_actor_id,
                "remoteInboxUrl": remote_inbox_url,
                "followActivityId": follow_activity_id,
            },
        )
        response.raise_for_status()


def _extract_error_message(response: httpx.Response) -> str:
    """Return the most useful gateway error message for operator-facing retry logs."""
    try:
        payload = response.json()
    except ValueError:
        return f"gateway returned HTTP {response.status_code}"

    error = payload.get("error")
    if isinstance(error, str) and error:
        return error
    return f"gateway returned HTTP {response.status_code}"
