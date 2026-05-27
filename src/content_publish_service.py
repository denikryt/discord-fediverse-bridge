"""Shared outbound ActivityPub publish service for Discord-authored content.

The module owns the generic Discord -> ActivityPub create path used by both
remote-subscription mode and local-community mode. Runtime layers call this
service by its canonical `ContentPublishService` name so publish terminology is
no longer tied to the older Discord-only service label.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

from .content_sync.outbound_publish import (
    build_discord_comment_body,
    build_discord_post_title,
    resolve_registered_user,
)
from .content_sync.persistence import persist_publish_artifacts

logger = logging.getLogger(__name__)

UNREGISTERED_REPLY = (
    "Your message was not federated because you do not have a registered "
    "federated identity yet. Use /register to create one."
)


@dataclass(slots=True)
class PublishResult:
    """Report the observable result of one Discord-originated publish attempt."""

    status: str
    reason: str
    activity_id: str | None = None
    object_id: str | None = None


class ContentPublishService:
    """Own reusable outbound AP create behavior for Discord-authored content.

    Responsibilities:
    - validate author registration
    - format Discord bodies and titles for AP publish
    - call the gateway publish boundary
    - persist the generic mapping/object rows shared by both bridge modes

    Mode-specific runtimes still own routing, dedup, reply-table lookups, and
    canonical thread/message mapping tables.
    """

    def __init__(
        self,
        *,
        database: object,
        fedify_gateway: object,
        bridge_prefix: str,
    ) -> None:
        """Initialise with the shared database, AP gateway, and bridge prefix."""
        self.database = database
        self.fedify_gateway = fedify_gateway
        self.bridge_prefix = bridge_prefix

    async def publish_thread_starter(
        self,
        *,
        thread: object,
        starter_message: object,
    ) -> PublishResult:
        """Publish one Discord forum-thread starter into a remote subscribed community."""
        subscription = self.database.remote_subscriptions.get_subscription_by_channel(getattr(thread, "parent_id"))
        if subscription is None:
            return PublishResult(status="ignored", reason="no_subscription")
        if subscription.status != "accepted":
            return PublishResult(status="ignored", reason="subscription_not_active")

        return await self.publish_post_to_community(
            thread=thread,
            starter_message=starter_message,
            community_actor_url=subscription.lemmy_community_actor_id,
            publish_call=self.fedify_gateway.publish_content,
        )

    async def publish_thread_message(self, *, message: object) -> PublishResult:
        """Publish one Discord thread message into a remote subscribed community."""
        thread = getattr(message, "channel")
        subscription = self.database.remote_subscriptions.get_subscription_by_channel(getattr(thread, "parent_id"))
        if subscription is None:
            return PublishResult(status="ignored", reason="no_subscription")
        if subscription.status != "accepted":
            return PublishResult(status="ignored", reason="subscription_not_active")

        thread_group = self.database.discord_fanout_groups.get_thread_group_by_any_thread(getattr(thread, "id"))
        if thread_group is None or thread_group.ap_object_id is None:
            return PublishResult(status="ignored", reason="no_post_context")

        if thread_group.source_starter_message_id == getattr(message, "id"):
            return PublishResult(status="ignored", reason="starter_message_already_handled")

        reply_target_ap_id = self._resolve_reply_target(message=message, thread_group=thread_group)
        return await self.publish_comment_to_community(
            message=message,
            community_actor_url=subscription.lemmy_community_actor_id,
            parent_object_id=reply_target_ap_id,
            publish_call=self.fedify_gateway.publish_content,
        )

    async def publish_local_thread_starter(
        self,
        *,
        thread: object,
        starter_message: object,
        community_actor_url: str,
    ) -> PublishResult:
        """Publish one Discord forum-thread starter into a local federated community."""
        return await self.publish_post_to_community(
            thread=thread,
            starter_message=starter_message,
            community_actor_url=community_actor_url,
            publish_call=self.fedify_gateway.publish_local_community_content,
        )

    async def publish_local_thread_message(
        self,
        *,
        message: object,
        community_actor_url: str,
        parent_object_id: str,
    ) -> PublishResult:
        """Publish one Discord reply inside a local community thread."""
        return await self.publish_comment_to_community(
            message=message,
            community_actor_url=community_actor_url,
            parent_object_id=parent_object_id,
            publish_call=self.fedify_gateway.publish_local_community_content,
        )

    async def publish_post_to_community(
        self,
        *,
        thread: object,
        starter_message: object,
        community_actor_url: str,
        publish_call: Callable[[object], Awaitable[object]],
    ) -> PublishResult:
        """Publish one Discord thread starter through the supplied gateway path."""
        user = await resolve_registered_user(
            database=self.database,
            author=getattr(starter_message, "author"),
            reply_target=starter_message,
            unregistered_reply=UNREGISTERED_REPLY,
        )
        if user is None:
            return PublishResult(status="ignored", reason="unregistered_user")

        author_name = self._author_name(getattr(starter_message, "author"))
        body = build_discord_comment_body(
            author_name=author_name,
            content=getattr(starter_message, "content"),
            bridge_prefix=self.bridge_prefix,
        )
        title = build_discord_post_title(thread_name=getattr(thread, "name"))
        publish_result = await publish_call(
            self._build_publish_request(
                actor_username=user.activitypub_username,
                community_actor_url=community_actor_url,
                kind="post",
                title=title,
                body_markdown=body,
                in_reply_to_object_id=None,
            )
        )

        persist_publish_artifacts(
            self.database,
            source_id=str(getattr(starter_message, "id")),
            actor_username=user.activitypub_username,
            actor_url=user.actor_url,
            community_actor_url=community_actor_url,
            activity_id=publish_result.activity_id,
            object_id=publish_result.object_id,
            kind="post",
            title=title,
            body_markdown=body,
            in_reply_to_object_id=None,
            discord_channel_id=getattr(thread, "parent_id", None),
            discord_message_id=getattr(starter_message, "id"),
        )
        logger.info(
            "Published Discord thread %s starter %s via AP actor %s",
            getattr(thread, "id"),
            getattr(starter_message, "id"),
            user.activitypub_username,
        )
        return PublishResult(
            status="published",
            reason="published",
            activity_id=publish_result.activity_id,
            object_id=publish_result.object_id,
        )

    async def publish_comment_to_community(
        self,
        *,
        message: object,
        community_actor_url: str,
        parent_object_id: str,
        publish_call: Callable[[object], Awaitable[object]],
    ) -> PublishResult:
        """Publish one Discord comment through the supplied gateway path."""
        thread = getattr(message, "channel")
        user = await resolve_registered_user(
            database=self.database,
            author=getattr(message, "author"),
            reply_target=message,
            unregistered_reply=UNREGISTERED_REPLY,
        )
        if user is None:
            return PublishResult(status="ignored", reason="unregistered_user")

        author_name = self._author_name(getattr(message, "author"))
        body = build_discord_comment_body(
            author_name=author_name,
            content=getattr(message, "content"),
            bridge_prefix=self.bridge_prefix,
        )
        publish_result = await publish_call(
            self._build_publish_request(
                actor_username=user.activitypub_username,
                community_actor_url=community_actor_url,
                kind="comment",
                title=None,
                body_markdown=body,
                in_reply_to_object_id=parent_object_id,
            )
        )

        persist_publish_artifacts(
            self.database,
            source_id=str(getattr(message, "id")),
            actor_username=user.activitypub_username,
            actor_url=user.actor_url,
            community_actor_url=community_actor_url,
            activity_id=publish_result.activity_id,
            object_id=publish_result.object_id,
            kind="comment",
            title=None,
            body_markdown=body,
            in_reply_to_object_id=parent_object_id,
            discord_channel_id=getattr(thread, "parent_id", None),
            discord_message_id=getattr(message, "id"),
        )
        logger.info(
            "Published Discord message %s in thread %s via AP actor %s",
            getattr(message, "id"),
            getattr(thread, "id"),
            user.activitypub_username,
        )
        return PublishResult(
            status="published",
            reason="published",
            activity_id=publish_result.activity_id,
            object_id=publish_result.object_id,
        )

    def _resolve_reply_target(self, *, message: object, thread_group: object) -> str:
        """Resolve the AP object ID that one remote-subscription reply should target."""
        post_ap_id = thread_group.ap_object_id
        reference = getattr(message, "reference", None)
        referenced_id = getattr(reference, "message_id", None) if reference else None

        if referenced_id is None:
            return post_ap_id

        thread_deliveries = self.database.discord_fanout_groups.get_thread_deliveries(thread_group.id)
        for delivery in thread_deliveries:
            if referenced_id == delivery.discord_starter_message_id:
                return post_ap_id

        parent_group = self.database.discord_fanout_groups.get_message_group_by_delivered_message(referenced_id)
        if parent_group is None or parent_group.ap_object_id is None:
            return post_ap_id
        return parent_group.ap_object_id

    @staticmethod
    def _author_name(author: object) -> str:
        """Resolve the best human-readable author label from a Discord author object."""
        return (
            getattr(author, "display_name", None)
            or getattr(author, "global_name", None)
            or getattr(author, "name", None)
            or "unknown"
        )

    @staticmethod
    def _build_publish_request(
        *,
        actor_username: str,
        community_actor_url: str,
        kind: str,
        title: str | None,
        body_markdown: str,
        in_reply_to_object_id: str | None,
    ) -> object:
        """Build one gateway publish request without importing mode-specific code."""
        from .fedify_gateway_client import PublishContentRequest

        return PublishContentRequest(
            actor_username=actor_username,
            community_actor_url=community_actor_url,
            kind=kind,
            title=title,
            body_markdown=body_markdown,
            in_reply_to_object_id=in_reply_to_object_id,
        )
