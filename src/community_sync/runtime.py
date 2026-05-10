"""Central orchestration entry point for shared community sync.

CommunityRuntime is the single call boundary for all thread/message events in
both directions (Discord→AP and AP→Discord). Phase 2 owns thread-group creation,
AP publish, and Discord fanout to sibling subscribed channels.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import discord

    from ..activitypub_handlers import HandlerResult
    from ..activitypub_models import ActivityPubEvent
    from ..db import Database
    from ..discord_publish_service import DiscordPublishService, PublishResult
    from ..runtime import Runtime
    from .discord_fanout import DiscordFanout

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _ReplyContext:
    """Carry per-thread reply reference IDs resolved before fanout begins.

    parent_message_group_id is set when the source message replies to a known
    message group, so handle_discord_message can populate the FK on creation.

    per_thread_references maps mirror thread_id -> discord message_id to use
    as the Discord reference when sending into that thread. A None value means
    flat send (no reference) for that thread.
    """

    parent_message_group_id: int | None
    per_thread_references: dict[int, int | None]

    def get_reference_for_thread(self, thread_id: int) -> int | None:
        """Return the Discord message ID to reference in this thread, or None for flat send."""
        return self.per_thread_references.get(thread_id)


def _resolve_reply_context(
    database: Database,
    message: object,
    thread_group: object,
    sibling_deliveries: list,
) -> _ReplyContext:
    """Resolve reply reference IDs for each sibling thread before fanout.

    Covers four cases:
    - No reference on source message → flat send for all siblings.
    - Reference is the source starter → each sibling uses its own starter message.
    - Reference maps to a known message group → per-thread delivery lookup.
    - Reference is unknown (pre-Phase-3 or out-of-thread) → flat send.

    Must be called after sibling_deliveries is computed but before
    create_message_group, so parent_message_group_id is available at group
    creation time.
    """
    reference = getattr(message, "reference", None)
    referenced_id = getattr(reference, "message_id", None) if reference else None

    if referenced_id is None:
        # Root message: no reference needed for any sibling.
        return _ReplyContext(
            parent_message_group_id=None,
            per_thread_references={d.discord_thread_id: None for d in sibling_deliveries},
        )

    # Reply to the source thread's starter: each sibling uses its own starter
    # message as the reference so the reply is anchored to the top of each mirror.
    if referenced_id == thread_group.source_starter_message_id:
        return _ReplyContext(
            parent_message_group_id=None,
            per_thread_references={
                d.discord_thread_id: d.discord_starter_message_id
                for d in sibling_deliveries
            },
        )

    # Look up whether the referenced message belongs to a known message group.
    # This covers replies to previously mirrored messages (Phase 3+).
    parent_group = database.get_message_group_by_delivered_message(referenced_id)
    if parent_group is None:
        # Unknown reference (pre-Phase-3 message or cross-thread reference):
        # fall back to flat send so the mirror is not silently dropped.
        return _ReplyContext(
            parent_message_group_id=None,
            per_thread_references={d.discord_thread_id: None for d in sibling_deliveries},
        )

    # Known mirrored message: resolve the per-thread mirror delivery so each
    # sibling references the correct local copy of the parent message.
    per_thread: dict[int, int | None] = {}
    for d in sibling_deliveries:
        mirror_delivery = database.get_message_delivery_in_thread(
            parent_group.id, d.discord_thread_id
        )
        # If no delivery exists for this thread (e.g. partial prior failure),
        # fall back to flat send for that specific sibling.
        per_thread[d.discord_thread_id] = (
            mirror_delivery.discord_message_id if mirror_delivery else None
        )

    return _ReplyContext(
        parent_message_group_id=parent_group.id,
        per_thread_references=per_thread,
    )


class CommunityRuntime:
    """Orchestrate all shared community sync events through one stable call boundary.

    Phase 2: owns thread-group creation, AP publish via DiscordPublishService,
    and local Discord fanout via DiscordFanout for sibling subscribed channels.
    Thread dedup is enforced here to prevent double-publish on Discord reconnects.
    """

    def __init__(
        self,
        *,
        database: Database,
        discord_publish_service: DiscordPublishService,
        discord_fanout: DiscordFanout | None = None,
    ) -> None:
        """Initialise the runtime with the shared database, publish service, and fanout.

        discord_fanout is optional so tests that only exercise AP publish paths
        do not need to construct a full BridgeBot. When None, mirror delivery is
        skipped and only the source delivery row is written.
        """
        self.database = database
        self.discord_publish_service = discord_publish_service
        self.discord_fanout = discord_fanout

    async def handle_discord_thread_create(
        self,
        thread: discord.Thread,
        starter_message: discord.Message,
    ) -> PublishResult:
        """Handle a new Discord forum thread from a subscribed channel.

        Deduplicates via CommunityThreadGroup, publishes to AP, creates the
        canonical thread-group row, records source delivery, then fans out to
        all sibling subscribed channels and records each mirror delivery.

        Returns status="ignored" with reason="duplicate_discord_thread" if this
        thread was already processed (e.g. a Discord reconnect re-fires the event).
        """
        # Dedup: if a thread group already exists for this source thread,
        # a reconnect or duplicate event fired — skip without re-publishing.
        if self.database.get_thread_group_by_source_thread(thread.id) is not None:
            logger.info("Thread %s already has a thread group — skipping duplicate", thread.id)
            return _ignored_result("duplicate_discord_thread")

        # Publish to AP via existing service; return early on non-publish outcomes.
        result = await self.discord_publish_service.publish_thread_starter(
            thread=thread,
            starter_message=starter_message,
        )
        if result.status != "published":
            return result

        # Resolve subscription for the source channel to get community_actor_id.
        # subscription is guaranteed non-None here: publish_thread_starter already
        # validated it and returned status="published" only when it was accepted.
        subscription = self.database.get_subscription_by_channel(thread.parent_id)

        # Create the canonical thread group for this source event.
        thread_group = self.database.create_thread_group(
            community_actor_id=subscription.lemmy_community_actor_id,
            source_channel_id=thread.parent_id,
            source_thread_id=thread.id,
            source_starter_message_id=starter_message.id,
            ap_activity_id=result.activity_id,
            ap_object_id=result.object_id,
        )

        # Record source delivery row for this channel.
        self.database.add_thread_delivery(
            thread_group_id=thread_group.id,
            discord_channel_id=thread.parent_id,
            discord_thread_id=thread.id,
            discord_starter_message_id=starter_message.id,
            role="source",
        )

        # Resolve sibling subscriptions: all accepted subscriptions for the same
        # community except the source channel.
        all_subs = self.database.get_subscriptions_by_community(
            subscription.lemmy_community_actor_id
        )
        sibling_channel_ids = [
            s.discord_channel_id
            for s in all_subs
            if s.discord_channel_id != thread.parent_id
        ]

        if sibling_channel_ids and self.discord_fanout is not None:
            mirror_results = await self.discord_fanout.mirror_thread_to_siblings(
                source_thread=thread,
                source_starter_message=starter_message,
                sibling_channel_ids=sibling_channel_ids,
            )
            for mirror in mirror_results:
                # Record each mirror delivery so the on_message guard can
                # identify mirror threads and skip re-publishing to AP.
                self.database.add_thread_delivery(
                    thread_group_id=thread_group.id,
                    discord_channel_id=mirror.channel_id,
                    discord_thread_id=mirror.thread_id,
                    discord_starter_message_id=mirror.starter_message_id,
                    role="mirror",
                )

        return result

    async def handle_discord_message(
        self,
        message: discord.Message,
    ) -> PublishResult:
        """Handle a new Discord thread message from a subscribed channel.

        Deduplicates via CommunityMessageGroup, publishes to AP via
        DiscordPublishService, creates the canonical message-group row, records
        source delivery, then fans out to all sibling mirror threads and records
        each mirror delivery. If the source thread has no CommunityThreadGroup
        (pre-Phase-2 / legacy thread), AP publish happens but no message-group
        rows are written and no fanout is attempted.

        Returns status='ignored' with reason='duplicate_discord_message' if this
        message was already processed (e.g. a Discord reconnect re-fires the event).
        """
        # Dedup: if a message group already exists for this source message,
        # a reconnect or duplicate Discord event fired — skip without re-publishing.
        if self.database.get_message_group_by_source_message(message.id) is not None:
            logger.info("Message %s already has a message group — skipping duplicate", message.id)
            return _ignored_result("duplicate_discord_message")

        # AP publish via existing service; return early on non-publish outcomes.
        result = await self.discord_publish_service.publish_thread_message(message=message)
        if result.status != "published":
            return result

        # Resolve the thread group for the source thread. If none exists (pre-Phase-2
        # thread or legacy path), skip message-group creation and fanout entirely.
        thread = message.channel
        thread_group = self.database.get_thread_group_by_source_thread(thread.id)
        if thread_group is None:
            return result

        # Compute sibling deliveries before creating the message group so that
        # _resolve_reply_context can build the per_thread_references map, and the
        # parent_message_group_id FK is available at group creation time.
        sibling_deliveries = [
            d for d in self.database.get_thread_deliveries(thread_group.id)
            if d.role == "mirror"
        ]

        # Resolve reply context from the source message's Discord reference.
        # This determines parent_message_group_id and the per-thread reference IDs
        # that each sibling thread send will use.
        reply_context = _resolve_reply_context(
            self.database, message, thread_group, sibling_deliveries
        )

        # Create the canonical message group for this source event, recording the
        # parent message group when this is a reply to a known mirrored message.
        message_group = self.database.create_message_group(
            community_actor_id=thread_group.community_actor_id,
            thread_group_id=thread_group.id,
            source_channel_id=thread.parent_id,
            source_thread_id=thread.id,
            source_message_id=message.id,
            ap_activity_id=result.activity_id,
            ap_object_id=result.object_id,
            parent_message_group_id=reply_context.parent_message_group_id,
        )
        # Record the source delivery so reply-chain resolution and dedup can find
        # this message by its Discord message ID later.
        self.database.add_message_delivery(
            message_group_id=message_group.id,
            discord_channel_id=thread.parent_id,
            discord_thread_id=thread.id,
            discord_message_id=message.id,
            role="source",
        )

        # Fan out to sibling mirror threads with per-thread Discord references.
        if self.discord_fanout is not None and sibling_deliveries:
            mirror_results = await self.discord_fanout.mirror_message_to_siblings(
                source_message=message,
                sibling_thread_deliveries=sibling_deliveries,
                reply_context=reply_context,
            )
            for mirror in mirror_results:
                # Each successfully delivered mirror message gets its own
                # delivery row so the on_message guard can identify mirror-thread
                # messages and the reply chain can be resolved correctly.
                self.database.add_message_delivery(
                    message_group_id=message_group.id,
                    discord_channel_id=mirror.channel_id,
                    discord_thread_id=mirror.thread_id,
                    discord_message_id=mirror.message_id,
                    role="mirror",
                )

        return result

    async def handle_inbound_post(
        self,
        event: ActivityPubEvent,
        runtime: Runtime,
    ) -> HandlerResult:
        """Handle an inbound ActivityPub post event.

        Delegates to the private _deliver_post helper in activitypub_handlers,
        bypassing the public handle_post_created entry point to avoid a circular
        call chain. Echo suppression is applied at the entry point before routing
        here, so _deliver_post receives only genuine inbound events.
        """
        # Lazy import avoids a circular module dependency: activitypub_handlers
        # imports Runtime which imports CommunityRuntime. The import is safe at
        # call time because all modules are fully initialised by then.
        from ..activitypub_handlers import _deliver_post
        return await _deliver_post(event, runtime)

    async def handle_inbound_comment(
        self,
        event: ActivityPubEvent,
        runtime: Runtime,
    ) -> HandlerResult:
        """Handle an inbound ActivityPub comment event.

        Delegates to the private _deliver_comment helper in activitypub_handlers.
        Echo suppression is applied at the entry point before routing here.
        """
        # Lazy import for the same circular-dependency reason as handle_inbound_post.
        from ..activitypub_handlers import _deliver_comment
        return await _deliver_comment(event, runtime)


def _ignored_result(reason: str) -> PublishResult:
    """Build a PublishResult for events that are intentionally skipped.

    Used when handle_discord_thread_create detects a duplicate and exits early
    without touching the AP gateway or creating any DB rows.
    """
    from ..discord_publish_service import PublishResult
    return PublishResult(status="ignored", reason=reason)
