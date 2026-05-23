"""Typed gateway-to-Python ActivityPub event contract models."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ActivityPubObject(BaseModel):
    # This is the normalized object shape that the Python bridge accepts from
    # the Fedify gateway, regardless of raw ActivityPub vocabulary details.
    model_config = ConfigDict(extra="forbid")

    ap_id: str
    kind: Literal["post", "comment"]
    lemmy_id: int = Field(alias="lemmy_id")
    post_ap_id: str | None = None
    post_lemmy_id: int | None = None
    parent_ap_id: str | None = None
    title: str | None = None
    body_markdown: str | None = None
    url: str
    published_at: datetime
    author_name: str


class ActivityPubEvent(BaseModel):
    # Event validation keeps the internal HTTP contract strict so malformed or
    # mismatched gateway payloads fail before side effects happen.
    model_config = ConfigDict(extra="forbid")

    event_type: Literal[
        "post.created",
        "post.updated",
        "post.deleted",
        "comment.created",
        "comment.updated",
        "comment.deleted",
    ]
    delivery_id: str
    occurred_at: datetime
    community_actor_id: str
    actor_id: str
    object: ActivityPubObject
    source_activity_json: dict[str, Any] | None = None
    source_activity_id: str | None = None
    source_announce_id: str | None = None

    @model_validator(mode="after")
    def validate_event_shape(self) -> "ActivityPubEvent":
        # The event type and object kind must stay in sync because downstream
        # handlers branch on that contract instead of re-inspecting payloads.
        post_events = {"post.created", "post.updated", "post.deleted"}
        comment_events = {"comment.created", "comment.updated", "comment.deleted"}
        if self.event_type in post_events and self.object.kind != "post":
            raise ValueError(f"{self.event_type} requires object.kind='post'")
        if self.event_type in comment_events and self.object.kind != "comment":
            raise ValueError(f"{self.event_type} requires object.kind='comment'")
        if self.event_type == "comment.created" and not self.object.post_ap_id:
            raise ValueError("comment.created requires object.post_ap_id")
        if self.event_type == "comment.created" and self.object.post_lemmy_id is None:
            raise ValueError("comment.created requires object.post_lemmy_id")
        return self


class FollowLifecycleObject(BaseModel):
    # Follow lifecycle events carry state-transition identifiers, not content
    # payloads, so they stay separate from the post/comment object contract.
    model_config = ConfigDict(extra="forbid")

    follow_activity_id: str


class FollowLifecycleEvent(BaseModel):
    # Follow acceptance is a different class of internal bridge event from
    # content fanout, so it gets its own typed shape and validation rules.
    model_config = ConfigDict(extra="forbid")

    event_type: Literal["follow.accepted"]
    delivery_id: str
    occurred_at: datetime
    community_actor_id: str
    actor_id: str
    object: FollowLifecycleObject


class LocalCommunityFollowRequestObject(BaseModel):
    """Carry the identifiers needed to persist and accept a remote follow."""

    model_config = ConfigDict(extra="forbid")

    follow_activity_id: str
    remote_inbox_url: str


class LocalCommunityFollowRequestEvent(BaseModel):
    """Describe one inbound Follow addressed to a local community actor."""

    model_config = ConfigDict(extra="forbid")

    event_type: Literal["local.follow_requested"]
    delivery_id: str
    occurred_at: datetime
    community_actor_id: str
    actor_id: str
    object: LocalCommunityFollowRequestObject


class LocalCommunityUnfollowRequestObject(BaseModel):
    """Carry the original Follow ID when a remote actor unfollows a local community."""

    model_config = ConfigDict(extra="forbid")

    follow_activity_id: str | None = None


class LocalCommunityUnfollowRequestEvent(BaseModel):
    """Describe one inbound Undo(Follow) addressed to a local community actor."""

    model_config = ConfigDict(extra="forbid")

    event_type: Literal["local.unfollow_requested"]
    delivery_id: str
    occurred_at: datetime
    community_actor_id: str
    actor_id: str
    object: LocalCommunityUnfollowRequestObject


BridgeGatewayEvent = Annotated[
    ActivityPubEvent | FollowLifecycleEvent | LocalCommunityFollowRequestEvent | LocalCommunityUnfollowRequestEvent,
    Field(discriminator="event_type"),
]
