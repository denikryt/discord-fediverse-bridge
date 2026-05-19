"""Gateway-side fake builders for bridge scenario tests."""

from __future__ import annotations

from unittest.mock import AsyncMock

from src.fedify_gateway_client import PublishContentResult
from tests_constants import BRIDGE_HOST_DOMAIN, LEMMY_EXAMPLE_DOMAIN

COMMUNITY_ACTOR_URL = f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers"


def build_publish_result(
    *,
    kind: str = "post",
    activity_id: str | None = None,
    object_id: str | None = None,
    community_actor_url: str = COMMUNITY_ACTOR_URL,
) -> PublishContentResult:
    """Build one stable publish result returned by the fake gateway."""
    return PublishContentResult(
        activity_id=activity_id
        or f"https://{BRIDGE_HOST_DOMAIN}/users/alice/activities/create/{kind}/1",
        object_id=object_id
        or f"https://{BRIDGE_HOST_DOMAIN}/users/alice/objects/{kind}/1",
        community_actor_url=community_actor_url,
    )


def build_gateway_mock(
    *,
    publish_kind: str = "post",
    activity_id: str | None = None,
    object_id: str | None = None,
    community_actor_url: str = COMMUNITY_ACTOR_URL,
) -> AsyncMock:
    """Build a gateway mock with a stable `publish_content` return value."""
    gateway = AsyncMock()
    gateway.publish_content.return_value = build_publish_result(
        kind=publish_kind,
        activity_id=activity_id,
        object_id=object_id,
        community_actor_url=community_actor_url,
    )
    return gateway

