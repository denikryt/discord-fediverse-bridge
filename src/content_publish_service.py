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

from .config import Settings
from .bridge_policy import BridgePolicyService
from .user_bans import UserBanService, canonical_local_user_handle, render_ban_message

from .content_sync.outbound_publish import (
    build_discord_comment_body,
    build_discord_post_title,
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
        settings: Settings | None = None,
        bridge_policy_service: BridgePolicyService,
    ) -> None:
        """Initialise with the shared database, AP gateway, and bridge prefix."""
        self.database = database
        self.fedify_gateway = fedify_gateway
        self.bridge_prefix = bridge_prefix
        self.settings = settings
        self.bridge_policy_service = bridge_policy_service
        self.user_ban_service = UserBanService(database=database, settings=settings) if settings is not None else None

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
        decision = self.bridge_policy_service.federation_decision(
            subscription.lemmy_community_actor_id
        )
        if not decision.allowed:
            return PublishResult(status="ignored", reason=f"federation_policy_{decision.reason.value}")

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
        decision = self.bridge_policy_service.federation_decision(
            subscription.lemmy_community_actor_id
        )
        if not decision.allowed:
            return PublishResult(status="ignored", reason=f"federation_policy_{decision.reason.value}")

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
            target_inbox_urls=self._allowed_local_follower_inboxes(community_actor_url),
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
            target_inbox_urls=self._allowed_local_follower_inboxes(community_actor_url),
        )

    async def publish_post_to_community(
        self,
        *,
        thread: object,
        starter_message: object,
        community_actor_url: str,
        publish_call: Callable[[object], Awaitable[object]],
        target_inbox_urls: list[str] | None = None,
    ) -> PublishResult:
        """Publish one Discord thread starter through the supplied gateway path."""
        user, rejection = await self._resolve_publish_user(
            author=getattr(starter_message, "author"),
            reply_target=starter_message,
            community_actor_url=community_actor_url,
        )
        if rejection is not None:
            return rejection

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
                target_inbox_urls=target_inbox_urls,
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
        target_inbox_urls: list[str] | None = None,
    ) -> PublishResult:
        """Publish one Discord comment through the supplied gateway path."""
        thread = getattr(message, "channel")
        user, rejection = await self._resolve_publish_user(
            author=getattr(message, "author"),
            reply_target=message,
            community_actor_url=community_actor_url,
        )
        if rejection is not None:
            return rejection

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
                target_inbox_urls=target_inbox_urls,
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

    def canonical_author_name_for_discord(self, author: object) -> str:
        """Return a stable local handle when the Discord account is registered."""
        user = self.database.users.get_user_by_discord_user_id(str(getattr(author, "id")))
        if user is not None and self.settings is not None:
            return canonical_local_user_handle(
                username=str(user.activitypub_username), settings=self.settings
            )
        return self._author_name(author)

    async def _resolve_publish_user(
        self, *, author: object, reply_target: object, community_actor_url: str
    ) -> tuple[object | None, PublishResult | None]:
        """Resolve registration, then enforce global/community bans before side effects."""
        discord_user_id = str(getattr(author, "id"))
        user = self.database.users.get_user_by_discord_user_id(discord_user_id)
        if user is None:
            await getattr(reply_target, "reply")(UNREGISTERED_REPLY)
            return None, PublishResult(status="ignored", reason="unregistered_user")
        if self.user_ban_service is not None:
            try:
                local_community = self.database.local_communities.get_local_community_by_actor_url(community_actor_url)
                decision = self.user_ban_service.check_discord_user(
                    discord_user_id=discord_user_id,
                    local_community=local_community,
                )
            except Exception:
                # Database uncertainty must never reopen the publish path. The
                # generic message avoids falsely claiming that the user is banned.
                logger.exception("Discord publish ban lookup failed for user %s", discord_user_id)
                try:
                    await getattr(reply_target, "reply")(
                        "The bridge could not verify publishing access. Please try again later."
                    )
                except Exception:
                    logger.exception("Failed to send publishing access verification error")
                return None, PublishResult(status="rejected", reason="ban_check_failed")
            if decision.banned:
                try:
                    await getattr(reply_target, "reply")(render_ban_message(decision))
                except Exception:
                    # Rejection delivery is best-effort; moderation remains fail-closed.
                    logger.exception("Failed to send Discord ban rejection for user %s", discord_user_id)
                return None, PublishResult(status="rejected", reason="user_banned")
        return user, None


    def _allowed_local_follower_inboxes(self, community_actor_url: str) -> list[str]:
        """Return accepted follower inboxes permitted by one current snapshot."""
        local_community = self.database.local_communities.get_local_community_by_actor_url(
            community_actor_url
        )
        if local_community is None:
            return []
        followers = self.database.remote_subscribers.list_remote_subscribers(
            getattr(local_community, "id"), status="accepted"
        )
        if self.bridge_policy_service is None:
            return [str(row.remote_inbox_url) for row in followers]
        snapshot = self.bridge_policy_service.snapshot()
        return [
            str(row.remote_inbox_url)
            for row in followers
            if snapshot.federation_decision(str(row.remote_inbox_url)).allowed
        ]

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
        target_inbox_urls: list[str] | None = None,
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
            target_inbox_urls=target_inbox_urls,
        )
