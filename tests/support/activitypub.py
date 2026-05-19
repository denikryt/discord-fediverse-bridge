"""ActivityPub event builders used by inbound and lifecycle tests."""

from __future__ import annotations

from datetime import UTC, datetime

from src.activitypub_models import ActivityPubEvent, FollowLifecycleEvent
from tests_constants import BRIDGE_EXAMPLE_DOMAIN, LEMMY_EXAMPLE_DOMAIN

COMMUNITY_ACTOR_URL = f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers"


def build_post_created_event(
    *,
    object_id: str,
    community_actor_id: str = COMMUNITY_ACTOR_URL,
    actor_id: str | None = None,
    delivery_id: str | None = None,
    title: str = "Test post",
    body_markdown: str = "test body",
    lemmy_id: int = 99,
) -> ActivityPubEvent:
    """Build one normalized inbound `post.created` event."""
    return ActivityPubEvent.model_validate(
        {
            "event_type": "post.created",
            "delivery_id": delivery_id
            or f"https://{LEMMY_EXAMPLE_DOMAIN}/activities/create/post/{lemmy_id}",
            "occurred_at": datetime.now(UTC).isoformat(),
            "community_actor_id": community_actor_id,
            "actor_id": actor_id or f"https://{LEMMY_EXAMPLE_DOMAIN}/u/alice",
            "object": {
                "ap_id": object_id,
                "kind": "post",
                "lemmy_id": lemmy_id,
                "post_ap_id": None,
                "post_lemmy_id": None,
                "parent_ap_id": None,
                "title": title,
                "body_markdown": body_markdown,
                "url": object_id,
                "published_at": datetime.now(UTC).isoformat(),
                "author_name": "alice",
            },
        }
    )


def build_comment_created_event(
    *,
    object_id: str,
    post_ap_id: str,
    community_actor_id: str = COMMUNITY_ACTOR_URL,
    actor_id: str | None = None,
    parent_ap_id: str | None = None,
    delivery_id: str | None = None,
    lemmy_id: int = 55,
    post_lemmy_id: int = 99,
    body_markdown: str = "test comment body",
) -> ActivityPubEvent:
    """Build one normalized inbound `comment.created` event."""
    return ActivityPubEvent.model_validate(
        {
            "event_type": "comment.created",
            "delivery_id": delivery_id
            or f"https://{LEMMY_EXAMPLE_DOMAIN}/activities/create/comment/{lemmy_id}",
            "occurred_at": datetime.now(UTC).isoformat(),
            "community_actor_id": community_actor_id,
            "actor_id": actor_id or f"https://{LEMMY_EXAMPLE_DOMAIN}/u/alice",
            "object": {
                "ap_id": object_id,
                "kind": "comment",
                "lemmy_id": lemmy_id,
                "post_ap_id": post_ap_id,
                "post_lemmy_id": post_lemmy_id,
                "parent_ap_id": parent_ap_id,
                "title": None,
                "body_markdown": body_markdown,
                "url": object_id,
                "published_at": datetime.now(UTC).isoformat(),
                "author_name": "alice",
            },
        }
    )


def build_follow_accepted_event(
    *,
    community_actor_id: str = COMMUNITY_ACTOR_URL,
    follow_activity_id: str = f"https://{BRIDGE_EXAMPLE_DOMAIN}/activities/follow/1",
) -> FollowLifecycleEvent:
    """Build one normalized `follow.accepted` lifecycle event."""
    return FollowLifecycleEvent(
        event_type="follow.accepted",
        delivery_id=f"https://{LEMMY_EXAMPLE_DOMAIN}/activities/accept/1",
        occurred_at=datetime.now(UTC),
        community_actor_id=community_actor_id,
        actor_id=community_actor_id,
        object={"follow_activity_id": follow_activity_id},
    )


def build_internal_event_headers(delivery_id: str, *, secret: str = "secret") -> dict[str, str]:
    """Build the trusted internal-auth headers used by the HTTP bridge endpoint."""
    return {
        "Authorization": f"Bearer {secret}",
        "X-Bridge-Delivery-Id": delivery_id,
    }

