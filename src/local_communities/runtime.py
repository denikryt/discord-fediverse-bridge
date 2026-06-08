"""Runtime orchestration for the Discord-backed local-community mode.

LocalCommunityRuntime owns the local-community domain boundary: routing from
Discord forum channels into a local federated community actor, and routing from
remote subscribers back into Discord. Shared content mechanics are delegated to
the common content-sync layer so this runtime only owns local-community policy
and canonical local-community mapping rows.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..inbound_activity_outcomes import InboundActivityOutcome
from ..bridge_policy import BridgePolicyService
from ..content_sync.inbound_references import build_message_reference
from ..db import Database
from ..content_publish_service import ContentPublishService
from ..fedify_gateway_client import DeleteContentRequest, FedifyGatewayClient, UpdateContentRequest
from ..formatting import format_lemmy_comment_for_discord, format_lemmy_post_for_discord, normalize_text
from ..local_community_lifecycle import evaluate_local_community_lifecycle
from .discord_fanout import LocalCommunityDiscordFanout
from .federation_fanout import LocalCommunityFederationFanout
from .delivery_mapping import (
    get_local_community_for_forum,
    get_local_community_message_for_discord_message,
    get_local_community_thread_for_ap_object,
    get_local_community_thread_for_discord_thread,
    get_local_community_thread_surface_for_discord_thread,
)
from .participant_routing import resolve_local_community_source_for_forum
from .reply_mapping import resolve_inbound_reply_target, resolve_outbound_reply_context, resolve_outbound_reply_context_for_surface

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ..activitypub_handlers import HandlerResult


def _discord_author_display_name(message: object) -> str | None:
    """Return the best available Discord-side author label for local fanout."""
    author = getattr(message, "author", None)
    return getattr(author, "display_name", None) or getattr(author, "name", None)


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
        bridge_policy_service: BridgePolicyService,
        bot: object | None = None,
    ) -> None:
        """Initialise the local-community runtime with shared long-lived services."""
        self.database = database
        self.fedify_gateway = fedify_gateway
        self.content_publish_service = content_publish_service
        self.bridge_prefix = bridge_prefix
        self.bridge_policy_service = bridge_policy_service
        self.bot = bot
        self.federation_fanout = LocalCommunityFederationFanout(
            database=database,
            fedify_gateway=fedify_gateway,
            policy_service=bridge_policy_service,
        )

    def _disabled_result_if_needed(self, local_community: object) -> LocalCommunityRuntimeResult | None:
        """Return an ignored result when a community is operationally disabled."""
        decision = evaluate_local_community_lifecycle(local_community)
        if decision.allowed:
            return None
        return LocalCommunityRuntimeResult(status="ignored", reason=decision.reason)

    def _is_community_id_disabled(self, local_community_id: int | None) -> bool:
        """Return whether a resolved canonical community id is disabled."""
        if local_community_id is None:
            return False
        community = self.database.local_communities.get_local_community_by_id(local_community_id)
        if community is None:
            return False
        return not evaluate_local_community_lifecycle(community).allowed

    async def handle_discord_thread_create(
        self,
        *,
        thread: object,
        starter_message: object,
    ) -> LocalCommunityRuntimeResult:
        """Publish one Discord thread starter into a local community as a post."""
        source = resolve_local_community_source_for_forum(
            self.database, getattr(thread, "parent_id", None)
        )
        if source is None:
            return LocalCommunityRuntimeResult(status="ignored", reason="not_local_community")
        disabled = self._disabled_result_if_needed(source.local_community)
        if disabled is not None:
            return disabled
        if source.source_kind == "local_subscriber":
            return await self._handle_local_subscriber_thread_create(
                thread=thread,
                starter_message=starter_message,
                local_community=source.local_community,
                local_subscriber=source.local_subscriber,
            )
        return await self._handle_host_thread_create(
            thread=thread,
            starter_message=starter_message,
            local_community=source.local_community,
        )

    async def _handle_host_thread_create(
        self,
        *,
        thread: object,
        starter_message: object,
        local_community: object,
    ) -> LocalCommunityRuntimeResult:
        """Handle the existing host-forum thread create path unchanged."""
        existing_thread = get_local_community_thread_for_discord_thread(self.database, getattr(thread, "id"))
        if existing_thread is not None:
            await self._fanout_thread_to_local_subscribers(
                local_community=local_community,
                thread_row=existing_thread,
                title=getattr(thread, "name", "Untitled thread"),
                content=getattr(starter_message, "content", ""),
                author_display_name=self.content_publish_service.canonical_author_name_for_discord(getattr(starter_message, "author")),
                source_forum_channel_id=getattr(thread, "parent_id"),
            )
            return LocalCommunityRuntimeResult(status="ignored", reason="duplicate_thread")

        publish_result = await self.content_publish_service.publish_local_thread_starter(
            thread=thread,
            starter_message=starter_message,
            community_actor_url=getattr(local_community, "actor_url"),
        )
        if publish_result.status != "published":
            return LocalCommunityRuntimeResult(status=publish_result.status, reason=publish_result.reason)

        thread_row = self.database.local_community_content.create_local_community_thread(
            local_community_id=getattr(local_community, "id"),
            discord_thread_id=getattr(thread, "id"),
            discord_starter_message_id=getattr(starter_message, "id"),
            ap_activity_id=publish_result.activity_id,
            ap_object_id=publish_result.object_id,
            direction="discord_to_ap",
            origin_kind="discord_local",
        )
        await self._fanout_thread_to_local_subscribers(
            local_community=local_community,
            thread_row=thread_row,
            title=getattr(thread, "name", "Untitled thread"),
            content=getattr(starter_message, "content", ""),
            author_display_name=self.content_publish_service.canonical_author_name_for_discord(getattr(starter_message, "author")),
            source_forum_channel_id=getattr(thread, "parent_id"),
        )
        return LocalCommunityRuntimeResult(
            status="published",
            reason="published",
            activity_id=publish_result.activity_id,
            object_id=publish_result.object_id,
        )

    async def _handle_local_subscriber_thread_create(
        self,
        *,
        thread: object,
        starter_message: object,
        local_community: object,
        local_subscriber: object | None,
    ) -> LocalCommunityRuntimeResult:
        """Handle a Stage 4 local-subscriber forum thread as canonical content."""
        if local_subscriber is None:
            return LocalCommunityRuntimeResult(status="ignored", reason="not_local_subscriber")
        source_surface = get_local_community_thread_surface_for_discord_thread(
            self.database, getattr(thread, "id")
        )
        if source_surface is not None:
            thread_row = self.database.local_community_surfaces.get_local_community_thread_for_surface(source_surface.id)
            if thread_row is not None:
                await self._fanout_thread_to_local_participants(
                    local_community=local_community,
                    thread_row=thread_row,
                    title=getattr(thread, "name", "Untitled thread"),
                    content=getattr(starter_message, "content", ""),
                    author_display_name=self.content_publish_service.canonical_author_name_for_discord(getattr(starter_message, "author")),
                    source_forum_channel_id=getattr(thread, "parent_id"),
                    include_host=True,
                )
            return LocalCommunityRuntimeResult(status="ignored", reason="duplicate_thread")

        publish_result = await self.content_publish_service.publish_local_thread_starter(
            thread=thread,
            starter_message=starter_message,
            community_actor_url=getattr(local_community, "actor_url"),
        )
        if publish_result.status != "published":
            return LocalCommunityRuntimeResult(status=publish_result.status, reason=publish_result.reason)

        thread_row = self.database.local_community_content.create_local_community_thread_canonical(
            local_community_id=getattr(local_community, "id"),
            ap_activity_id=publish_result.activity_id,
            ap_object_id=publish_result.object_id,
            direction="discord_to_ap",
            origin_kind="discord_local_subscriber",
        )
        self.database.local_community_surfaces.create_local_community_thread_surface(
            local_community_thread_id=getattr(thread_row, "id"),
            discord_forum_channel_id=getattr(thread, "parent_id"),
            discord_thread_id=getattr(thread, "id"),
            discord_starter_message_id=getattr(starter_message, "id"),
            role="local_subscriber",
            local_subscriber_id=getattr(local_subscriber, "id"),
        )
        await self._fanout_thread_to_local_participants(
            local_community=local_community,
            thread_row=thread_row,
            title=getattr(thread, "name", "Untitled thread"),
            content=getattr(starter_message, "content", ""),
            author_display_name=self.content_publish_service.canonical_author_name_for_discord(getattr(starter_message, "author")),
            source_forum_channel_id=getattr(thread, "parent_id"),
            include_host=True,
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
        source = resolve_local_community_source_for_forum(
            self.database, getattr(thread, "parent_id", None)
        )
        if source is None:
            return LocalCommunityRuntimeResult(status="ignored", reason="not_local_community")
        disabled = self._disabled_result_if_needed(source.local_community)
        if disabled is not None:
            return disabled
        if source.source_kind == "local_subscriber":
            return await self._handle_local_subscriber_message(
                message=message,
                local_community=source.local_community,
                local_subscriber=source.local_subscriber,
            )
        return await self._handle_host_message(message=message, local_community=source.local_community)

    async def _handle_host_message(self, *, message: object, local_community: object) -> LocalCommunityRuntimeResult:
        """Handle the existing host-forum Discord reply path unchanged."""
        thread = getattr(message, "channel")
        existing_message = get_local_community_message_for_discord_message(self.database, getattr(message, "id"))
        if existing_message is not None:
            thread_row = self.database.local_community_content.get_local_community_thread_by_id(getattr(existing_message, "local_community_thread_id"))
            if thread_row is not None:
                await self._fanout_message_to_local_subscribers(
                    local_community=local_community,
                    thread_row=thread_row,
                    message_row=existing_message,
                    content=getattr(message, "content", ""),
                    author_display_name=self.content_publish_service.canonical_author_name_for_discord(getattr(message, "author")),
                    source_forum_channel_id=getattr(thread, "parent_id"),
                )
            return LocalCommunityRuntimeResult(status="ignored", reason="duplicate_message")

        thread_row = get_local_community_thread_for_discord_thread(self.database, getattr(thread, "id"))
        if thread_row is None:
            return LocalCommunityRuntimeResult(status="ignored", reason="no_thread_context")
        thread_surface = get_local_community_thread_surface_for_discord_thread(self.database, getattr(thread, "id"))
        if thread_surface is None:
            return LocalCommunityRuntimeResult(status="ignored", reason="no_thread_surface")
        if getattr(thread_surface, "discord_starter_message_id") == getattr(message, "id"):
            return LocalCommunityRuntimeResult(status="ignored", reason="starter_message_already_handled")

        reply_context = resolve_outbound_reply_context(database=self.database, thread_row=thread_row, message=message)
        publish_result = await self.content_publish_service.publish_local_thread_message(
            message=message,
            community_actor_url=getattr(local_community, "actor_url"),
            parent_object_id=reply_context.parent_ap_object_id,
        )
        if publish_result.status != "published":
            return LocalCommunityRuntimeResult(status=publish_result.status, reason=publish_result.reason)

        message_row = self.database.local_community_content.create_local_community_message(
            local_community_thread_id=getattr(thread_row, "id"),
            discord_message_id=getattr(message, "id"),
            ap_activity_id=publish_result.activity_id,
            ap_object_id=publish_result.object_id,
            parent_ap_object_id=reply_context.parent_ap_object_id,
            parent_discord_message_id=reply_context.parent_discord_message_id,
            direction="discord_to_ap",
        )
        await self._fanout_message_to_local_subscribers(
            local_community=local_community,
            thread_row=thread_row,
            message_row=message_row,
            content=getattr(message, "content", ""),
            author_display_name=self.content_publish_service.canonical_author_name_for_discord(getattr(message, "author")),
            source_forum_channel_id=getattr(thread, "parent_id"),
        )
        return LocalCommunityRuntimeResult(
            status="published",
            reason="published",
            activity_id=publish_result.activity_id,
            object_id=publish_result.object_id,
        )

    async def _handle_local_subscriber_message(
        self,
        *,
        message: object,
        local_community: object,
        local_subscriber: object | None,
    ) -> LocalCommunityRuntimeResult:
        """Handle a Stage 4 local-subscriber Discord reply as canonical content."""
        if local_subscriber is None:
            return LocalCommunityRuntimeResult(status="ignored", reason="not_local_subscriber")
        thread = getattr(message, "channel")
        existing_message = get_local_community_message_for_discord_message(self.database, getattr(message, "id"))
        if existing_message is not None:
            thread_row = self.database.local_community_content.get_local_community_thread_by_id(getattr(existing_message, "local_community_thread_id"))
            if thread_row is not None:
                await self._fanout_message_to_local_participants(
                    local_community=local_community,
                    thread_row=thread_row,
                    message_row=existing_message,
                    content=getattr(message, "content", ""),
                    author_display_name=self.content_publish_service.canonical_author_name_for_discord(getattr(message, "author")),
                    source_forum_channel_id=getattr(thread, "parent_id"),
                    include_host=True,
                )
            return LocalCommunityRuntimeResult(status="ignored", reason="duplicate_message")

        thread_surface = get_local_community_thread_surface_for_discord_thread(self.database, getattr(thread, "id"))
        if thread_surface is None or getattr(thread_surface, "role") != "local_subscriber":
            return LocalCommunityRuntimeResult(status="ignored", reason="no_thread_surface")
        if getattr(thread_surface, "discord_starter_message_id") == getattr(message, "id"):
            return LocalCommunityRuntimeResult(status="ignored", reason="starter_message_already_handled")
        thread_row = self.database.local_community_surfaces.get_local_community_thread_for_surface(getattr(thread_surface, "id"))
        if thread_row is None:
            return LocalCommunityRuntimeResult(status="ignored", reason="no_thread_context")

        reply_context = resolve_outbound_reply_context_for_surface(
            database=self.database,
            thread_row=thread_row,
            source_thread_surface=thread_surface,
            message=message,
        )
        publish_result = await self.content_publish_service.publish_local_thread_message(
            message=message,
            community_actor_url=getattr(local_community, "actor_url"),
            parent_object_id=reply_context.parent_ap_object_id,
        )
        if publish_result.status != "published":
            return LocalCommunityRuntimeResult(status=publish_result.status, reason=publish_result.reason)

        message_row = self.database.local_community_content.create_local_community_message_canonical(
            local_community_thread_id=getattr(thread_row, "id"),
            ap_activity_id=publish_result.activity_id,
            ap_object_id=publish_result.object_id,
            parent_ap_object_id=reply_context.parent_ap_object_id,
            direction="discord_to_ap",
        )
        self.database.local_community_surfaces.create_local_community_message_surface(
            local_community_message_id=getattr(message_row, "id"),
            local_community_thread_surface_id=getattr(thread_surface, "id"),
            discord_forum_channel_id=getattr(thread, "parent_id"),
            discord_message_id=getattr(message, "id"),
            parent_discord_message_id=reply_context.parent_discord_message_id,
            role="local_subscriber",
            local_subscriber_id=getattr(local_subscriber, "id"),
        )
        await self._fanout_message_to_local_participants(
            local_community=local_community,
            thread_row=thread_row,
            message_row=message_row,
            content=getattr(message, "content", ""),
            author_display_name=self.content_publish_service.canonical_author_name_for_discord(getattr(message, "author")),
            source_forum_channel_id=getattr(thread, "parent_id"),
            include_host=True,
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

        local_community = self.database.local_communities.get_local_community_by_actor_url(getattr(event, "community_actor_id"))
        if local_community is None:
            return _HandlerResult(status="skipped", outcome=InboundActivityOutcome.IGNORED_UNKNOWN_LOCAL_COMMUNITY, detail="unknown local community")
        existing = get_local_community_thread_for_ap_object(self.database, getattr(getattr(event, "object"), "ap_id"))
        if existing is not None:
            await self._fanout_thread_to_local_subscribers(
                local_community=local_community,
                thread_row=existing,
                title=getattr(getattr(event, "object"), "title", None) or "Untitled remote post",
                content=self._format_inbound_post_body(event),
                author_display_name=None,
                source_forum_channel_id=None,
            )
            await self.federation_fanout.relay_create(
                event=event,
                local_community=local_community,
                object_kind="post",
            )
            return _HandlerResult(status="skipped", outcome=InboundActivityOutcome.IGNORED_ALREADY_APPLIED, detail="post already mapped")
        remote_subscriber = self.database.remote_subscribers.get_remote_subscriber(
            local_community_id=getattr(local_community, "id"),
            remote_actor_id=getattr(event, "actor_id"),
        )
        if remote_subscriber is None or getattr(remote_subscriber, "status") != "accepted":
            return _HandlerResult(status="skipped", outcome=InboundActivityOutcome.IGNORED_ACTOR_NOT_SUBSCRIBER, detail="remote actor is not an accepted remote subscriber")
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
        thread_row = self.database.local_community_content.create_local_community_thread(
            local_community_id=getattr(local_community, "id"),
            discord_thread_id=getattr(created_thread, "id"),
            discord_starter_message_id=getattr(starter_message, "id"),
            ap_activity_id=getattr(event, "delivery_id"),
            ap_object_id=getattr(getattr(event, "object"), "ap_id"),
            direction="ap_to_discord",
            origin_kind="remote_follower",
        )
        await self._fanout_thread_to_local_subscribers(
            local_community=local_community,
            thread_row=thread_row,
            title=thread_title,
            content=self._format_inbound_post_body(event),
            author_display_name=None,
            source_forum_channel_id=None,
        )
        await self.federation_fanout.relay_create(
            event=event,
            local_community=local_community,
            object_kind="post",
        )
        return _HandlerResult(status="processed", outcome=InboundActivityOutcome.APPLIED, detail="remote post created thread")

    async def handle_inbound_comment(self, event: object, runtime: object) -> HandlerResult:
        """Mirror one remote comment into the mapped Discord thread."""
        from ..activitypub_handlers import HandlerResult as _HandlerResult

        local_community = self.database.local_communities.get_local_community_by_actor_url(getattr(event, "community_actor_id"))
        if local_community is None:
            return _HandlerResult(status="skipped", outcome=InboundActivityOutcome.IGNORED_UNKNOWN_LOCAL_COMMUNITY, detail="unknown local community")
        existing_message = self.database.local_community_content.get_local_community_message_by_ap_object_id(getattr(getattr(event, "object"), "ap_id"))
        if existing_message is not None:
            existing_thread = self.database.local_community_content.get_local_community_thread_by_id(
                getattr(existing_message, "local_community_thread_id")
            )
            if existing_thread is not None:
                await self._fanout_message_to_local_subscribers(
                    local_community=local_community,
                    thread_row=existing_thread,
                    message_row=existing_message,
                    content=self._format_inbound_comment_body(event),
                    author_display_name=None,
                    source_forum_channel_id=None,
                )
            await self.federation_fanout.relay_create(
                event=event,
                local_community=local_community,
                object_kind="comment",
            )
            return _HandlerResult(status="skipped", outcome=InboundActivityOutcome.IGNORED_ALREADY_APPLIED, detail="comment already mapped")
        remote_subscriber = self.database.remote_subscribers.get_remote_subscriber(
            local_community_id=getattr(local_community, "id"),
            remote_actor_id=getattr(event, "actor_id"),
        )
        if remote_subscriber is None or getattr(remote_subscriber, "status") != "accepted":
            return _HandlerResult(status="skipped", outcome=InboundActivityOutcome.IGNORED_ACTOR_NOT_SUBSCRIBER, detail="remote actor is not an accepted remote subscriber")

        thread_row = self.database.local_community_content.get_local_community_thread_by_ap_object_id(getattr(getattr(event, "object"), "post_ap_id"))
        if thread_row is None:
            return _HandlerResult(status="skipped", outcome=InboundActivityOutcome.IGNORED_UNMAPPED_CONTEXT, detail="comment parent post is not mapped")
        if self.bot is None:
            raise RuntimeError("LocalCommunityRuntime requires bot for inbound Discord delivery")

        host_thread_surface = self.database.local_community_surfaces.get_host_local_community_thread_surface(
            getattr(thread_row, "id")
        )
        if host_thread_surface is None:
            return _HandlerResult(status="skipped", outcome=InboundActivityOutcome.IGNORED_UNMAPPED_CONTEXT, detail="comment thread host surface not mapped")
        discord_thread = await self.bot.get_thread_by_id(
            getattr(host_thread_surface, "discord_thread_id")
        )
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
        message_row = self.database.local_community_content.create_local_community_message(
            local_community_thread_id=getattr(thread_row, "id"),
            discord_message_id=getattr(created_message, "id"),
            ap_activity_id=getattr(event, "delivery_id"),
            ap_object_id=getattr(getattr(event, "object"), "ap_id"),
            parent_ap_object_id=getattr(getattr(event, "object"), "parent_ap_id"),
            parent_discord_message_id=parent_discord_message_id,
            direction="ap_to_discord",
        )
        self._persist_inbound_activitypub_message_mapping(
            event=event,
            discord_thread_id=getattr(discord_thread, "id"),
            discord_message_id=getattr(created_message, "id"),
        )
        await self._fanout_message_to_local_subscribers(
            local_community=local_community,
            thread_row=thread_row,
            message_row=message_row,
            content=self._format_inbound_comment_body(event),
            author_display_name=None,
            source_forum_channel_id=None,
        )
        await self.federation_fanout.relay_create(
            event=event,
            local_community=local_community,
            object_kind="comment",
        )
        return _HandlerResult(status="processed", outcome=InboundActivityOutcome.APPLIED, detail="remote comment created message")


    async def _fanout_thread_to_local_subscribers(
        self,
        *,
        local_community: object,
        thread_row: object,
        title: str,
        content: str,
        author_display_name: str | None,
        source_forum_channel_id: int | None,
    ) -> None:
        """Best-effort Stage 3 fanout of one canonical post to subscribers."""
        await self._fanout_thread_to_local_participants(
            local_community=local_community,
            thread_row=thread_row,
            title=title,
            content=content,
            author_display_name=author_display_name,
            source_forum_channel_id=source_forum_channel_id,
            include_host=False,
        )

    async def _fanout_thread_to_local_participants(
        self,
        *,
        local_community: object,
        thread_row: object,
        title: str,
        content: str,
        author_display_name: str | None,
        source_forum_channel_id: int | None,
        include_host: bool,
    ) -> None:
        """Best-effort fanout of one canonical post to selected local surfaces."""
        if self.bot is None:
            return
        fanout = LocalCommunityDiscordFanout(database=self.database, bot=self.bot, policy_service=self.bridge_policy_service)
        await fanout.fanout_thread(
            local_community=local_community,
            thread_row=thread_row,
            title=title,
            content=content,
            author_display_name=author_display_name,
            source_forum_channel_id=source_forum_channel_id,
            include_host=include_host,
        )

    async def _fanout_message_to_local_subscribers(
        self,
        *,
        local_community: object,
        thread_row: object,
        message_row: object,
        content: str,
        author_display_name: str | None,
        source_forum_channel_id: int | None,
    ) -> None:
        """Best-effort Stage 3 fanout of one canonical comment to subscribers."""
        await self._fanout_message_to_local_participants(
            local_community=local_community,
            thread_row=thread_row,
            message_row=message_row,
            content=content,
            author_display_name=author_display_name,
            source_forum_channel_id=source_forum_channel_id,
            include_host=False,
        )

    async def _fanout_message_to_local_participants(
        self,
        *,
        local_community: object,
        thread_row: object,
        message_row: object,
        content: str,
        author_display_name: str | None,
        source_forum_channel_id: int | None,
        include_host: bool,
    ) -> None:
        """Best-effort fanout of one canonical comment to selected local surfaces."""
        if self.bot is None:
            return
        fanout = LocalCommunityDiscordFanout(database=self.database, bot=self.bot, policy_service=self.bridge_policy_service)
        await fanout.fanout_message(
            local_community=local_community,
            thread_row=thread_row,
            message_row=message_row,
            content=content,
            author_display_name=author_display_name,
            source_forum_channel_id=source_forum_channel_id,
            include_host=include_host,
        )


    def _surface_can_mutate(self, surface: object, local_community_id: int) -> bool:
        """Return whether one Discord surface can author canonical mutations.

        Host surfaces are always authoritative.  Local-subscriber surfaces are
        authoritative only while the matching local subscriber row remains
        active, matching Stage 4's create-source routing invariant.
        """
        if getattr(surface, "role") != "local_subscriber":
            return True
        local_subscriber = self.database.local_subscribers.get_local_subscriber(
            local_community_id=local_community_id,
            discord_channel_id=getattr(surface, "discord_forum_channel_id"),
        )
        return (
            local_subscriber is not None
            and getattr(local_subscriber, "status") == "active"
            and getattr(local_subscriber, "id") == getattr(surface, "local_subscriber_id")
        )

    async def _send_update_request(self, *, runtime: object, published: object, new_content: str) -> None:
        """Send one gateway Update request for a canonical published object."""
        gateway = getattr(runtime, "fedify_gateway", self.fedify_gateway)
        try:
            await gateway.update_content(
                UpdateContentRequest(
                    actor_username=published.actor_username,
                    community_actor_url=published.community_actor_url,
                    ap_object_id=published.object_id,
                    kind=published.kind,
                    title=published.title,
                    body_markdown=new_content,
                    in_reply_to_object_id=published.in_reply_to_object_id,
                    target_inbox_urls=self._allowed_remote_inboxes(
                        published.community_actor_url
                    ),
                )
            )
        except Exception:
            logger.exception("Failed to send local-community edit for AP object %s", published.object_id)

    async def _send_delete_request(self, *, runtime: object, published: object) -> None:
        """Send one gateway Delete request for a canonical published object."""
        gateway = getattr(runtime, "fedify_gateway", self.fedify_gateway)
        try:
            await gateway.delete_content(
                DeleteContentRequest(
                    actor_username=published.actor_username,
                    community_actor_url=published.community_actor_url,
                    ap_object_id=published.object_id,
                    target_inbox_urls=self._allowed_remote_inboxes(
                        published.community_actor_url
                    ),
                )
            )
        except Exception:
            logger.exception("Failed to send local-community delete for AP object %s", published.object_id)


    def _allowed_remote_inboxes(self, community_actor_url: str) -> list[str]:
        """Return accepted remote-subscriber inboxes allowed by current policy."""
        local_community = self.database.local_communities.get_local_community_by_actor_url(
            community_actor_url
        )
        if local_community is None:
            return []
        snapshot = self.bridge_policy_service.snapshot()
        return [
            str(row.remote_inbox_url)
            for row in self.database.remote_subscribers.list_remote_subscribers(
                getattr(local_community, "id"), status="accepted"
            )
            if snapshot.federation_decision(str(row.remote_inbox_url)).allowed
        ]

    async def _fanout_thread_edit(
        self, *, runtime: object, thread_row: object, source_surface_id: int | None, new_content: str
    ) -> None:
        """Best-effort edit fanout for starter surfaces of one canonical post."""
        bot = self.bot or getattr(runtime, "bot", None)
        if bot is None:
            return
        fanout = LocalCommunityDiscordFanout(database=self.database, bot=bot, policy_service=self.bridge_policy_service)
        await fanout.fanout_thread_starter_edit(
            thread_row=thread_row,
            source_surface_id=source_surface_id,
            new_content=new_content,
        )

    async def _fanout_message_edit(
        self, *, runtime: object, message_row: object, source_surface_id: int | None, new_content: str
    ) -> None:
        """Best-effort edit fanout for message surfaces of one canonical comment."""
        bot = self.bot or getattr(runtime, "bot", None)
        if bot is None:
            return
        fanout = LocalCommunityDiscordFanout(database=self.database, bot=bot, policy_service=self.bridge_policy_service)
        await fanout.fanout_message_edit(
            message_row=message_row,
            source_surface_id=source_surface_id,
            new_content=new_content,
        )

    async def _fanout_thread_delete(self, *, runtime: object, thread_row: object, source_surface_id: int | None) -> None:
        """Best-effort delete marker fanout for starter surfaces."""
        bot = self.bot or getattr(runtime, "bot", None)
        if bot is None:
            return
        fanout = LocalCommunityDiscordFanout(database=self.database, bot=bot, policy_service=self.bridge_policy_service)
        await fanout.fanout_thread_starter_delete(thread_row=thread_row, source_surface_id=source_surface_id)

    async def _fanout_message_delete(self, *, runtime: object, message_row: object, source_surface_id: int | None) -> None:
        """Best-effort delete marker fanout for message surfaces."""
        bot = self.bot or getattr(runtime, "bot", None)
        if bot is None:
            return
        fanout = LocalCommunityDiscordFanout(database=self.database, bot=bot, policy_service=self.bridge_policy_service)
        await fanout.fanout_message_delete(message_row=message_row, source_surface_id=source_surface_id)

    def _persist_inbound_activitypub_message_mapping(
        self,
        *,
        event: object,
        discord_thread_id: int,
        discord_message_id: int,
    ) -> None:
        """Persist the generic AP mapping for one mirrored inbound comment.

        Local-community placement is still owned by `local_community_messages`,
        but the gateway resolves later direct replies by reading
        `message_mappings.object_id`.  Persisting this row only after Discord
        send succeeds guarantees the gateway never resolves a parent that Python
        cannot place back into Discord.
        """
        object_payload = getattr(event, "object")
        object_id = getattr(object_payload, "ap_id")
        activity_id = getattr(event, "delivery_id")

        # ActivityPub deliveries are replayable.  Check both unique AP columns
        # before inserting so a duplicate relay cannot turn a successfully
        # mirrored comment into an integrity error.
        if self.database.message_mappings.get_message_mapping_by_object_id(object_id) is not None:
            return
        if self.database.message_mappings.get_message_mapping_by_activity_id(activity_id) is not None:
            return

        self.database.message_mappings.create_message_mapping(
            source_platform="activitypub",
            source_id=object_id,
            activity_id=activity_id,
            object_id=object_id,
            actor_url=getattr(event, "actor_id"),
            community_actor_url=getattr(event, "community_actor_id"),
            discord_channel_id=discord_thread_id,
            discord_message_id=discord_message_id,
        )

    async def handle_discord_message_edit(
        self,
        *,
        message_id: int,
        new_content: str,
        runtime: object,
        author_display_name: str = "",
    ) -> None:
        """Propagate one Discord-authored local-community edit to all participants.

        Stage 5 resolves the edited Discord message through surface rows first.
        That lets host and local-subscriber copies both resolve the same
        canonical AP object, while still excluding the user-edited source copy
        from local Discord fanout.
        """
        del author_display_name

        thread_surface = self.database.local_community_surfaces.get_local_community_thread_surface_by_starter_message_id(message_id)
        if thread_surface is not None:
            thread_row = self.database.local_community_surfaces.get_local_community_thread_for_surface(getattr(thread_surface, "id"))
            if thread_row is None or not self._surface_can_mutate(thread_surface, getattr(thread_row, "local_community_id")):
                return
            if self._is_community_id_disabled(getattr(thread_row, "local_community_id", None)):
                return
            published = self.database.activitypub_objects.get_published_activity_object_by_object_id(getattr(thread_row, "ap_object_id"))
            if published is None:
                return
            await self._send_update_request(runtime=runtime, published=published, new_content=new_content)
            await self._fanout_thread_edit(
                runtime=runtime,
                thread_row=thread_row,
                source_surface_id=getattr(thread_surface, "id"),
                new_content=new_content,
            )
            return

        message_surface = self.database.local_community_surfaces.get_local_community_message_surface_by_discord_message_id(message_id)
        if message_surface is None:
            return
        message_row = self.database.local_community_surfaces.get_local_community_message_for_surface(getattr(message_surface, "id"))
        if message_row is None:
            return
        thread_row = self.database.local_community_content.get_local_community_thread_by_id(getattr(message_row, "local_community_thread_id"))
        if thread_row is None or not self._surface_can_mutate(message_surface, getattr(thread_row, "local_community_id")):
            return
        if self._is_community_id_disabled(getattr(thread_row, "local_community_id", None)):
            return
        published = self.database.activitypub_objects.get_published_activity_object_by_object_id(getattr(message_row, "ap_object_id"))
        if published is None:
            return
        await self._send_update_request(runtime=runtime, published=published, new_content=new_content)
        await self._fanout_message_edit(
            runtime=runtime,
            message_row=message_row,
            source_surface_id=getattr(message_surface, "id"),
            new_content=new_content,
        )

    async def handle_discord_message_delete(
        self,
        *,
        message_id: int,
        runtime: object,
    ) -> None:
        """Propagate one Discord-authored local-community delete to participants."""
        thread_surface = self.database.local_community_surfaces.get_local_community_thread_surface_by_starter_message_id(message_id)
        if thread_surface is not None:
            thread_row = self.database.local_community_surfaces.get_local_community_thread_for_surface(getattr(thread_surface, "id"))
            if thread_row is None or not self._surface_can_mutate(thread_surface, getattr(thread_row, "local_community_id")):
                return
            if self._is_community_id_disabled(getattr(thread_row, "local_community_id", None)):
                return
            published = self.database.activitypub_objects.get_published_activity_object_by_object_id(getattr(thread_row, "ap_object_id"))
            if published is None:
                return
            await self._send_delete_request(runtime=runtime, published=published)
            await self._fanout_thread_delete(
                runtime=runtime,
                thread_row=thread_row,
                source_surface_id=getattr(thread_surface, "id"),
            )
            return

        message_surface = self.database.local_community_surfaces.get_local_community_message_surface_by_discord_message_id(message_id)
        if message_surface is None:
            return
        message_row = self.database.local_community_surfaces.get_local_community_message_for_surface(getattr(message_surface, "id"))
        if message_row is None:
            return
        thread_row = self.database.local_community_content.get_local_community_thread_by_id(getattr(message_row, "local_community_thread_id"))
        if thread_row is None or not self._surface_can_mutate(message_surface, getattr(thread_row, "local_community_id")):
            return
        if self._is_community_id_disabled(getattr(thread_row, "local_community_id", None)):
            return
        published = self.database.activitypub_objects.get_published_activity_object_by_object_id(getattr(message_row, "ap_object_id"))
        if published is None:
            return
        await self._send_delete_request(runtime=runtime, published=published)
        await self._fanout_message_delete(
            runtime=runtime,
            message_row=message_row,
            source_surface_id=getattr(message_surface, "id"),
        )

    async def handle_inbound_post_update(self, event: object, runtime: object) -> HandlerResult:
        """Edit the starter message for one inbound remote post update."""
        from ..activitypub_handlers import HandlerResult as _HandlerResult

        thread_row = self.database.local_community_content.get_local_community_thread_by_ap_object_id(getattr(getattr(event, "object"), "ap_id"))
        if thread_row is None:
            return _HandlerResult(status="skipped", outcome=InboundActivityOutcome.IGNORED_UNMAPPED_CONTEXT, detail="post not yet mapped")
        local_community = self.database.local_communities.get_local_community_by_actor_url(getattr(event, "community_actor_id"))
        await self._fanout_thread_edit(
            runtime=runtime,
            thread_row=thread_row,
            source_surface_id=None,
            new_content=self._format_inbound_post_body(event),
        )
        if local_community is not None:
            await self.federation_fanout.relay_update_or_delete(
                event=event,
                local_community=local_community,
                object_kind="post",
                operation="update",
            )
        return _HandlerResult(status="processed", outcome=InboundActivityOutcome.APPLIED, detail="post updated")

    async def handle_inbound_post_delete(self, event: object, runtime: object) -> HandlerResult:
        """Mark the starter message deleted for one inbound remote post delete."""
        from ..activitypub_handlers import HandlerResult as _HandlerResult

        thread_row = self.database.local_community_content.get_local_community_thread_by_ap_object_id(getattr(getattr(event, "object"), "ap_id"))
        if thread_row is None:
            return _HandlerResult(status="skipped", outcome=InboundActivityOutcome.IGNORED_UNMAPPED_CONTEXT, detail="post not yet mapped")
        local_community = self.database.local_communities.get_local_community_by_actor_url(getattr(event, "community_actor_id"))
        await self._fanout_thread_delete(
            runtime=runtime,
            thread_row=thread_row,
            source_surface_id=None,
        )
        if local_community is not None:
            await self.federation_fanout.relay_update_or_delete(
                event=event,
                local_community=local_community,
                object_kind="post",
                operation="delete",
            )
        return _HandlerResult(status="processed", outcome=InboundActivityOutcome.APPLIED, detail="post deleted")

    async def handle_inbound_comment_update(self, event: object, runtime: object) -> HandlerResult:
        """Edit the mirrored Discord copy for one inbound remote comment update."""
        from ..activitypub_handlers import HandlerResult as _HandlerResult

        message_row = self.database.local_community_content.get_local_community_message_by_ap_object_id(getattr(getattr(event, "object"), "ap_id"))
        if message_row is None:
            return _HandlerResult(status="skipped", outcome=InboundActivityOutcome.IGNORED_UNMAPPED_CONTEXT, detail="comment not yet mapped")
        thread_row = self.database.local_community_content.get_local_community_thread_by_id(getattr(message_row, "local_community_thread_id"))
        if thread_row is None:
            return _HandlerResult(status="skipped", outcome=InboundActivityOutcome.IGNORED_UNMAPPED_CONTEXT, detail="comment thread not mapped")
        local_community = self.database.local_communities.get_local_community_by_actor_url(getattr(event, "community_actor_id"))
        await self._fanout_message_edit(
            runtime=runtime,
            message_row=message_row,
            source_surface_id=None,
            new_content=self._format_inbound_comment_body(event),
        )
        if local_community is not None:
            await self.federation_fanout.relay_update_or_delete(
                event=event,
                local_community=local_community,
                object_kind="comment",
                operation="update",
            )
        return _HandlerResult(status="processed", outcome=InboundActivityOutcome.APPLIED, detail="comment updated")

    async def handle_inbound_comment_delete(self, event: object, runtime: object) -> HandlerResult:
        """Mark the mirrored Discord copy deleted for one inbound remote comment delete."""
        from ..activitypub_handlers import HandlerResult as _HandlerResult

        message_row = self.database.local_community_content.get_local_community_message_by_ap_object_id(getattr(getattr(event, "object"), "ap_id"))
        if message_row is None:
            return _HandlerResult(status="skipped", outcome=InboundActivityOutcome.IGNORED_UNMAPPED_CONTEXT, detail="comment not yet mapped")
        thread_row = self.database.local_community_content.get_local_community_thread_by_id(getattr(message_row, "local_community_thread_id"))
        if thread_row is None:
            return _HandlerResult(status="skipped", outcome=InboundActivityOutcome.IGNORED_UNMAPPED_CONTEXT, detail="comment thread not mapped")
        local_community = self.database.local_communities.get_local_community_by_actor_url(getattr(event, "community_actor_id"))
        await self._fanout_message_delete(
            runtime=runtime,
            message_row=message_row,
            source_surface_id=None,
        )
        if local_community is not None:
            await self.federation_fanout.relay_update_or_delete(
                event=event,
                local_community=local_community,
                object_kind="comment",
                operation="delete",
            )
        return _HandlerResult(status="processed", outcome=InboundActivityOutcome.APPLIED, detail="comment deleted")

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

        local_community = self.database.local_communities.get_local_community_by_actor_url(local_community_actor_id)
        if local_community is None:
            return _HandlerResult(status="skipped", outcome=InboundActivityOutcome.IGNORED_UNKNOWN_LOCAL_COMMUNITY, detail="unknown local community")
        decision = self.bridge_policy_service.snapshot().federation_decision(
            remote_inbox_url or remote_actor_id
        )
        if not decision.allowed:
            return _HandlerResult(
                status="skipped",
                outcome=(
                    InboundActivityOutcome.IGNORED_INSTANCE_BLOCKLISTED
                    if decision.reason.value == "blocklisted"
                    else InboundActivityOutcome.IGNORED_INSTANCE_NOT_ALLOWLISTED
                ),
                detail="remote follow denied by federation policy",
            )

        existing = self.database.remote_subscribers.get_remote_subscriber(
            local_community_id=getattr(local_community, "id"),
            remote_actor_id=remote_actor_id,
        )
        if existing is not None:
            # Accept(Follow) delivery is intentionally idempotent. A remote
            # server such as Mastodon can remain in a "requested" state if the
            # bridge persisted the remote subscriber but the original Accept was lost, so
            # repeated Follow deliveries must refresh the stored request details
            # and re-send the Accept instead of returning early.
            self.database.remote_subscribers.update_remote_subscriber_acceptance(
                local_community_id=getattr(local_community, "id"),
                remote_actor_id=remote_actor_id,
                remote_inbox_url=remote_inbox_url,
                follow_activity_id=follow_activity_id,
                status="accepted",
            )
            detail = "local community remote subscriber accepted again"
        else:
            self.database.remote_subscribers.create_remote_subscriber(
                local_community_id=getattr(local_community, "id"),
                remote_actor_id=remote_actor_id,
                remote_inbox_url=remote_inbox_url,
                follow_activity_id=follow_activity_id,
                status="accepted",
            )
            detail = "local community remote subscriber accepted"

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
        return _HandlerResult(status="processed", outcome=InboundActivityOutcome.APPLIED, detail=detail)

    async def handle_unfollow_request(
        self,
        *,
        local_community_actor_id: str,
        remote_actor_id: str,
        follow_activity_id: str | None,
    ) -> HandlerResult:
        """Remove one remote actor from a local community remote-subscriber set."""
        from ..activitypub_handlers import HandlerResult as _HandlerResult

        local_community = self.database.local_communities.get_local_community_by_actor_url(local_community_actor_id)
        if local_community is None:
            return _HandlerResult(status="skipped", outcome=InboundActivityOutcome.IGNORED_UNKNOWN_LOCAL_COMMUNITY, detail="unknown local community")
        remote_subscriber = self.database.remote_subscribers.get_remote_subscriber(
            local_community_id=getattr(local_community, "id"),
            remote_actor_id=remote_actor_id,
        )
        if remote_subscriber is None:
            return _HandlerResult(status="skipped", outcome=InboundActivityOutcome.IGNORED_UNKNOWN_FOLLOW, detail="local community remote subscriber not found")
        if follow_activity_id is not None and getattr(remote_subscriber, "follow_activity_id") != follow_activity_id:
            logger.info(
                "Local-community unfollow Follow ID mismatch community=%s remote_actor=%s stored=%s incoming=%s",
                getattr(local_community, "slug"), remote_actor_id, getattr(remote_subscriber, "follow_activity_id"), follow_activity_id,
            )
        self.database.remote_subscribers.delete_remote_subscriber(
            local_community_id=getattr(local_community, "id"),
            remote_actor_id=remote_actor_id,
        )
        return _HandlerResult(status="processed", outcome=InboundActivityOutcome.APPLIED, detail="local community remote subscriber removed")

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
            actor_id=getattr(event, "actor_id", ""),
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
