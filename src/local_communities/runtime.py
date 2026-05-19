"""Runtime orchestration for the Discord-backed local-community mode.

LocalCommunityRuntime owns only the local-community flow: Discord forum threads
and messages published into one local federated community surface, plus remote
followers posting and replying back into Discord.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

import discord

from ..db import Database
from ..fedify_gateway_client import FedifyGatewayClient, PublishContentRequest
from ..formatting import format_discord_body_for_lemmy, format_thread_title_for_discord
from ..discord_publish_service import UNREGISTERED_REPLY
from .delivery_mapping import (
    get_local_community_for_forum,
    get_local_community_message_for_discord_message,
    get_local_community_thread_for_ap_object,
    get_local_community_thread_for_discord_thread,
)
from .reply_mapping import resolve_inbound_reply_target, resolve_outbound_reply_context

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ..activitypub_handlers import HandlerResult


@dataclass(slots=True)
class LocalCommunityRuntimeResult:
    """Report the observable result of one local-community runtime action."""

    status: str
    reason: str
    activity_id: str | None = None
    object_id: str | None = None


class LocalCommunityRuntime:
    """Own local-community Discord/AP orchestration around a narrow runtime API."""

    def __init__(
        self,
        *,
        database: Database,
        fedify_gateway: FedifyGatewayClient,
        bridge_prefix: str,
        bot: object | None = None,
    ) -> None:
        """Initialise the local-community runtime with shared long-lived services."""
        self.database = database
        self.fedify_gateway = fedify_gateway
        self.bridge_prefix = bridge_prefix
        self.bot = bot

    async def handle_discord_thread_create(
        self,
        *,
        thread: object,
        starter_message: object,
    ) -> LocalCommunityRuntimeResult:
        """Publish one Discord thread starter into a local community as a post."""
        local_community = get_local_community_for_forum(
            self.database, getattr(thread, "parent_id")
        )
        if local_community is None:
            return LocalCommunityRuntimeResult(status="ignored", reason="not_local_community")
        if get_local_community_thread_for_discord_thread(self.database, getattr(thread, "id")) is not None:
            return LocalCommunityRuntimeResult(status="ignored", reason="duplicate_thread")

        user = self.database.get_user_by_discord_user_id(
            str(getattr(getattr(starter_message, "author"), "id"))
        )
        if user is None:
            await getattr(starter_message, "reply")(UNREGISTERED_REPLY)
            return LocalCommunityRuntimeResult(status="ignored", reason="unregistered_user")

        author_name = self._author_name(getattr(starter_message, "author"))
        body = format_discord_body_for_lemmy(
            author_name,
            getattr(starter_message, "content"),
            self.bridge_prefix,
        )
        title = format_thread_title_for_discord(getattr(thread, "name"))
        # Local communities publish to accepted follower inboxes, not to one
        # remote community inbox, so they must use the dedicated gateway path.
        publish_result = await self.fedify_gateway.publish_local_community_content(
            PublishContentRequest(
                actor_username=user.activitypub_username,
                community_actor_url=getattr(local_community, "actor_url"),
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
            community_actor_url=getattr(local_community, "actor_url"),
            discord_channel_id=getattr(thread, "parent_id"),
            discord_message_id=getattr(starter_message, "id"),
        )
        self.database.create_published_activity_object(
            actor_username=user.activitypub_username,
            actor_url=user.actor_url,
            community_actor_url=getattr(local_community, "actor_url"),
            activity_id=publish_result.activity_id,
            object_id=publish_result.object_id,
            kind="post",
            title=title,
            body_markdown=body,
            in_reply_to_object_id=None,
            discord_channel_id=getattr(thread, "parent_id"),
            discord_message_id=getattr(starter_message, "id"),
        )
        self.database.create_local_community_thread(
            local_community_id=getattr(local_community, "id"),
            discord_thread_id=getattr(thread, "id"),
            discord_starter_message_id=getattr(starter_message, "id"),
            ap_activity_id=publish_result.activity_id,
            ap_object_id=publish_result.object_id,
            direction="discord_to_ap",
            origin_kind="discord_local",
        )
        return LocalCommunityRuntimeResult(
            status="published",
            reason="published",
            activity_id=publish_result.activity_id,
            object_id=publish_result.object_id,
        )

    async def handle_discord_message(self, *, message: object) -> LocalCommunityRuntimeResult:
        """Publish one Discord reply inside a local-community thread as a comment."""
        thread = getattr(message, "channel")
        local_community = get_local_community_for_forum(
            self.database, getattr(thread, "parent_id")
        )
        if local_community is None:
            return LocalCommunityRuntimeResult(status="ignored", reason="not_local_community")
        if get_local_community_message_for_discord_message(self.database, getattr(message, "id")) is not None:
            return LocalCommunityRuntimeResult(status="ignored", reason="duplicate_message")

        thread_row = get_local_community_thread_for_discord_thread(self.database, getattr(thread, "id"))
        if thread_row is None:
            return LocalCommunityRuntimeResult(status="ignored", reason="no_thread_context")

        if getattr(thread_row, "discord_starter_message_id") == getattr(message, "id"):
            return LocalCommunityRuntimeResult(status="ignored", reason="starter_message_already_handled")

        user = self.database.get_user_by_discord_user_id(
            str(getattr(getattr(message, "author"), "id"))
        )
        if user is None:
            await getattr(message, "reply")(UNREGISTERED_REPLY)
            return LocalCommunityRuntimeResult(status="ignored", reason="unregistered_user")

        reply_context = resolve_outbound_reply_context(
            database=self.database,
            thread_row=thread_row,
            message=message,
        )
        author_name = self._author_name(getattr(message, "author"))
        body = format_discord_body_for_lemmy(
            author_name,
            getattr(message, "content"),
            self.bridge_prefix,
        )
        # Comment fanout for local communities uses the same follower-aware
        # gateway contract as top-level posts.
        publish_result = await self.fedify_gateway.publish_local_community_content(
            PublishContentRequest(
                actor_username=user.activitypub_username,
                community_actor_url=getattr(local_community, "actor_url"),
                kind="comment",
                title=None,
                body_markdown=body,
                in_reply_to_object_id=reply_context.parent_ap_object_id,
            )
        )

        self.database.create_message_mapping(
            source_platform="discord",
            source_id=str(getattr(message, "id")),
            activity_id=publish_result.activity_id,
            object_id=publish_result.object_id,
            actor_url=user.actor_url,
            community_actor_url=getattr(local_community, "actor_url"),
            discord_channel_id=getattr(thread, "parent_id"),
            discord_message_id=getattr(message, "id"),
        )
        self.database.create_published_activity_object(
            actor_username=user.activitypub_username,
            actor_url=user.actor_url,
            community_actor_url=getattr(local_community, "actor_url"),
            activity_id=publish_result.activity_id,
            object_id=publish_result.object_id,
            kind="comment",
            title=None,
            body_markdown=body,
            in_reply_to_object_id=reply_context.parent_ap_object_id,
            discord_channel_id=getattr(thread, "parent_id"),
            discord_message_id=getattr(message, "id"),
        )
        self.database.create_local_community_message(
            local_community_thread_id=getattr(thread_row, "id"),
            discord_message_id=getattr(message, "id"),
            ap_activity_id=publish_result.activity_id,
            ap_object_id=publish_result.object_id,
            parent_ap_object_id=reply_context.parent_ap_object_id,
            parent_discord_message_id=reply_context.parent_discord_message_id,
            direction="discord_to_ap",
        )
        return LocalCommunityRuntimeResult(
            status="published",
            reason="published",
            activity_id=publish_result.activity_id,
            object_id=publish_result.object_id,
        )

    async def handle_inbound_post(self, event: object, runtime: object) -> HandlerResult:
        """Mirror one remote top-level post into a new Discord forum thread."""
        from ..activitypub_handlers import HandlerResult as _HandlerResult

        local_community = self.database.get_local_community_by_actor_url(
            getattr(event, "community_actor_id")
        )
        if local_community is None:
            return _HandlerResult(status="skipped", detail="unknown local community")
        existing = get_local_community_thread_for_ap_object(self.database, getattr(getattr(event, "object"), "ap_id"))
        if existing is not None:
            return _HandlerResult(status="skipped", detail="post already mapped")
        follower = self.database.get_local_community_follower(
            local_community_id=getattr(local_community, "id"),
            remote_actor_id=getattr(event, "actor_id"),
        )
        if follower is None or getattr(follower, "status") != "accepted":
            return _HandlerResult(status="skipped", detail="remote actor is not an accepted follower")
        if self.bot is None:
            raise RuntimeError("LocalCommunityRuntime requires bot for inbound Discord delivery")

        await self.bot.wait_until_bridge_ready()
        forum_channel = await self.bot.fetch_forum_channel(getattr(local_community, "discord_forum_channel_id"))
        thread_title = getattr(getattr(event, "object"), "title", None) or "Untitled remote post"
        created = await forum_channel.create_thread(
            name=thread_title,
            content=self._format_inbound_post_body(event),
        )
        created_thread, starter_message = self._unpack_created_thread(created)
        self.database.create_local_community_thread(
            local_community_id=getattr(local_community, "id"),
            discord_thread_id=getattr(created_thread, "id"),
            discord_starter_message_id=getattr(starter_message, "id"),
            ap_activity_id=getattr(event, "delivery_id"),
            ap_object_id=getattr(getattr(event, "object"), "ap_id"),
            direction="ap_to_discord",
            origin_kind="remote_follower",
        )
        return _HandlerResult(status="processed", detail="remote post created thread")

    async def handle_inbound_comment(self, event: object, runtime: object) -> HandlerResult:
        """Mirror one remote comment into the mapped Discord thread."""
        from ..activitypub_handlers import HandlerResult as _HandlerResult

        local_community = self.database.get_local_community_by_actor_url(
            getattr(event, "community_actor_id")
        )
        if local_community is None:
            return _HandlerResult(status="skipped", detail="unknown local community")
        if self.database.get_local_community_message_by_ap_object_id(getattr(getattr(event, "object"), "ap_id")) is not None:
            return _HandlerResult(status="skipped", detail="comment already mapped")
        follower = self.database.get_local_community_follower(
            local_community_id=getattr(local_community, "id"),
            remote_actor_id=getattr(event, "actor_id"),
        )
        if follower is None or getattr(follower, "status") != "accepted":
            return _HandlerResult(status="skipped", detail="remote actor is not an accepted follower")

        thread_row = self.database.get_local_community_thread_by_ap_object_id(
            getattr(getattr(event, "object"), "post_ap_id")
        )
        if thread_row is None:
            return _HandlerResult(status="skipped", detail="comment parent post is not mapped")
        if self.bot is None:
            raise RuntimeError("LocalCommunityRuntime requires bot for inbound Discord delivery")

        discord_thread = await self.bot.get_thread_by_id(getattr(thread_row, "discord_thread_id"))
        parent_discord_message_id = resolve_inbound_reply_target(
            database=self.database,
            parent_ap_object_id=getattr(getattr(event, "object"), "parent_ap_id"),
            thread_row=thread_row,
        )
        send_kwargs: dict[str, object] = {}
        if parent_discord_message_id is not None:
            # discord.py validates the concrete reference type at send time, so
            # local-community replies must build a real MessageReference rather
            # than a duck-typed object carrying only message_id.
            send_kwargs["reference"] = self._build_message_reference(
                discord_thread=discord_thread,
                message_id=parent_discord_message_id,
            )
        created_message = await discord_thread.send(
            self._format_inbound_comment_body(event),
            **send_kwargs,
        )
        self.database.create_local_community_message(
            local_community_thread_id=getattr(thread_row, "id"),
            discord_message_id=getattr(created_message, "id"),
            ap_activity_id=getattr(event, "delivery_id"),
            ap_object_id=getattr(getattr(event, "object"), "ap_id"),
            parent_ap_object_id=getattr(getattr(event, "object"), "parent_ap_id"),
            parent_discord_message_id=parent_discord_message_id,
            direction="ap_to_discord",
        )
        return _HandlerResult(status="processed", detail="remote comment created message")

    async def handle_follow_request(
        self,
        *,
        local_community_actor_id: str,
        remote_actor_id: str,
        remote_inbox_url: str,
        follow_activity_id: str,
    ) -> HandlerResult:
        """Persist and accept one remote follow request for a local community."""
        from ..activitypub_handlers import HandlerResult as _HandlerResult

        local_community = self.database.get_local_community_by_actor_url(local_community_actor_id)
        if local_community is None:
            return _HandlerResult(status="skipped", detail="unknown local community")
        existing = self.database.get_local_community_follower(
            local_community_id=getattr(local_community, "id"),
            remote_actor_id=remote_actor_id,
        )
        if existing is not None:
            return _HandlerResult(status="processed", detail="local community follower already accepted")

        self.database.create_local_community_follower(
            local_community_id=getattr(local_community, "id"),
            remote_actor_id=remote_actor_id,
            remote_inbox_url=remote_inbox_url,
            follow_activity_id=follow_activity_id,
            status="accepted",
        )
        await self.fedify_gateway.accept_local_community_follow(
            community_slug=getattr(local_community, "slug"),
            community_actor_url=getattr(local_community, "actor_url"),
            remote_actor_id=remote_actor_id,
            remote_inbox_url=remote_inbox_url,
            follow_activity_id=follow_activity_id,
        )
        return _HandlerResult(status="processed", detail="local community follower accepted")

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
    def _unpack_created_thread(created: object) -> tuple[object, object]:
        """Normalize both supported Discord `create_thread()` return shapes."""
        if isinstance(created, tuple):
            return created[0], created[1]
        return getattr(created, "thread"), getattr(created, "message")

    @staticmethod
    def _format_inbound_post_body(event: object) -> str:
        """Render one inbound remote post for Discord starter-message creation."""
        object_payload = getattr(event, "object")
        author_name = getattr(object_payload, "author_name", "remote")
        body_markdown = getattr(object_payload, "body_markdown", None) or ""
        return f"**{author_name}**\n\n{body_markdown}".strip()

    @staticmethod
    def _format_inbound_comment_body(event: object) -> str:
        """Render one inbound remote comment for Discord message creation."""
        object_payload = getattr(event, "object")
        author_name = getattr(object_payload, "author_name", "remote")
        body_markdown = getattr(object_payload, "body_markdown", None) or ""
        return f"**{author_name}**\n\n{body_markdown}".strip()

    @staticmethod
    def _build_message_reference(
        *,
        discord_thread: object,
        message_id: int,
    ) -> discord.MessageReference:
        """Build one discord.py-compatible reference for an inbound mirrored reply.

        The runtime only needs the parent message id and the thread channel id.
        `fail_if_not_exists=False` keeps reply fanout resilient when Discord no
        longer has the exact cached parent message object locally.
        """
        guild_id = getattr(discord_thread, "guild_id", None)
        guild = getattr(discord_thread, "guild", None)
        if guild_id is None and guild is not None:
            guild_id = getattr(guild, "id", None)
        return discord.MessageReference(
            message_id=message_id,
            channel_id=getattr(discord_thread, "id"),
            guild_id=guild_id,
            fail_if_not_exists=False,
        )
