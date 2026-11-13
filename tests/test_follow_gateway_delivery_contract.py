"""Authenticated gateway Follow delivery outcome classification tests.

Only the verified pre-HTTP signing failure unlocks the persisted SQLite Follow
for a safe same-ID retry. An HTTP error body is not delivery evidence.
"""

from __future__ import annotations

import httpx
import pytest

from src.fedify_gateway_client import (
    FedifyGatewayClient,
    FollowCommunityResult,
    FollowNotDispatchedError,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "payload", "expected"),
    [
        (422, {"code": "follow_sender_unavailable", "dispatched": False}, FollowNotDispatchedError),
        (422, {"code": "follow_sender_unavailable", "dispatched": True}, httpx.HTTPStatusError),
        (500, {"error": 'No actor found for the given username "services/bridge".'}, httpx.HTTPStatusError),
        (503, {"error": "upstream unavailable"}, httpx.HTTPStatusError),
    ],
)
async def test_only_structured_pre_network_failure_unlocks_follow(
    status: int, payload: dict, expected: type[Exception],
) -> None:
    """Test the real HTTP client path with a controlled authenticated gateway."""
    client = object.__new__(FedifyGatewayClient)
    client._shared_secret = "secret"

    async def respond(request: httpx.Request) -> httpx.Response:
        """Require the internal authorization and preserve source identity."""
        assert request.headers["Authorization"] == "Bearer secret"
        assert request.url.path == "/send-prepared-community-follow"
        assert b"follow-42" in request.content
        return httpx.Response(status, json=payload)

    client._client = httpx.AsyncClient(
        base_url="http://gateway.test", transport=httpx.MockTransport(respond),
    )
    try:
        with pytest.raises(expected):
            await client.send_prepared_community_follow(FollowCommunityResult(
                community_actor_url="https://nodebb.example/category/42",
                community_inbox_url="https://nodebb.example/category/42/inbox",
                follow_activity_id="https://bridge.example/activities/follow-42",
            ))
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_successful_gateway_response_means_synchronous_follow_http_completed() -> None:
    """The gateway's 200 is transport completion, never a signed Group Accept."""
    client = object.__new__(FedifyGatewayClient)
    client._shared_secret = "secret"
    client._client = httpx.AsyncClient(
        base_url="http://gateway.test",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True})),
    )
    try:
        await client.send_prepared_community_follow(FollowCommunityResult(
            community_actor_url="https://nodebb.example/category/42",
            community_inbox_url="https://nodebb.example/category/42/inbox",
            follow_activity_id="https://bridge.example/activities/follow-42",
        ))
    finally:
        await client.close()
