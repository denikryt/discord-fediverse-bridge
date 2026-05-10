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
    """Own outbound AP publish for Discord thread starters and replies.

    Responsibilities after Phase 5:
    - Validate subscription (accepted) and user registration.
    - Publish to ActivityPub via FedifyGatewayClient.
    - Persist MessageMapping and PublishedActivityObject for echo suppression
      and reply-chain resolution.

    Dedup, fanout, PostLink, and CommentLink are all owned by CommunityRuntime
    from Phase 5 onward. This service does not write PostLink or CommentLink rows.
    """

    def __init__(
        self,
        *,
        database: Database,
        fedify_gateway: FedifyGatewayClient,
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
        """Publish one Discord forum-thread starter as a user-authored AP post.

        Validates subscription and registration, then publishes to AP. Does not
        write PostLink rows — CommunityRuntime owns thread-group persistence.
        Dedup against duplicate thread events is enforced by CommunityRuntime
        via get_thread_group_by_source_thread before calling this method.
        """
        subscription = self.database.get_subscription_by_channel(
            getattr(thread, "parent_id")
        )
        if subscription is None:
            return PublishResult(status="ignored", reason="no_subscription")
        if subscription.status != "accepted":
            return PublishResult(status="ignored", reason="subscription_not_active")

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
        """Publish one Discord thread message as a user-authored AP comment.

        Resolves post context from CommunityThreadGroup (the sole path from Phase 5
        onward — PostLink fallback removed). Does not write CommentLink rows.
        Dedup against duplicate message events is enforced by CommunityRuntime via
        get_message_group_by_source_message before calling this method.
        """
        thread = getattr(message, "channel")
        subscription = self.database.get_subscription_by_channel(
            getattr(thread, "parent_id")
        )
        if subscription is None:
            return PublishResult(status="ignored", reason="no_subscription")
        if subscription.status != "accepted":
            return PublishResult(status="ignored", reason="subscription_not_active")

        # Resolve post context from CommunityThreadGroup — works for source, mirror,
        # and inbound threads (Phase 9). If no thread group exists, the thread
        # predates Phase 2 or was never registered, so AP publish is not possible.
        thread_group = self.database.get_thread_group_by_any_thread(getattr(thread, "id"))
        if thread_group is None or thread_group.ap_object_id is None:
            return PublishResult(status="ignored", reason="no_post_context")

        if thread_group.source_starter_message_id == getattr(message, "id"):
            return PublishResult(
                status="ignored", reason="starter_message_already_handled"
            )

        user = self.database.get_user_by_discord_user_id(
            str(getattr(getattr(message, "author"), "id"))
        )
        if user is None:
            await getattr(message, "reply")(UNREGISTERED_REPLY)
            return PublishResult(status="ignored", reason="unregistered_user")

        # Resolve the AP reply target using the message's Discord reference.
        # parent_ap_id is the comment AP id if replying to a known comment,
        # or None (falling back to the post AP id) for root-level replies.
        reply_target_ap_id = self._resolve_reply_target(
            message=message,
            thread_group=thread_group,
        )
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
                in_reply_to_object_id=reply_target_ap_id,
            )
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
            in_reply_to_object_id=reply_target_ap_id,
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

    def _resolve_reply_target(self, *, message: object, thread_group: object) -> str:
        """Resolve the AP object ID that this Discord reply should target.

        Uses CommunityMessageGroup.ap_object_id for replies to known prior messages.
        Falls back to the thread group's post AP object ID for root replies or
        when the referenced message has no known message group.

        Phase 9: Also checks if the referenced message is any starter message in
        the thread group (source, mirror, or inbound) — all treat as root reply.
        """
        post_ap_id = thread_group.ap_object_id
        reference = getattr(message, "reference", None)
        referenced_id = getattr(reference, "message_id", None) if reference else None

        if referenced_id is None:
            # Root reply to the post.
            return post_ap_id

        # Check if this is a reply to any starter message in the thread group.
        # Phase 9: Generalised to handle replies to mirror/inbound starters.
        thread_deliveries = self.database.get_thread_deliveries(thread_group.id)
        for delivery in thread_deliveries:
            if referenced_id == delivery.discord_starter_message_id:
                # Reply to any thread starter (source, mirror, or inbound) — targets the post.
                return post_ap_id

        # Look up whether the referenced Discord message belongs to a known
        # message group and resolve its AP object ID.
        parent_group = self.database.get_message_group_by_delivered_message(referenced_id)
        if parent_group is None or parent_group.ap_object_id is None:
            # Unknown reference (pre-Phase-3 or cross-thread) — fall back to post.
            return post_ap_id

        return parent_group.ap_object_id

    @staticmethod
    def _author_name(author: object) -> str:
        """Return the display name the bridge uses in outbound markdown content."""
        return getattr(author, "display_name", None) or getattr(author, "name")
