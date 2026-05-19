"""Internal HTTP client for bridge-to-gateway federation operations."""

from __future__ import annotations

from dataclasses import dataclass

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


@dataclass(slots=True)
class PublishContentResult:
    # The returned IDs are the canonical AP identifiers Python must persist for
    # dedup and later inbound loop suppression.
    activity_id: str
    object_id: str
    community_actor_url: str


@dataclass(slots=True)
class PublishLocalCommunityContentResult:
    """Describe one local-community publish fanout outcome.

    Local-community publishes still produce one canonical AP activity/object id
    pair, but the gateway also reports how many accepted followers received the
    delivery so Python can distinguish this path from ordinary remote-community
    publishes.
    """

    activity_id: str
    object_id: str
    community_actor_url: str
    delivered_follower_count: int
    failed_follower_count: int


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


@dataclass(slots=True)
class DeleteContentRequest:
    # actorUsername must match the original attributedTo — same ownership rule.
    actor_username: str
    community_actor_url: str
    ap_object_id: str


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

    async def update_content(self, request: UpdateContentRequest) -> None:
        """Trigger one gateway-side Update activity for an edited post or comment."""
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
            },
        )
        response.raise_for_status()

    async def delete_content(self, request: DeleteContentRequest) -> None:
        """Trigger one gateway-side Delete activity for a deleted post or comment."""
        response = await self._client.post(
            "/delete",
            headers={"Authorization": f"Bearer {self._shared_secret}"},
            json={
                "actorUsername": request.actor_username,
                "communityActorUrl": request.community_actor_url,
                "apObjectId": request.ap_object_id,
            },
        )
        response.raise_for_status()

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
