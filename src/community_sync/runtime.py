"""Central orchestration entry point for shared community sync.

CommunityRuntime is the single call boundary for all thread/message events in
both directions (Discord→AP and AP→Discord). Phase 2+ owns thread-group creation,
AP publish, and Discord fanout to sibling subscribed channels. Phase 5 adds
direct inbound AP delivery onto shared group tables, replacing the legacy
PostLink/CommentLink path. Phase 8 adds edit and delete propagation in both
directions, using the same delivery row tables for reverse-lookup.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

import discord

if TYPE_CHECKING:
    from ..activitypub_handlers import HandlerResult
    from ..activitypub_models import ActivityPubEvent
    from ..db import Database
    from ..discord_publish_service import DiscordPublishService, PublishResult
    from ..fedify_gateway_client import DeleteContentRequest, UpdateContentRequest
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

    # Phase 9: Reply to any thread starter (source, mirror, or inbound): each
    # sibling uses its own starter message as the reference so the reply is
    # anchored to the top of each mirror.
    thread_deliveries = database.get_thread_deliveries(thread_group.id)
    for delivery in thread_deliveries:
        if referenced_id == delivery.discord_starter_message_id:
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


def _resolve_inbound_reference(
    database: Database,
    parent_group: object,
    thread_delivery: object,
) -> discord.MessageReference | None:
    """Resolve the Discord reply reference for one inbound comment delivery.

    Returns a MessageReference when the parent message group has a delivery
    in this specific thread. Returns None (flat send) when the parent is
    the AP post root (parent_group=None) or when no delivery exists for
    this thread (e.g. partial prior failure — best-effort fallback).
    """
    if parent_group is None:
        return None
    delivery = database.get_message_delivery_in_thread(
        parent_group.id, thread_delivery.discord_thread_id
    )
    if delivery is None:
        return None
    # fail_if_not_exists=False: send still lands even if the referenced
    # message was deleted; Discord just suppresses the reply banner.
    return discord.MessageReference(
        message_id=delivery.discord_message_id,
        channel_id=thread_delivery.discord_thread_id,
        fail_if_not_exists=False,
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
        bot: object | None = None,
    ) -> None:
        """Initialise the runtime with the shared database, publish service, fanout, and bot.

        discord_fanout is optional so tests that only exercise AP publish paths
        do not need to construct a full BridgeBot. When None, mirror delivery is
        skipped and only the source delivery row is written.

        bot is the BridgeBot instance used for inbound AP delivery (fetch_forum_channel,
        get_thread_by_id, wait_until_bridge_ready). Optional for backward compat with
        tests that only exercise outbound paths.
        """
        self.database = database
        self.discord_publish_service = discord_publish_service
        self.discord_fanout = discord_fanout
        self.bot = bot

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

        # Resolve the thread group for the originating thread. If none exists (pre-Phase-2
        # thread or legacy path), skip message-group creation and fanout entirely.
        thread = message.channel
        thread_group = self.database.get_thread_group_by_any_thread(thread.id)
        if thread_group is None:
            return result

        # Phase 9: Compute sibling deliveries = all threads in the group except the
        # originating thread (regardless of role). This enables mirror and inbound
        # threads to fan out to all other threads, not just source threads receiving
        # from mirrors.
        sibling_deliveries = [
            d for d in self.database.get_thread_deliveries(thread_group.id)
            if d.discord_thread_id != thread.id
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
        """Handle an inbound ActivityPub post by creating Discord threads in all subscriptions.

        Deduplicates via CommunityThreadGroup.ap_object_id. Creates one thread per
        accepted subscription and records each as an 'inbound' delivery row. Does not
        write PostLink rows — group tables are the sole mapping from Phase 5 onward.

        Returns status='skipped' if the post is already mapped or no subscriptions exist.
        Returns status='processed' after successful delivery to all subscribed channels.

        Retry and partial-delivery behavior (Phase 6):
          If a thread group already exists for this ap_id (e.g. because a prior delivery
          partially succeeded for some channels but failed for others), the retry returns
          'skipped' — it does not attempt to complete missing channel deliveries. The
          get-then-create guard is not atomic: a concurrent replay can race past it, but
          the UNIQUE constraint on (thread_group_id, discord_channel_id) provides the
          final safety net at the DB level. Full per-channel retry on partial failure is
          Phase 8 scope.
        """
        from ..activitypub_handlers import HandlerResult as _HandlerResult

        # Dedup: if a thread group already exists for this AP object, a duplicate
        # or replayed event arrived — skip without creating more threads.
        if self.database.get_thread_group_by_ap_object(event.object.ap_id) is not None:
            logger.info("Post %s already mapped to a thread group — skipping", event.object.ap_id)
            return _HandlerResult(status="skipped", detail="post already mapped")

        subscriptions = self.database.get_subscriptions_by_community(event.community_actor_id)
        accepted = [s for s in subscriptions if s.status == "accepted"]
        if not accepted:
            return _HandlerResult(status="skipped", detail="no subscriptions for this community")

        # Use self.bot when available; fall back to runtime.bot for backward compat
        # with callers that pass the full Runtime (e.g. existing integration tests).
        bot = self.bot or runtime.bot
        await bot.wait_until_bridge_ready()

        # Create the canonical thread group. source_* fields are None because
        # inbound AP events have no single source Discord channel.
        # delivery_id is the closest equivalent to an activity_id for inbound events.
        thread_group = self.database.create_thread_group(
            community_actor_id=event.community_actor_id,
            source_channel_id=None,
            source_thread_id=None,
            source_starter_message_id=None,
            ap_activity_id=event.delivery_id,
            ap_object_id=event.object.ap_id,
        )

        # Lazy import avoids circular dependency: bridge_lemmy_to_discord imports db.
        from ..bridge_lemmy_to_discord import _create_inbound_discord_thread

        for subscription in accepted:
            forum_channel = await bot.fetch_forum_channel(subscription.discord_channel_id)
            thread_id, starter_message_id = await _create_inbound_discord_thread(
                forum_channel=forum_channel,
                event=event,
            )
            self.database.add_thread_delivery(
                thread_group_id=thread_group.id,
                discord_channel_id=subscription.discord_channel_id,
                discord_thread_id=thread_id,
                discord_starter_message_id=starter_message_id,
                role="inbound",
            )

        logger.info(
            "Delivered inbound post %s into %d subscribed channel(s)",
            event.object.ap_id, len(accepted),
        )
        return _HandlerResult(status="processed", detail="post created")

    async def handle_inbound_comment(
        self,
        event: ActivityPubEvent,
        runtime: Runtime,
    ) -> HandlerResult:
        """Handle an inbound ActivityPub comment by delivering it into all mapped threads.

        Deduplicates via CommunityMessageGroup.ap_object_id. Resolves the parent thread
        group via post_ap_id and the parent message group via parent_ap_id (when present).
        Delivers the comment into every thread in the thread group with the correct per-thread
        Discord reply reference. Does not write CommentLink rows.

        Returns status='skipped' if already mapped, status='deferred' if the parent post
        is not yet mapped, status='processed' after successful delivery.

        Retry and partial-delivery behavior (Phase 6):
          If a message group already exists for this ap_id, the retry returns 'skipped'.
          The get-then-create guard is not atomic; the UNIQUE constraint on
          CommunityMessageGroupDelivery.discord_message_id is the final safety net.
          Partial delivery (some threads succeeded, some failed) is not retried here —
          the message group row already exists, so the retry hits the 'already mapped'
          guard and skips entirely. Full per-thread retry on partial failure is Phase 8 scope.
        """
        from ..activitypub_handlers import HandlerResult as _HandlerResult

        logger.info(
            "[handle_inbound_comment] ap_id=%s post_ap_id=%s parent_ap_id=%s",
            event.object.ap_id, event.object.post_ap_id, event.object.parent_ap_id,
        )

        # Dedup: if a message group already exists for this AP object, skip.
        if self.database.get_message_group_by_ap_object(event.object.ap_id) is not None:
            logger.info("Comment %s already mapped — skipping", event.object.ap_id)
            return _HandlerResult(status="skipped", detail="comment already mapped")

        # Resolve the thread group via the post AP id. If the post hasn't been
        # delivered yet, defer so the caller can retry when the post arrives.
        thread_group = self.database.get_thread_group_by_ap_object(
            event.object.post_ap_id or ""
        )
        if thread_group is None:
            logger.info(
                "Skipping comment %s — parent post %s not yet mapped",
                event.object.ap_id, event.object.post_ap_id,
            )
            return _HandlerResult(status="deferred", detail="parent post not mapped yet")

        logger.info(
            "[handle_inbound_comment] thread_group=%s deliveries=%s",
            thread_group.id,
            [d.discord_thread_id for d in self.database.get_thread_deliveries(thread_group.id)],
        )

        bot = self.bot or runtime.bot
        await bot.wait_until_bridge_ready()

        # Resolve the parent message group when this is a reply to a prior comment.
        # get_message_group_by_ap_object returns None when parent_ap_id is the post
        # itself (root comment), which is the correct flat-send fallback.
        parent_group = None
        if event.object.parent_ap_id:
            parent_group = self.database.get_message_group_by_ap_object(
                event.object.parent_ap_id
            )

        message_group = self.database.create_message_group(
            community_actor_id=event.community_actor_id,
            thread_group_id=thread_group.id,
            source_channel_id=None,
            source_thread_id=None,
            source_message_id=None,
            ap_activity_id=event.delivery_id,
            ap_object_id=event.object.ap_id,
            parent_message_group_id=parent_group.id if parent_group else None,
        )

        from ..bridge_lemmy_to_discord import _send_inbound_comment

        thread_deliveries = self.database.get_thread_deliveries(thread_group.id)
        for thread_delivery in thread_deliveries:
            thread = await bot.get_thread_by_id(thread_delivery.discord_thread_id)
            # Resolve the per-thread Discord reply reference. Returns None when the
            # parent is the post root or when no delivery exists for this thread.
            reference = _resolve_inbound_reference(self.database, parent_group, thread_delivery)
            message = await _send_inbound_comment(
                thread=thread,
                event=event,
                reference=reference,
            )
            self.database.add_message_delivery(
                message_group_id=message_group.id,
                discord_channel_id=thread_delivery.discord_channel_id,
                discord_thread_id=thread_delivery.discord_thread_id,
                discord_message_id=message.id,
                role="inbound",
            )

        logger.info(
            "Delivered inbound comment %s into %d thread(s)",
            event.object.ap_id, len(thread_deliveries),
        )
        return _HandlerResult(status="processed", detail="comment created")


    async def handle_discord_message_edit(
        self,
        message_id: int,
        new_content: str,
        runtime: Runtime,
    ) -> None:
        """Propagate a Discord source-message edit to all mirror messages and to AP.

        Resolves the message group by the edited message ID, edits all mirror
        deliveries via DiscordFanout, then sends one AP Update to the gateway.
        Returns silently if no delivery row exists for message_id (unknown message).

        If a mirror edit fails, the error is logged and the AP Update is still sent —
        individual mirror failures do not abort the outbound AP propagation.
        """
        from ..fedify_gateway_client import UpdateContentRequest

        # Reverse-lookup the message group by the Discord message ID.
        # Covers both source and mirror deliveries via the delivery table.
        message_group = self.database.get_message_group_by_delivered_message(message_id)
        if message_group is None:
            return

        # Mirror deliveries are the non-source copies that need to be updated.
        all_deliveries = self.database.get_message_deliveries(message_group.id)
        mirror_deliveries = [d for d in all_deliveries if d.role != "source"]

        if self.discord_fanout is not None and mirror_deliveries:
            await self.discord_fanout.propagate_edit(
                mirror_deliveries=mirror_deliveries,
                new_content=new_content,
            )

        # Send the AP Update if the message group has an AP object and community actor.
        # These are set for all Discord-originated messages published via the bridge.
        if message_group.ap_object_id and message_group.community_actor_id:
            # Resolve the actor username from the source thread's publish record.
            # The actor owns the AP object and must be the one who sends Update.
            actor_username = await _resolve_actor_username(
                self.database, message_group
            )
            if actor_username:
                try:
                    # For comments, resolve the parent post AP object ID
                    in_reply_to_object_id = None
                    if message_group.parent_message_group_id:
                        parent = self.database.get_message_group_by_id(message_group.parent_message_group_id)
                        if parent:
                            in_reply_to_object_id = parent.ap_object_id
                    elif message_group.thread_group_id:
                        thread_group = self.database.get_thread_group_by_id(message_group.thread_group_id)
                        if thread_group:
                            in_reply_to_object_id = thread_group.ap_object_id

                    await runtime.fedify_gateway.update_content(UpdateContentRequest(
                        actor_username=actor_username,
                        community_actor_url=message_group.community_actor_id,
                        ap_object_id=message_group.ap_object_id,
                        kind="comment",
                        body_markdown=new_content,
                        in_reply_to_object_id=in_reply_to_object_id,
                    ))
                except Exception:
                    logger.exception("Failed to send message edit to AP gateway")

    async def handle_discord_message_delete(
        self,
        message_id: int,
        runtime: Runtime,
    ) -> None:
        """Propagate a Discord source-message delete to all mirror messages and to AP.

        Resolves the message group by the deleted message ID, deletes all mirror
        deliveries via DiscordFanout, then sends one AP Delete to the gateway.
        Returns silently if no delivery row exists for message_id (unknown message).

        If a mirror delete fails, the error is logged and the AP Delete is still sent —
        individual mirror failures do not abort the outbound AP propagation.
        """
        from ..fedify_gateway_client import DeleteContentRequest

        message_group = self.database.get_message_group_by_delivered_message(message_id)
        if message_group is None:
            return

        all_deliveries = self.database.get_message_deliveries(message_group.id)
        mirror_deliveries = [d for d in all_deliveries if d.role != "source"]

        if self.discord_fanout is not None and mirror_deliveries:
            await self.discord_fanout.propagate_delete(mirror_deliveries=mirror_deliveries)

        if message_group.ap_object_id and message_group.community_actor_id:
            actor_username = await _resolve_actor_username(
                self.database, message_group
            )
            if actor_username:
                try:
                    await runtime.fedify_gateway.delete_content(DeleteContentRequest(
                        actor_username=actor_username,
                        community_actor_url=message_group.community_actor_id,
                        ap_object_id=message_group.ap_object_id,
                    ))
                except Exception:
                    logger.exception("Failed to send message delete to AP gateway")

    async def handle_inbound_post_update(
        self,
        event: ActivityPubEvent,
        runtime: Runtime,
    ) -> HandlerResult:
        """Handle an inbound AP Update for a post by editing all Discord thread starters.

        Resolves thread group via ap_object_id. If not found, returns 'skipped' —
        the Update arrived before the original Create and no deferred retry is attempted.
        Edits all thread deliveries concurrently via asyncio.gather.
        """
        from ..activitypub_handlers import HandlerResult as _HandlerResult

        thread_group = self.database.get_thread_group_by_ap_object(event.object.ap_id)
        if thread_group is None:
            logger.info("Post update for %s — no thread group found, skipping", event.object.ap_id)
            return _HandlerResult(status="skipped", detail="post not yet mapped")

        thread_deliveries = self.database.get_thread_deliveries(thread_group.id)
        if not thread_deliveries:
            return _HandlerResult(status="skipped", detail="no thread deliveries")

        bot = self.bot or runtime.bot
        new_content = event.object.body_markdown or ""

        async def _edit_thread_starter(delivery: object) -> None:
            try:
                thread = await bot.get_thread_by_id(delivery.discord_thread_id)
                starter = await thread.fetch_message(delivery.discord_starter_message_id)
                await starter.edit(content=new_content)
                logger.info(
                    "Edited inbound post starter in thread %s", delivery.discord_thread_id
                )
            except Exception:
                logger.exception(
                    "Failed to edit inbound post starter in thread %s",
                    delivery.discord_thread_id,
                )

        # Edit all thread starters concurrently; failures are logged individually.
        await asyncio.gather(
            *[_edit_thread_starter(d) for d in thread_deliveries],
            return_exceptions=True,
        )

        return _HandlerResult(status="processed", detail="post updated")

    async def handle_inbound_post_delete(
        self,
        event: ActivityPubEvent,
        runtime: Runtime,
    ) -> HandlerResult:
        """Handle an inbound AP Delete for a post by deleting all Discord threads.

        Resolves thread group via ap_object_id. Missing deliveries are skipped.
        Requires MANAGE_THREADS permission; failure is logged as a partial failure.
        """
        from ..activitypub_handlers import HandlerResult as _HandlerResult

        thread_group = self.database.get_thread_group_by_ap_object(event.object.ap_id)
        if thread_group is None:
            logger.info("Post delete for %s — no thread group found, skipping", event.object.ap_id)
            return _HandlerResult(status="skipped", detail="post not yet mapped")

        thread_deliveries = self.database.get_thread_deliveries(thread_group.id)

        bot = self.bot or runtime.bot

        async def _delete_thread(delivery: object) -> None:
            try:
                thread = await bot.get_thread_by_id(delivery.discord_thread_id)
                await thread.delete()
                logger.info(
                    "Deleted inbound post thread %s", delivery.discord_thread_id
                )
            except Exception:
                logger.exception(
                    "Failed to delete inbound post thread %s", delivery.discord_thread_id
                )

        # Delete all threads concurrently; each failure is logged without aborting others.
        await asyncio.gather(
            *[_delete_thread(d) for d in thread_deliveries],
            return_exceptions=True,
        )

        return _HandlerResult(status="processed", detail="post deleted")

    async def handle_inbound_comment_update(
        self,
        event: ActivityPubEvent,
        runtime: Runtime,
    ) -> HandlerResult:
        """Handle an inbound AP Update for a comment by editing all Discord message deliveries.

        Resolves message group via ap_object_id. Returns 'skipped' if not found.
        Edits all delivery messages concurrently via asyncio.gather.
        """
        from ..activitypub_handlers import HandlerResult as _HandlerResult

        message_group = self.database.get_message_group_by_ap_object(event.object.ap_id)
        if message_group is None:
            logger.info(
                "Comment update for %s — no message group found, skipping",
                event.object.ap_id,
            )
            return _HandlerResult(status="skipped", detail="comment not yet mapped")

        deliveries = self.database.get_message_deliveries(message_group.id)
        if not deliveries:
            return _HandlerResult(status="skipped", detail="no message deliveries")

        bot = self.bot or runtime.bot
        new_content = event.object.body_markdown or ""

        async def _edit_message(delivery: object) -> None:
            try:
                thread = await bot.get_thread_by_id(delivery.discord_thread_id)
                message = await thread.fetch_message(delivery.discord_message_id)
                await message.edit(content=new_content)
                logger.info(
                    "Edited inbound comment message %s in thread %s",
                    delivery.discord_message_id,
                    delivery.discord_thread_id,
                )
            except Exception:
                logger.exception(
                    "Failed to edit inbound comment message %s in thread %s",
                    delivery.discord_message_id,
                    delivery.discord_thread_id,
                )

        await asyncio.gather(
            *[_edit_message(d) for d in deliveries],
            return_exceptions=True,
        )

        return _HandlerResult(status="processed", detail="comment updated")

    async def handle_inbound_comment_delete(
        self,
        event: ActivityPubEvent,
        runtime: Runtime,
    ) -> HandlerResult:
        """Handle an inbound AP Delete for a comment by deleting all Discord message deliveries.

        Resolves message group via ap_object_id. Missing deliveries are skipped without error.
        Deletes all delivery messages concurrently via asyncio.gather.
        """
        from ..activitypub_handlers import HandlerResult as _HandlerResult

        message_group = self.database.get_message_group_by_ap_object(event.object.ap_id)
        if message_group is None:
            logger.info(
                "Comment delete for %s — no message group found, skipping",
                event.object.ap_id,
            )
            return _HandlerResult(status="skipped", detail="comment not yet mapped")

        deliveries = self.database.get_message_deliveries(message_group.id)

        bot = self.bot or runtime.bot

        async def _delete_message(delivery: object) -> None:
            try:
                thread = await bot.get_thread_by_id(delivery.discord_thread_id)
                message = await thread.fetch_message(delivery.discord_message_id)
                await message.delete()
                logger.info(
                    "Deleted inbound comment message %s in thread %s",
                    delivery.discord_message_id,
                    delivery.discord_thread_id,
                )
            except Exception:
                logger.exception(
                    "Failed to delete inbound comment message %s in thread %s",
                    delivery.discord_message_id,
                    delivery.discord_thread_id,
                )

        await asyncio.gather(
            *[_delete_message(d) for d in deliveries],
            return_exceptions=True,
        )

        return _HandlerResult(status="processed", detail="comment deleted")


async def _resolve_actor_username(database: Database, message_group: object) -> str | None:
    """Resolve the AP actor username for a message group's source message.

    Looks up the user record that owns the source Discord message publish.
    Returns None if no user is found (e.g. legacy messages without actor mapping).
    The actor username is required to send Update/Delete activities — Lemmy
    enforces that the actor matches the original attributedTo.
    """
    from ..models import MessageMapping, User
    from sqlalchemy import select

    source_message_id = getattr(message_group, "source_message_id", None)
    if source_message_id is None:
        return None

    # Look up the MessageMapping row for this source message to find the actor.
    with database.session() as session:
        mapping = session.scalar(
            select(MessageMapping).where(
                MessageMapping.discord_message_id == str(source_message_id)
            )
        )
        if mapping is None:
            return None
        user = session.scalar(
            select(User).where(User.actor_url == mapping.actor_url)
        )
        return user.activitypub_username if user else None


def _ignored_result(reason: str) -> PublishResult:
    """Build a PublishResult for events that are intentionally skipped.

    Used when handle_discord_thread_create detects a duplicate and exits early
    without touching the AP gateway or creating any DB rows.
    """
    from ..discord_publish_service import PublishResult
    return PublishResult(status="ignored", reason=reason)
