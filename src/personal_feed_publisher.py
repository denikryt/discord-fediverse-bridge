"""Publish owner-authored personal-feed root messages as canonical User Actor Notes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timezone
from hashlib import sha256
from urllib.parse import quote

from .content_sync.persistence import (
    PublicationConflictError,
    inspect_publish_artifacts,
    persist_publish_artifacts,
)
from .fedify_gateway_client import RenderPersonalFeedRequest


@dataclass(frozen=True, slots=True)
class PersonalFeedPublishResult:
    """Describe one personal-feed admission or publication outcome."""

    status: str
    reason: str
    activity_id: str | None = None
    object_id: str | None = None


class PersonalFeedPublisher:
    """Own personal-feed root admission, rendering, atomic persistence, and worker wake."""

    def __init__(
        self,
        *,
        database: object,
        personal_feed_service: object,
        fedify_gateway: object,
        user_ban_service: object,
        bridge_policy_service: object,
        fedify_origin: str,
        delivery_worker: object | None = None,
    ) -> None:
        """Initialize the publisher with non-secret identity configuration and adapters."""
        self.database = database
        self.personal_feed_service = personal_feed_service
        self.fedify_gateway = fedify_gateway
        self.user_ban_service = user_ban_service
        self.bridge_policy_service = bridge_policy_service
        self.fedify_origin = fedify_origin.rstrip("/")
        self.delivery_worker = delivery_worker

    async def publish_root(
        self, *, message: object, binding: object
    ) -> PersonalFeedPublishResult:
        """Publish one currently bound owner-authored ordinary text-channel root."""
        guild_id = getattr(getattr(message, "guild", None), "id", None)
        channel_id = getattr(getattr(message, "channel", None), "id", None)
        message_id = getattr(message, "id", None)
        author_id = str(getattr(getattr(message, "author", None), "id", ""))
        if not all(
            isinstance(value, int) and value > 0
            for value in (guild_id, channel_id, message_id)
        ):
            return PersonalFeedPublishResult("ignored", "invalid_discord_identity")
        current = self.personal_feed_service.resolve_root_binding(guild_id, channel_id)
        if current is None or current.id != binding.id:
            return PersonalFeedPublishResult("ignored", "binding_changed")
        if author_id != str(current.discord_user_id):
            return PersonalFeedPublishResult("ignored", "not_owner")
        body = str(getattr(message, "content", ""))
        if body.strip() == "":
            return PersonalFeedPublishResult("ignored", "empty_content")
        user = self.database.users.get_user_by_discord_user_id(author_id)
        if user is None or int(user.id) != int(current.user_id):
            return PersonalFeedPublishResult("ignored", "identity_changed")
        if not self.bridge_policy_service.is_discord_guild_allowed(guild_id):
            return PersonalFeedPublishResult("rejected", "guild_disallowed")
        decision = self.user_ban_service.check_global_discord_user(author_id)
        if decision.banned:
            return PersonalFeedPublishResult("rejected", "user_banned")

        operation_id = sha256(
            f"personal-feed:{guild_id}:{channel_id}:{message_id}:{user.id}".encode()
        ).hexdigest()
        username = quote(str(user.activitypub_username), safe="")
        object_id = f"{self.fedify_origin}/users/{username}/post/{operation_id}"
        activity_id = f"{self.fedify_origin}/users/{username}/activities/create/post/{operation_id}"
        created_at = getattr(message, "created_at").astimezone(timezone.utc)
        published_at = created_at.isoformat(timespec="milliseconds").replace(
            "+00:00", "Z"
        )

        existing = inspect_publish_artifacts(
            self.database,
            source_id=str(message_id),
            actor_url=str(user.actor_url),
            publication_context="personal_feed",
            community_actor_url=None,
            discord_guild_id=guild_id,
            discord_channel_id=channel_id,
            discord_message_id=message_id,
            activity_id=activity_id,
            object_id=object_id,
            published_at=created_at,
        )
        if existing is not None:
            return PersonalFeedPublishResult(
                "already_published", "duplicate", activity_id, object_id
            )

        rendered = await self.fedify_gateway.render_personal_feed(
            RenderPersonalFeedRequest(
                actor_username=str(user.activitypub_username),
                operation_id=operation_id,
                activity_id=activity_id,
                object_id=object_id,
                body_markdown=body,
                published_at=published_at,
            )
        )
        if (
            rendered.activity_id != activity_id
            or rendered.object_id != object_id
            or rendered.published_at != published_at
        ):
            raise PublicationConflictError(
                "gateway render response changed deterministic identity"
            )
        persisted = persist_publish_artifacts(
            self.database,
            source_id=str(message_id),
            actor_username=str(user.activitypub_username),
            actor_url=str(user.actor_url),
            publication_context="personal_feed",
            community_actor_url=None,
            activity_id=activity_id,
            object_id=object_id,
            activity_json=rendered.activity_json,
            kind="post",
            title=None,
            body_markdown=body,
            in_reply_to_object_id=None,
            discord_guild_id=guild_id,
            discord_channel_id=channel_id,
            discord_message_id=message_id,
            published_at=created_at,
        )
        if persisted.created and self.delivery_worker is not None:
            self.delivery_worker.wake()
        return PersonalFeedPublishResult(
            "published" if persisted.created else "already_published",
            "published" if persisted.created else "duplicate",
            activity_id,
            object_id,
        )
