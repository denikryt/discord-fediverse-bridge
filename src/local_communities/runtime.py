"""Runtime orchestration for the Discord-backed local-community mode.

LocalCommunityRuntime owns the local-community domain boundary: routing from
Discord forum channels into a local federated community actor, and routing from
remote followers back into Discord. Shared content mechanics are delegated to
the common content-sync layer so this runtime only owns local-community policy
and canonical local-community mapping rows.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..content_sync.edit_delete import (
    edit_discord_message,
    mark_discord_message_deleted,
    resolve_published_object_for_discord_message,
)
from ..content_sync.inbound_references import build_message_reference
from ..db import Database
from ..discord_publish_service import ContentPublishService
from ..fedify_gateway_client import DeleteContentRequest, FedifyGatewayClient, UpdateContentRequest
from ..formatting import format_lemmy_comment_for_discord, format_lemmy_post_for_discord, normalize_text
from .federation_fanout import LocalCommunityFederationFanout
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
        content_publish_service: ContentPublishService,
        bridge_prefix: str,
        bot: object | None = None,
    ) -> None:
        """Initialise the local-community runtime with shared long-lived services."""
        self.database = database
        self.fedify_gateway = fedify_gateway
        self.content_publish_service = content_publish_service
        self.bridge_prefix = bridge_prefix
        self.bot = bot
        self.federation_fanout = LocalCommunityFederationFanout(
            database=database,
            fedify_gateway=fedify_gateway,
        )

    async def handle_discord_thread_create(
        self,
        *,
        thread: object,
        starter_message: object,
    ) -> LocalCommunityRuntimeResult:
        """Publish one Discord thread starter into a local community as a post."""
        local_community = get_local_community_for_forum(self.database, getattr(thread, "parent_id"))
        if local_community is None:
            return LocalCommunityRuntimeResult(status="ignored", reason="not_local_community")
        if get_local_community_thread_for_discord_thread(self.database, getattr(thread, "id")) is not None:
            return LocalCommunityRuntimeResult(status="ignored", reason="duplicate_thread")

        # Shared create-path logic owns registration checks, formatting, gateway
        # publish, and generic publish persistence.
        publish_result = await self.content_publish_service.publish_local_thread_starter(
            thread=thread,
            starter_message=starter_message,
            community_actor_url=getattr(local_community, "actor_url"),
        )
        if publish_result.status != "published":
            return LocalCommunityRuntimeResult(
                status=publish_result.status,
                reason=publish_result.reason,
            )

        # The local-community runtime still owns the canonical post/thread row
        # that binds the Discord thread to the AP post object.
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
        local_community = get_local_community_for_forum(self.database, getattr(thread, "parent_id"))
        if local_community is None:
            return LocalCommunityRuntimeResult(status="ignored", reason="not_local_community")
        if get_local_community_message_for_discord_message(self.database, getattr(message, "id")) is not None:
            return LocalCommunityRuntimeResult(status="ignored", reason="duplicate_message")

        thread_row = get_local_community_thread_for_discord_thread(self.database, getattr(thread, "id"))
        if thread_row is None:
            return LocalCommunityRuntimeResult(status="ignored", reason="no_thread_context")
        if getattr(thread_row, "discord_starter_message_id") == getattr(message, "id"):
            return LocalCommunityRuntimeResult(status="ignored", reason="starter_message_already_handled")

        reply_context = resolve_outbound_reply_context(
            database=self.database,
            thread_row=thread_row,
            message=message,
        )
        publish_result = await self.content_publish_service.publish_local_thread_message(
            message=message,
            community_actor_url=getattr(local_community, "actor_url"),
            parent_object_id=reply_context.parent_ap_object_id,
        )
        if publish_result.status != "published":
            return LocalCommunityRuntimeResult(
                status=publish_result.status,
                reason=publish_result.reason,
            )

        # The local-community runtime still owns the canonical comment/message
        # row that binds the Discord reply to the AP comment object.
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

        local_community = self.database.get_local_community_by_actor_url(getattr(event, "community_actor_id"))
        if local_community is None:
            return _HandlerResult(status="skipped", detail="unknown local community")
        existing = get_local_community_thread_for_ap_object(self.database, getattr(getattr(event, "object"), "ap_id"))
        if existing is not None:
            await self.federation_fanout.relay_create(
                event=event,
                local_community=local_community,
                object_kind="post",
            )
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
        await self.federation_fanout.relay_create(
            event=event,
            local_community=local_community,
            object_kind="post",
        )
        return _HandlerResult(status="processed", detail="remote post created thread")

    async def handle_inbound_comment(self, event: object, runtime: object) -> HandlerResult:
        """Mirror one remote comment into the mapped Discord thread."""
        from ..activitypub_handlers import HandlerResult as _HandlerResult

        local_community = self.database.get_local_community_by_actor_url(getattr(event, "community_actor_id"))
        if local_community is None:
            return _HandlerResult(status="skipped", detail="unknown local community")
        if self.database.get_local_community_message_by_ap_object_id(getattr(getattr(event, "object"), "ap_id")) is not None:
            await self.federation_fanout.relay_create(
                event=event,
                local_community=local_community,
                object_kind="comment",
            )
            return _HandlerResult(status="skipped", detail="comment already mapped")
        follower = self.database.get_local_community_follower(
            local_community_id=getattr(local_community, "id"),
            remote_actor_id=getattr(event, "actor_id"),
        )
        if follower is None or getattr(follower, "status") != "accepted":
            return _HandlerResult(status="skipped", detail="remote actor is not an accepted follower")

        thread_row = self.database.get_local_community_thread_by_ap_object_id(getattr(getattr(event, "object"), "post_ap_id"))
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
            # The shared helper keeps the reference contract identical to the
            # remote-subscription mode and avoids duck-typed reply objects.
            send_kwargs["reference"] = build_message_reference(
                discord_thread=discord_thread,
                message_id=parent_discord_message_id,
            )
        created_message = await discord_thread.send(self._format_inbound_comment_body(event), **send_kwargs)
        self.database.create_local_community_message(
            local_community_thread_id=getattr(thread_row, "id"),
            discord_message_id=getattr(created_message, "id"),
            ap_activity_id=getattr(event, "delivery_id"),
            ap_object_id=getattr(getattr(event, "object"), "ap_id"),
            parent_ap_object_id=getattr(getattr(event, "object"), "parent_ap_id"),
            parent_discord_message_id=parent_discord_message_id,
            direction="ap_to_discord",
        )
        await self.federation_fanout.relay_create(
            event=event,
            local_community=local_community,
            object_kind="comment",
        )
        return _HandlerResult(status="processed", detail="remote comment created message")

    async def handle_discord_message_edit(
        self,
        *,
        message_id: int,
        new_content: str,
        runtime: object,
        author_display_name: str = "",
    ) -> None:
        """Propagate one Discord-authored local-community edit to ActivityPub.

        Local-community mode has no sibling Discord mirrors, so this path only
        sends the AP Update when the edited message belongs to a local
        Discord-originated post or comment.
        """
        del author_display_name

        published = resolve_published_object_for_discord_message(self.database, discord_message_id=message_id)
        if published is None:
            return
        if self.database.get_local_community_thread_by_starter_message_id(message_id) is None and (
            self.database.get_local_community_message_by_discord_message_id(message_id) is None
        ):
            return

        try:
            await runtime.fedify_gateway.update_content(
                UpdateContentRequest(
                    actor_username=published.actor_username,
                    community_actor_url=published.community_actor_url,
                    ap_object_id=published.object_id,
                    kind=published.kind,
                    title=published.title,
                    body_markdown=new_content,
                    in_reply_to_object_id=published.in_reply_to_object_id,
                )
            )
        except Exception:
            logger.exception("Failed to send local-community edit for Discord message %s", message_id)

    async def handle_discord_message_delete(
        self,
        *,
        message_id: int,
        runtime: object,
    ) -> None:
        """Propagate one Discord-authored local-community delete to ActivityPub."""
        published = resolve_published_object_for_discord_message(self.database, discord_message_id=message_id)
        if published is None:
            return
        if self.database.get_local_community_thread_by_starter_message_id(message_id) is None and (
            self.database.get_local_community_message_by_discord_message_id(message_id) is None
        ):
            return

        try:
            await runtime.fedify_gateway.delete_content(
                DeleteContentRequest(
                    actor_username=published.actor_username,
                    community_actor_url=published.community_actor_url,
                    ap_object_id=published.object_id,
                )
            )
        except Exception:
            logger.exception("Failed to send local-community delete for Discord message %s", message_id)

    async def handle_inbound_post_update(self, event: object, runtime: object) -> HandlerResult:
        """Edit the starter message for one inbound remote post update."""
        from ..activitypub_handlers import HandlerResult as _HandlerResult

        thread_row = self.database.get_local_community_thread_by_ap_object_id(getattr(getattr(event, "object"), "ap_id"))
        if thread_row is None:
            return _HandlerResult(status="skipped", detail="post not yet mapped")

        local_community = self.database.get_local_community_by_actor_url(getattr(event, "community_actor_id"))
        bot = self.bot or runtime.bot
        await edit_discord_message(
            bot=bot,
            discord_thread_id=getattr(thread_row, "discord_thread_id"),
            discord_message_id=getattr(thread_row, "discord_starter_message_id"),
            new_content=self._format_inbound_post_body(event),
            preserve_header=False,
        )
        if local_community is not None:
            await self.federation_fanout.relay_update_or_delete(
                event=event,
                local_community=local_community,
                object_kind="post",
                operation="update",
            )
        return _HandlerResult(status="processed", detail="post updated")

    async def handle_inbound_post_delete(self, event: object, runtime: object) -> HandlerResult:
        """Mark the starter message deleted for one inbound remote post delete."""
        from ..activitypub_handlers import HandlerResult as _HandlerResult

        thread_row = self.database.get_local_community_thread_by_ap_object_id(getattr(getattr(event, "object"), "ap_id"))
        if thread_row is None:
            return _HandlerResult(status="skipped", detail="post not yet mapped")

        local_community = self.database.get_local_community_by_actor_url(getattr(event, "community_actor_id"))
        bot = self.bot or runtime.bot
        await mark_discord_message_deleted(
            bot=bot,
            discord_thread_id=getattr(thread_row, "discord_thread_id"),
            discord_message_id=getattr(thread_row, "discord_starter_message_id"),
        )
        if local_community is not None:
            await self.federation_fanout.relay_update_or_delete(
                event=event,
                local_community=local_community,
                object_kind="post",
                operation="delete",
            )
        return _HandlerResult(status="processed", detail="post deleted")

    async def handle_inbound_comment_update(self, event: object, runtime: object) -> HandlerResult:
        """Edit the mirrored Discord copy for one inbound remote comment update."""
        from ..activitypub_handlers import HandlerResult as _HandlerResult

        message_row = self.database.get_local_community_message_by_ap_object_id(getattr(getattr(event, "object"), "ap_id"))
        if message_row is None:
            return _HandlerResult(status="skipped", detail="comment not yet mapped")
        thread_row = self.database.get_local_community_thread_by_id(getattr(message_row, "local_community_thread_id"))
        if thread_row is None:
            return _HandlerResult(status="skipped", detail="comment thread not mapped")

        local_community = self.database.get_local_community_by_actor_url(getattr(event, "community_actor_id"))
        bot = self.bot or runtime.bot
        await edit_discord_message(
            bot=bot,
            discord_thread_id=getattr(thread_row, "discord_thread_id"),
            discord_message_id=getattr(message_row, "discord_message_id"),
            new_content=self._format_inbound_comment_body(event),
            preserve_header=False,
        )
        if local_community is not None:
            await self.federation_fanout.relay_update_or_delete(
                event=event,
                local_community=local_community,
                object_kind="comment",
                operation="update",
            )
        return _HandlerResult(status="processed", detail="comment updated")

    async def handle_inbound_comment_delete(self, event: object, runtime: object) -> HandlerResult:
        """Mark the mirrored Discord copy deleted for one inbound remote comment delete."""
        from ..activitypub_handlers import HandlerResult as _HandlerResult

        message_row = self.database.get_local_community_message_by_ap_object_id(getattr(getattr(event, "object"), "ap_id"))
        if message_row is None:
            return _HandlerResult(status="skipped", detail="comment not yet mapped")
        thread_row = self.database.get_local_community_thread_by_id(getattr(message_row, "local_community_thread_id"))
        if thread_row is None:
            return _HandlerResult(status="skipped", detail="comment thread not mapped")

        local_community = self.database.get_local_community_by_actor_url(getattr(event, "community_actor_id"))
        bot = self.bot or runtime.bot
        await mark_discord_message_deleted(
            bot=bot,
            discord_thread_id=getattr(thread_row, "discord_thread_id"),
            discord_message_id=getattr(message_row, "discord_message_id"),
        )
        if local_community is not None:
            await self.federation_fanout.relay_update_or_delete(
                event=event,
                local_community=local_community,
                object_kind="comment",
                operation="delete",
            )
        return _HandlerResult(status="processed", detail="comment deleted")

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
            # Accept(Follow) delivery is intentionally idempotent. A remote
            # server such as Mastodon can remain in a "requested" state if the
            # bridge persisted the follower but the original Accept was lost, so
            # repeated Follow deliveries must refresh the stored request details
            # and re-send the Accept instead of returning early.
            self.database.update_local_community_follower_acceptance(
                local_community_id=getattr(local_community, "id"),
                remote_actor_id=remote_actor_id,
                remote_inbox_url=remote_inbox_url,
                follow_activity_id=follow_activity_id,
                status="accepted",
            )
            detail = "local community follower accepted again"
        else:
            self.database.create_local_community_follower(
                local_community_id=getattr(local_community, "id"),
                remote_actor_id=remote_actor_id,
                remote_inbox_url=remote_inbox_url,
                follow_activity_id=follow_activity_id,
                status="accepted",
            )
            detail = "local community follower accepted"

        logger.info(
            "Accepting local-community Follow community=%s remote_actor=%s inbox=%s follow_activity=%s",
            getattr(local_community, "slug"),
            remote_actor_id,
            remote_inbox_url,
            follow_activity_id,
        )
        await self.fedify_gateway.accept_local_community_follow(
            community_slug=getattr(local_community, "slug"),
            community_actor_url=getattr(local_community, "actor_url"),
            remote_actor_id=remote_actor_id,
            remote_inbox_url=remote_inbox_url,
            follow_activity_id=follow_activity_id,
        )
        return _HandlerResult(status="processed", detail=detail)

    @staticmethod
    def _unpack_created_thread(created: object) -> tuple[object, object]:
        """Normalize both supported Discord `create_thread()` return shapes."""
        if isinstance(created, tuple):
            return created[0], created[1]
        return getattr(created, "thread"), getattr(created, "message")

    @staticmethod
    def _format_inbound_post_body(event: object) -> str:
        """Render one inbound remote post using the shared Discord-facing format."""
        object_payload = getattr(event, "object")
        return format_lemmy_post_for_discord(
            getattr(object_payload, "author_name", "remote"),
            getattr(object_payload, "title", None) or "Untitled remote post",
            normalize_text(getattr(object_payload, "body_markdown", None)),
            getattr(object_payload, "url", ""),
        )

    @staticmethod
    def _format_inbound_comment_body(event: object) -> str:
        """Render one inbound remote comment using the shared Discord-facing format."""
        object_payload = getattr(event, "object")
        return format_lemmy_comment_for_discord(
            getattr(object_payload, "author_name", "remote"),
            normalize_text(getattr(object_payload, "body_markdown", None)),
            getattr(object_payload, "url", ""),
            actor_id=getattr(event, "actor_id", ""),
        )
