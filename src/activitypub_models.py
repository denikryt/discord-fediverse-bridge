from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ActivityPubObject(BaseModel):
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
    model_config = ConfigDict(extra="forbid")

    event_type: Literal["post.created", "comment.created"]
    delivery_id: str
    occurred_at: datetime
    community_actor_id: str
    actor_id: str
    object: ActivityPubObject

    @model_validator(mode="after")
    def validate_event_shape(self) -> "ActivityPubEvent":
        if self.event_type == "post.created" and self.object.kind != "post":
            raise ValueError("post.created requires object.kind='post'")
        if self.event_type == "comment.created" and self.object.kind != "comment":
            raise ValueError("comment.created requires object.kind='comment'")
        if self.event_type == "comment.created" and not self.object.post_ap_id:
            raise ValueError("comment.created requires object.post_ap_id")
        if self.event_type == "comment.created" and self.object.post_lemmy_id is None:
            raise ValueError("comment.created requires object.post_lemmy_id")
        return self
