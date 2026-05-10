"""Publish Discord-originated content through registered local AP user actors."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .db import Database
from .fedify_gateway_client import FedifyGatewayClient, PublishContentRequest
from .formatting import format_discord_body_for_lemmy, format_thread_title_for_discord

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


class DiscordPublishService:
    """Own outbound publish policy for Discord thread starters and replies."""

    # The service centralizes all Stage 6 decisions around registration,
    # accepted subscriptions, dedup, reply-target resolution, and persistence
    # so the Discord bot stays focused on event intake only.
    def __init__(
        self,
        *,
        database: Database,
        fedify_gateway: FedifyGatewayClient,
        bridge_prefix: str,
    ) -> None:
        self.database = database
        self.fedify_gateway = fedify_gateway
        self.bridge_prefix = bridge_prefix

    async def publish_thread_starter(
        self,
        *,
        thread: object,
        starter_message: object,
    ) -> PublishResult:
        """Publish one Discord forum-thread starter as a user-authored AP post."""
        subscription = self.database.get_subscription_by_channel(
            getattr(thread, "parent_id")
        )
        if subscription is None:
            return PublishResult(status="ignored", reason="no_subscription")
        if subscription.status != "accepted":
            return PublishResult(
                status="ignored", reason="subscription_not_active"
            )
        if self.database.get_post_link_by_thread_id(getattr(thread, "id")) is not None:
            return PublishResult(
                status="ignored", reason="duplicate_discord_thread"
            )
        if self.database.get_message_mapping_by_discord_message_id(
            getattr(starter_message, "id")
        ) is not None:
            return PublishResult(
                status="ignored", reason="duplicate_discord_message"
            )

        user = self.database.get_user_by_discord_user_id(
            str(getattr(getattr(starter_message, "author"), "id"))
        )
        if user is None:
            await getattr(starter_message, "reply")(UNREGISTERED_REPLY)
            return PublishResult(status="ignored", reason="unregistered_user")

        author_name = self._author_name(getattr(starter_message, "author"))
        body = format_discord_body_for_lemmy(
            author_name,
            getattr(starter_message, "content"),
            self.bridge_prefix,
        )
        title = format_thread_title_for_discord(getattr(thread, "name"))
        publish_result = await self.fedify_gateway.publish_content(
            PublishContentRequest(
                actor_username=user.activitypub_username,
                community_actor_url=subscription.lemmy_community_actor_id,
                kind="post",
                title=title,
                body_markdown=body,
                in_reply_to_object_id=None,
            )
        )

        # PostLink stays active in Stage 6 so later thread replies can resolve
        # the post context immediately, before any remote Announce loops back.
        self.database.create_post_link(
            lemmy_post_id=-int(getattr(thread, "id")),
            lemmy_post_ap_id=publish_result.object_id,
            discord_forum_thread_id=getattr(thread, "id"),
            discord_starter_message_id=getattr(starter_message, "id"),
            direction="discord_to_activitypub",
        )
        self.database.create_message_mapping(
            source_platform="discord",
            source_id=str(getattr(starter_message, "id")),
            activity_id=publish_result.activity_id,
            object_id=publish_result.object_id,
            actor_url=user.actor_url,
            community_actor_url=subscription.lemmy_community_actor_id,
            discord_channel_id=getattr(thread, "parent_id"),
            discord_message_id=getattr(starter_message, "id"),
        )
        # The durable object store lets the gateway later serve this exact AP
        # object back to Lemmy when reply chains reference its canonical URL.
        self.database.create_published_activity_object(
            actor_username=user.activitypub_username,
            actor_url=user.actor_url,
            community_actor_url=subscription.lemmy_community_actor_id,
            activity_id=publish_result.activity_id,
            object_id=publish_result.object_id,
            kind="post",
            title=title,
            body_markdown=body,
            in_reply_to_object_id=None,
            discord_channel_id=getattr(thread, "parent_id"),
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

    async def publish_thread_message(self, *, message: object) -> PublishResult:
        """Publish one Discord thread message as a user-authored AP comment."""
        thread = getattr(message, "channel")
        subscription = self.database.get_subscription_by_channel(
            getattr(thread, "parent_id")
        )
        if subscription is None:
            return PublishResult(status="ignored", reason="no_subscription")
        if subscription.status != "accepted":
            return PublishResult(
                status="ignored", reason="subscription_not_active"
            )
        if self.database.get_message_mapping_by_discord_message_id(
            getattr(message, "id")
        ) is not None:
            return PublishResult(
                status="ignored", reason="duplicate_discord_message"
            )
        if self.database.has_comment_link_for_discord_message(getattr(message, "id")):
            return PublishResult(
                status="ignored", reason="duplicate_discord_message"
            )

        post_link = self.database.get_post_link_by_thread_id(getattr(thread, "id"))
        if post_link is None or post_link.lemmy_post_ap_id is None:
            # Backward-compat fallback for Phase 2+ threads that have a
            # CommunityThreadGroup but no PostLink. Remove when Phase 5 drops
            # PostLink and rewrites this method to use CommunityThreadGroup directly.
            thread_group = self.database.get_thread_group_by_source_thread(getattr(thread, "id"))
            if thread_group is None or thread_group.ap_object_id is None:
                return PublishResult(status="ignored", reason="no_post_context")
            from types import SimpleNamespace
            post_link = SimpleNamespace(
                lemmy_post_ap_id=thread_group.ap_object_id,
                discord_starter_message_id=thread_group.source_starter_message_id,
            )
        if post_link.discord_starter_message_id == getattr(message, "id"):
            return PublishResult(
                status="ignored", reason="starter_message_already_handled"
            )

        user = self.database.get_user_by_discord_user_id(
            str(getattr(getattr(message, "author"), "id"))
        )
        if user is None:
            await getattr(message, "reply")(UNREGISTERED_REPLY)
            return PublishResult(status="ignored", reason="unregistered_user")

        target = self._resolve_comment_target(post_link=post_link, message=message)
        author_name = self._author_name(getattr(message, "author"))
        body = format_discord_body_for_lemmy(
            author_name,
            getattr(message, "content"),
            self.bridge_prefix,
        )
        publish_result = await self.fedify_gateway.publish_content(
            PublishContentRequest(
                actor_username=user.activitypub_username,
                community_actor_url=subscription.lemmy_community_actor_id,
                kind="comment",
                title=None,
                body_markdown=body,
                in_reply_to_object_id=target.reply_target_object_id,
            )
        )

        # CommentLink remains the current runtime parent lookup table, while
        # MessageMapping captures the generic AP activity/object ids for dedup.
        self.database.create_comment_link(
            lemmy_comment_id=-int(getattr(message, "id")),
            lemmy_comment_ap_id=publish_result.object_id,
            lemmy_parent_comment_ap_id=target.parent_comment_ap_id,
            lemmy_post_id=-int(getattr(thread, "id")),
            discord_forum_thread_id=getattr(thread, "id"),
            discord_message_id=getattr(message, "id"),
            direction="discord_to_activitypub",
        )
        self.database.create_message_mapping(
            source_platform="discord",
            source_id=str(getattr(message, "id")),
            activity_id=publish_result.activity_id,
            object_id=publish_result.object_id,
            actor_url=user.actor_url,
            community_actor_url=subscription.lemmy_community_actor_id,
            discord_channel_id=getattr(thread, "parent_id"),
            discord_message_id=getattr(message, "id"),
        )
        # Store the published comment body and parent object so local gateway
        # URLs can be resolved without relying on transient HTTP state later.
        self.database.create_published_activity_object(
            actor_username=user.activitypub_username,
            actor_url=user.actor_url,
            community_actor_url=subscription.lemmy_community_actor_id,
            activity_id=publish_result.activity_id,
            object_id=publish_result.object_id,
            kind="comment",
            title=None,
            body_markdown=body,
            in_reply_to_object_id=target.reply_target_object_id,
            discord_channel_id=getattr(thread, "parent_id"),
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

    def _resolve_comment_target(self, *, post_link: object, message: object):
        """Resolve the AP object that one Discord reply should target."""
        reference = getattr(message, "reference", None)
        reference_message_id = (
            getattr(reference, "message_id", None) if reference is not None else None
        )
        if reference_message_id is None:
            return _CommentTarget(
                reply_target_object_id=getattr(post_link, "lemmy_post_ap_id"),
                parent_comment_ap_id=None,
            )
        if reference_message_id == getattr(post_link, "discord_starter_message_id"):
            return _CommentTarget(
                reply_target_object_id=getattr(post_link, "lemmy_post_ap_id"),
                parent_comment_ap_id=None,
            )

        parent_link = self.database.get_comment_link_by_discord_message_id(
            reference_message_id
        )
        if (
            parent_link is None
            or parent_link.discord_forum_thread_id != getattr(getattr(message, "channel"), "id")
            or parent_link.lemmy_comment_ap_id is None
        ):
            return _CommentTarget(
                reply_target_object_id=getattr(post_link, "lemmy_post_ap_id"),
                parent_comment_ap_id=None,
            )

        return _CommentTarget(
            reply_target_object_id=parent_link.lemmy_comment_ap_id,
            parent_comment_ap_id=parent_link.lemmy_comment_ap_id,
        )

    @staticmethod
    def _author_name(author: object) -> str:
        """Return the display name the bridge uses in outbound markdown content."""
        return getattr(author, "display_name", None) or getattr(author, "name")


@dataclass(slots=True)
class _CommentTarget:
    """Carry the chosen AP reply target and optional parent-comment identity."""

    reply_target_object_id: str
    parent_comment_ap_id: str | None
