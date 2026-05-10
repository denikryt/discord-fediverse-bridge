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


@dataclass(slots=True)
class DeleteContentRequest:
    # actorUsername must match the original attributedTo — same ownership rule.
    actor_username: str
    community_actor_url: str
    ap_object_id: str


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
