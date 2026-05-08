"""Internal HTTP client for bridge-to-gateway follow lifecycle calls."""

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


class FedifyGatewayClient:
    """Call the local Fedify gateway for follow lifecycle operations."""

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
