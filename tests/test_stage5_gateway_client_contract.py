"""Python/gateway canonical mutation HTTP response contracts for Stage 5."""

from __future__ import annotations

import json

import httpx
import pytest

from src.fedify_gateway_client import (
    DeleteContentRequest,
    FedifyGatewayClient,
    UpdateContentRequest,
)


@pytest.mark.asyncio
async def test_update_and_delete_endpoints_return_exact_canonical_activity_json() -> None:
    requests: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append({"path": request.url.path, "payload": payload})
        kind = "Update" if request.url.path == "/update" else "Delete"
        activity = {
            "id": payload["activityId"],
            "type": kind,
            "actor": "https://bridge.example/actors/alice",
            "object": (
                {
                    "id": payload["apObjectId"],
                    "type": "Note",
                    "inReplyTo": payload.get("inReplyToObjectId"),
                    "audience": payload["communityActorUrl"],
                }
                if kind == "Update"
                else payload["apObjectId"]
            ),
        }
        return httpx.Response(
            200,
            json={
                "activityId": payload["activityId"],
                "objectId": payload["apObjectId"],
                "activityJson": activity,
            },
        )

    client = object.__new__(FedifyGatewayClient)
    client._shared_secret = "secret"
    client._client = httpx.AsyncClient(
        base_url="http://gateway.test", transport=httpx.MockTransport(handler)
    )
    try:
        update = await client.update_content(
            UpdateContentRequest(
                actor_username="alice",
                community_actor_url="https://remote.example/c/group",
                ap_object_id="https://bridge.example/users/alice/comment/1",
                kind="comment",
                body_markdown="edited",
                in_reply_to_object_id="https://remote.example/post/1",
                operation_id="operation-update",
                activity_id="https://bridge.example/users/alice/activities/update/operation-update",
                mutation_timestamp="2026-06-16T12:00:00+00:00",
                expected_revision=1,
                next_revision=2,
            )
        )
        delete = await client.delete_content(
            DeleteContentRequest(
                actor_username="alice",
                community_actor_url="https://remote.example/c/group",
                ap_object_id="https://bridge.example/users/alice/comment/1",
                operation_id="operation-delete",
                activity_id="https://bridge.example/users/alice/activities/delete/operation-delete",
                mutation_timestamp="2026-06-16T12:01:00+00:00",
                expected_revision=2,
                next_revision=3,
            )
        )
    finally:
        await client.close()

    assert update.activity_json["type"] == "Update"
    assert update.activity_json["object"]["inReplyTo"] == "https://remote.example/post/1"
    assert delete.activity_json == {
        "id": "https://bridge.example/users/alice/activities/delete/operation-delete",
        "type": "Delete",
        "actor": "https://bridge.example/actors/alice",
        "object": "https://bridge.example/users/alice/comment/1",
    }
    assert [item["path"] for item in requests] == ["/update", "/delete"]
    assert requests[0]["payload"]["expectedRevision"] == 1
    assert requests[1]["payload"]["nextRevision"] == 3
