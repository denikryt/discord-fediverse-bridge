"""Typed gateway-to-Python ActivityPub event contract models."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def activity_actor_id(value: Any) -> str | None:
    """Read one actor ID from a string or an embedded ActivityPub actor."""
    if isinstance(value, dict):
        value = value.get("id")
    return value if isinstance(value, str) and value else None


class ActivityPubObject(BaseModel):
    # This is the normalized object shape that the Python bridge accepts from
    # the Fedify gateway, regardless of raw ActivityPub vocabulary details.
    model_config = ConfigDict(extra="forbid")

    ap_id: str
    kind: Literal["post", "comment"]
    lemmy_id: int | None = Field(default=None, alias="lemmy_id")
    post_ap_id: str | None = None
    post_lemmy_id: int | None = None
    parent_ap_id: str | None = None
    title: str | None = None
    body_markdown: str | None = None
    url: str | None = None
    published_at: datetime
    author_name: str
    attributed_to_actor_id: str | None = None


class ActivityPubTarget(BaseModel):
    """Discriminate community and personal-feed routing without fake ownership."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["community", "personal_feed"]
    root_object_id: str | None = None


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
    community_actor_id: str | None = None
    # Gateway-owned recipient proof. Only authenticated local-Group content
    # uses it; never derive this identity in Python from arbitrary addressing.
    verified_destination_group_actor_id: str | None = None
    target: ActivityPubTarget | None = None
    actor_id: str
    # Server-generated evidence from the outer Fedify-verified activity.
    # An Announce cannot certify that its embedded Person signed a Create.
    verified_sender_actor_id: str | None = None
    object: ActivityPubObject
    source_activity_json: dict[str, Any] | None = None
    source_activity_id: str | None = None
    source_announce_id: str | None = None

    @model_validator(mode="after")
    def validate_event_shape(self) -> "ActivityPubEvent":
        # A scoped receipt must be tied to exactly the canonical community
        # selected at the signed inbox boundary. Personal feeds stay global.
        verified = self.verified_destination_group_actor_id
        if verified is not None and (
            not verified or verified != self.community_actor_id
            or (self.target is not None and self.target.kind != "community")
        ):
            raise ValueError("verified destination must match community actor")
        # The event type and object kind must stay in sync because downstream
        # handlers branch on that contract instead of re-inspecting payloads.
        post_events = {"post.created", "post.updated", "post.deleted"}
        comment_events = {"comment.created", "comment.updated", "comment.deleted"}
        if self.event_type in post_events and self.object.kind != "post":
            raise ValueError(f"{self.event_type} requires object.kind='post'")
        if self.event_type in comment_events and self.object.kind != "comment":
            raise ValueError(f"{self.event_type} requires object.kind='comment'")
        personal = self.target is not None and self.target.kind == "personal_feed"
        if personal:
            if self.community_actor_id is not None:
                raise ValueError("personal_feed target cannot carry community_actor_id")
            if not self.target.root_object_id:
                raise ValueError("personal_feed target requires root_object_id")
            if self.object.kind != "comment":
                raise ValueError("personal_feed inbound content must be a comment")
            if not self.object.parent_ap_id:
                raise ValueError("personal_feed comment requires parent_ap_id")
            if not self.object.attributed_to_actor_id:
                raise ValueError(
                    "personal_feed content requires attributed_to_actor_id"
                )
        else:
            if not self.community_actor_id:
                raise ValueError("community content requires community_actor_id")
            if self.event_type == "comment.created" and not self.object.post_ap_id:
                raise ValueError("comment.created requires object.post_ap_id")
            if (
                self.event_type == "comment.created"
                and self.object.post_lemmy_id is None
            ):
                raise ValueError("comment.created requires object.post_lemmy_id")
            if self.object.lemmy_id is None:
                raise ValueError("community content requires object.lemmy_id")
        return self


class RemoteGroupAnnounceObject(BaseModel):
    """One verified Group distribution target, not a Person-signed Create."""

    model_config = ConfigDict(extra="forbid")
    ap_id: str
    inline_object: dict[str, Any] | None = None
    inner_create_id: str | None = None


class RemoteGroupAnnounceEvent(BaseModel):
    """Pass a signed Group Announce to Python for secure NodeBB object resolution.

    Fedify validates the incoming request before the gateway builds this event.
    Python must additionally validate the accepted NodeBB Follow and canonical
    Group identity before it trusts an inline object or fetches an URI target.
    """

    model_config = ConfigDict(extra="forbid")
    event_type: Literal["remote.group_announced"]
    actor_id: str
    community_actor_id: str
    delivery_id: str
    occurred_at: datetime
    object: RemoteGroupAnnounceObject
    source_activity_json: dict[str, Any]

    @model_validator(mode="after")
    def validate_signed_envelope_identity(self) -> "RemoteGroupAnnounceEvent":
        """Reject internal mismatches before receipt admission or any fetch."""
        source = self.source_activity_json
        if (source.get("type") != "Announce" or source.get("id") != self.delivery_id
                or source.get("actor") != self.actor_id or self.actor_id != self.community_actor_id):
            raise ValueError("Group distribution identity conflicts with signed envelope")
        obj = source.get("object")
        if isinstance(obj, dict) and obj.get("type") == "Create":
            if obj.get("id") != self.object.inner_create_id:
                raise ValueError("inner Create identity conflicts with signed envelope")
            obj = obj.get("object")
        source_id = obj if isinstance(obj, str) else obj.get("id") if isinstance(obj, dict) else None
        if source_id != self.object.ap_id:
            raise ValueError("Group distributed object identity mismatch")
        if isinstance(obj, dict) and obj != self.object.inline_object:
            raise ValueError("Group distributed inline object mismatch")
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

    event_type: Literal["follow.accepted", "follow.rejected"]
    delivery_id: str
    occurred_at: datetime
    community_actor_id: str | None = None
    target: ActivityPubTarget | None = None
    actor_id: str
    object: FollowLifecycleObject


class LocalFollowRequestObject(BaseModel):
    """Carry the identifiers needed to persist and accept a remote follow."""

    model_config = ConfigDict(extra="forbid")

    follow_activity_id: str
    remote_inbox_url: str
    remote_shared_inbox_url: str | None = None


class LocalFollowRequestEvent(BaseModel):
    """Describe one inbound Follow addressed to a local community actor."""

    model_config = ConfigDict(extra="forbid")

    event_type: Literal["local.follow_requested"]
    delivery_id: str
    occurred_at: datetime
    target_actor_kind: Literal["community", "user"]
    target_actor_id: str
    actor_id: str
    object: LocalFollowRequestObject


class LocalUnfollowRequestObject(BaseModel):
    """Carry the original Follow ID when a remote actor unfollows a local community."""

    model_config = ConfigDict(extra="forbid")

    follow_activity_id: str | None = None


class LocalUnfollowRequestEvent(BaseModel):
    """Describe one inbound Undo(Follow) addressed to a local community actor."""

    model_config = ConfigDict(extra="forbid")

    event_type: Literal["local.unfollow_requested"]
    delivery_id: str
    occurred_at: datetime
    target_actor_kind: Literal["community", "user"]
    target_actor_id: str
    actor_id: str
    object: LocalUnfollowRequestObject


class RemotePersonObject(BaseModel):
    """One public remote Person Note, independent of Lemmy numeric IDs."""

    model_config = ConfigDict(extra="forbid")
    ap_id: str
    author_name: str | None = None
    avatar_url: str | None = None
    body_markdown: str | None = None
    published_at: datetime | None = None


class RemotePersonContentEvent(BaseModel):
    """A Fedify-authenticated remote Person root Create/Update/Delete."""

    model_config = ConfigDict(extra="forbid")
    event_type: Literal["remote.person.created", "remote.person.updated", "remote.person.deleted"]
    delivery_id: str
    occurred_at: datetime
    actor_id: str
    verified_sender_actor_id: str
    source_activity_id: str | None = None
    source_activity_json: dict[str, Any]
    object: RemotePersonObject

    @model_validator(mode="after")
    def check_signed_identity(self) -> "RemotePersonContentEvent":
        """Reject any inner actor that differs from Fedify's signed sender."""
        if not self.actor_id or self.actor_id != self.verified_sender_actor_id:
            raise ValueError('remote Person actor must match signed sender')
        if not self.object.ap_id:
            raise ValueError('remote Person object ID is required')
        return self


class RemotePersonCommentObject(BaseModel):
    """One canonical public reply attributed to a separately verified Person."""

    model_config = ConfigDict(extra="forbid")
    ap_id: str
    parent_ap_id: str | None = None
    author_name: str | None = None
    body_markdown: str | None = None
    published_at: datetime | None = None


class RemotePersonCommentEvent(BaseModel):
    """A signed Note reply to a known, user-subscribed remote conversation."""

    model_config = ConfigDict(extra="forbid")
    event_type: Literal[
        "remote.person.comment.created", "remote.person.comment.updated",
        "remote.person.comment.deleted",
    ]
    delivery_id: str
    occurred_at: datetime
    actor_id: str
    verified_sender_actor_id: str
    source_activity_id: str | None = None
    source_activity_json: dict[str, Any]
    object: RemotePersonCommentObject

    @model_validator(mode="after")
    def check_signed_identity(self) -> "RemotePersonCommentEvent":
        """Prevent anonymous activity or creation without a specific parent."""
        if not self.actor_id or self.actor_id != self.verified_sender_actor_id:
            raise ValueError("remote Person comment actor must match signed sender")
        if not self.object.ap_id:
            raise ValueError("remote Person comment object ID is required")
        if not self.event_type.endswith(".deleted") and not self.object.parent_ap_id:
            raise ValueError("remote Person comment requires inReplyTo")
        return self


BridgeGatewayEvent = Annotated[
    ActivityPubEvent
    | RemoteGroupAnnounceEvent
    | RemotePersonContentEvent
    | RemotePersonCommentEvent
    | FollowLifecycleEvent
    | LocalFollowRequestEvent
    | LocalUnfollowRequestEvent,
    Field(discriminator="event_type"),
]
