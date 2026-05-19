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
from typing import TYPE_CHECKING

import discord

from .backfill import backfill_post_as_thread_group
from .delivery_mapping import (
    get_message_group_for_ap_object,
    get_message_group_for_delivered_message,
    get_message_group_for_source_message,
    get_sibling_thread_deliveries,
    get_thread_group_for_any_thread,
    get_thread_group_for_ap_object,
    get_thread_group_for_source_thread,
)
from .edit_delete import (
    get_inbound_comment_edit_deliveries,
    get_outbound_delete_deliveries,
    get_outbound_edit_deliveries,
    propagate_inbound_comment_delete,
    propagate_inbound_comment_update,
    propagate_inbound_post_delete,
    propagate_inbound_post_update,
    resolve_actor_username,
)
from .inbound_mapping import get_accepted_subscriptions, get_parent_message_group, needs_backfill
from .reply_mapping import resolve_inbound_reference, resolve_reply_context

if TYPE_CHECKING:
    from ..activitypub_handlers import HandlerResult
    from ..activitypub_models import ActivityPubEvent
    from ..db import Database
    from ..discord_publish_service import ContentPublishService, PublishResult
    from ..fedify_gateway_client import DeleteContentRequest, UpdateContentRequest
    from ..runtime import Runtime
    from .discord_fanout import DiscordFanout

logger = logging.getLogger(__name__)


class CommunityRuntime:
    """Orchestrate all shared community sync events through one stable call boundary.

    Phase 2: owns thread-group creation, AP publish via ContentPublishService,
    and local Discord fanout via DiscordFanout for sibling subscribed channels.
    Thread dedup is enforced here to prevent double-publish on Discord reconnects.
    """

    def __init__(
        self,
        *,
        database: Database,
        content_publish_service: ContentPublishService | None = None,
        discord_publish_service: ContentPublishService | None = None,
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
        # Accept the old keyword for test compatibility while the rest of the
        # suite migrates to the clearer `content_publish_service` name.
        self.content_publish_service = content_publish_service or discord_publish_service
        if self.content_publish_service is None:
            raise ValueError("CommunityRuntime requires a content publish service")
        # Keep the old attribute name as an alias so older tests and helpers
        # that patch `.discord_publish_service` continue to work during the
        # migration to the clearer service name.
        self.discord_publish_service = self.content_publish_service
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
        if get_thread_group_for_source_thread(self.database, thread.id) is not None:
            logger.info("Thread %s already has a thread group — skipping duplicate", thread.id)
            return _ignored_result("duplicate_discord_thread")

        # Publish to AP via existing service; return early on non-publish outcomes.
        result = await self.content_publish_service.publish_thread_starter(
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
        if get_message_group_for_source_message(self.database, message.id) is not None:
            logger.info("Message %s already has a message group — skipping duplicate", message.id)
            return _ignored_result("duplicate_discord_message")

        # AP publish via existing service; return early on non-publish outcomes.
        result = await self.content_publish_service.publish_thread_message(message=message)
        if result.status != "published":
            return result

        # Resolve the thread group for the originating thread. If none exists (pre-Phase-2
        # thread or legacy path), skip message-group creation and fanout entirely.
        thread = message.channel
        thread_group = get_thread_group_for_any_thread(self.database, thread.id)
        if thread_group is None:
            return result

        # Phase 9: Compute sibling deliveries = all threads in the group except the
        # originating thread (regardless of role). This enables mirror and inbound
        # threads to fan out to all other threads, not just source threads receiving
        # from mirrors.
        sibling_deliveries = get_sibling_thread_deliveries(
            self.database, thread_group_id=thread_group.id, source_thread_id=thread.id
        )

        # Resolve reply context from the source message's Discord reference.
        # This determines parent_message_group_id and the per-thread reference IDs
        # that each sibling thread send will use.
        reply_context = resolve_reply_context(
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
        if get_thread_group_for_ap_object(self.database, event.object.ap_id) is not None:
            logger.info("Post %s already mapped to a thread group — skipping", event.object.ap_id)
            return _HandlerResult(status="skipped", detail="post already mapped")

        accepted = get_accepted_subscriptions(self.database, event.community_actor_id)
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

        If the parent post has no CommunityThreadGroup yet, attempts on-demand backfill:
        fetches the post from the remote AP endpoint, creates the Discord thread in all
        subscribed channels that have no delivery row, and persists the group/delivery rows.
        Falls back to deferred if the fetch or parse fails.

        Returns status='skipped' if already mapped, status='deferred' if the parent post
        is not yet mapped and cannot be fetched, status='processed' after successful delivery.

        Retry and partial-delivery behavior (Phase 6):
          If a message group already exists for this ap_id, the retry returns 'skipped'.
          The get-then-create guard is not atomic; the UNIQUE constraint on
          CommunityMessageGroupDelivery.discord_message_id is the final safety net.
          Partial delivery (some threads succeeded, some failed) is not retried here —
          the message group row already exists, so the retry hits the 'already mapped'
          guard and skips entirely. Full per-thread retry on partial failure is Phase 8 scope.
        """
        from ..activitypub_handlers import HandlerResult as _HandlerResult

        logger.debug(
            "[handle_inbound_comment] ap_id=%s post_ap_id=%s parent_ap_id=%s",
            event.object.ap_id, event.object.post_ap_id, event.object.parent_ap_id,
        )

        # Dedup: if a message group already exists for this AP object, skip.
        if get_message_group_for_ap_object(self.database, event.object.ap_id) is not None:
            logger.info("Comment %s already mapped — skipping", event.object.ap_id)
            return _HandlerResult(status="skipped", detail="comment already mapped")

        # Resolve the thread group and ensure all subscribed channels have a delivery
        # row. When the group is missing entirely, or when some channels have no
        # delivery row yet (partial prior delivery), backfill fills the gaps by
        # fetching the post from the remote AP endpoint. Fetch failure → deferred.
        post_ap_id = event.object.post_ap_id or ""
        thread_group = get_thread_group_for_ap_object(self.database, post_ap_id)
        bot = self.bot or runtime.bot

        should_backfill = needs_backfill(
            thread_group=thread_group,
            community_actor_id=event.community_actor_id,
            database=self.database,
        )
        if should_backfill:
            thread_group = await backfill_post_as_thread_group(
                post_ap_id=post_ap_id,
                community_actor_id=event.community_actor_id,
                delivery_id=event.delivery_id,
                bot=bot,
                database=self.database,
            )
            if thread_group is None:
                logger.info(
                    "Deferring comment %s — parent post %s not mapped and fetch failed",
                    event.object.ap_id, event.object.post_ap_id,
                )
                return _HandlerResult(status="deferred", detail="parent post not mapped and fetch failed")

        logger.debug(
            "[handle_inbound_comment] thread_group=%s deliveries=%s",
            thread_group.id,
            [d.discord_thread_id for d in self.database.get_thread_deliveries(thread_group.id)],
        )

        bot = self.bot or runtime.bot
        await bot.wait_until_bridge_ready()

        # Resolve the parent message group when this is a reply to a prior comment.
        # get_message_group_by_ap_object returns None when parent_ap_id is the post
        # itself (root comment), which is the correct flat-send fallback.
        parent_group = get_parent_message_group(self.database, event.object.parent_ap_id)

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
            reference = resolve_inbound_reference(
                self.database, parent_group, thread_delivery
            )
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
        author_display_name: str = "",
    ) -> None:
        """Propagate a Discord source-message edit to all mirror messages and to AP.

        Resolves the message group by the edited message ID, edits all mirror
        deliveries via DiscordFanout, then sends one AP Update to the gateway.
        Returns silently if no delivery row exists for message_id (unknown message).

        author_display_name is used to build the mirror header so the attribution
        line is preserved even when the mirror message had no prior header.

        If a mirror edit fails, the error is logged and the AP Update is still sent —
        individual mirror failures do not abort the outbound AP propagation.
        """
        from ..fedify_gateway_client import UpdateContentRequest

        # Reverse-lookup the message group by the Discord message ID.
        # Covers both source and mirror deliveries via the delivery table.
        message_group = get_message_group_for_delivered_message(self.database, message_id)
        if message_group is None:
            return

        # Mirror deliveries are the bot-owned copies that need to be updated.
        # Only role="mirror" — inbound deliveries are owned by the AP sender,
        # not the bot, so editing them would result in a 403 Forbidden.
        all_deliveries = self.database.get_message_deliveries(message_group.id)
        mirror_deliveries = get_outbound_edit_deliveries(all_deliveries)

        if self.discord_fanout is not None and mirror_deliveries:
            await self.discord_fanout.propagate_edit(
                mirror_deliveries=mirror_deliveries,
                new_content=new_content,
                author_display_name=author_display_name,
            )

        # Send the AP Update if the message group has an AP object and community actor.
        # These are set for all Discord-originated messages published via the bridge.
        if message_group.ap_object_id and message_group.community_actor_id:
            # Resolve the actor username from the source thread's publish record.
            # The actor owns the AP object and must be the one who sends Update.
            actor_username = await resolve_actor_username(
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

        message_group = get_message_group_for_delivered_message(self.database, message_id)
        if message_group is None:
            return

        all_deliveries = self.database.get_message_deliveries(message_group.id)
        mirror_deliveries = get_outbound_delete_deliveries(all_deliveries)

        if self.discord_fanout is not None and mirror_deliveries:
            await self.discord_fanout.propagate_delete(mirror_deliveries=mirror_deliveries)

        if message_group.ap_object_id and message_group.community_actor_id:
            actor_username = await resolve_actor_username(
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

        thread_group = get_thread_group_for_ap_object(self.database, event.object.ap_id)
        if thread_group is None:
            logger.info("Post update for %s — no thread group found, skipping", event.object.ap_id)
            return _HandlerResult(status="skipped", detail="post not yet mapped")

        thread_deliveries = self.database.get_thread_deliveries(thread_group.id)
        if not thread_deliveries:
            return _HandlerResult(status="skipped", detail="no thread deliveries")

        bot = self.bot or runtime.bot
        new_content = event.object.body_markdown or ""

        await propagate_inbound_post_update(
            bot=bot, thread_deliveries=thread_deliveries, new_content=new_content
        )

        return _HandlerResult(status="processed", detail="post updated")

    async def handle_inbound_post_delete(
        self,
        event: ActivityPubEvent,
        runtime: Runtime,
    ) -> HandlerResult:
        """Handle an inbound AP Delete for a post by marking all thread starter messages deleted.

        Edits each thread's starter message to '*deleted by creator*' rather than
        deleting the thread, so the conversation history is preserved in Discord.
        Resolves thread group via ap_object_id. Returns 'skipped' if not found.
        """
        from ..activitypub_handlers import HandlerResult as _HandlerResult

        thread_group = get_thread_group_for_ap_object(self.database, event.object.ap_id)
        if thread_group is None:
            logger.info("Post delete for %s — no thread group found, skipping", event.object.ap_id)
            return _HandlerResult(status="skipped", detail="post not yet mapped")

        thread_deliveries = self.database.get_thread_deliveries(thread_group.id)

        bot = self.bot or runtime.bot

        await propagate_inbound_post_delete(bot=bot, thread_deliveries=thread_deliveries)

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

        message_group = get_message_group_for_ap_object(self.database, event.object.ap_id)
        if message_group is None:
            logger.info(
                "Comment update for %s — no message group found, skipping",
                event.object.ap_id,
            )
            return _HandlerResult(status="skipped", detail="comment not yet mapped")

        all_deliveries = self.database.get_message_deliveries(message_group.id)
        # Only edit messages the bot itself wrote: inbound (created by bot from AP)
        # and mirror (bot-owned copies in sibling channels).
        # Source messages are user-authored — editing them returns 403 Forbidden.
        deliveries = get_inbound_comment_edit_deliveries(all_deliveries)
        if not deliveries:
            return _HandlerResult(status="skipped", detail="no message deliveries")

        bot = self.bot or runtime.bot
        new_content = event.object.body_markdown or ""

        await propagate_inbound_comment_update(
            bot=bot, deliveries=deliveries, new_content=new_content
        )

        return _HandlerResult(status="processed", detail="comment updated")

    async def handle_inbound_comment_delete(
        self,
        event: ActivityPubEvent,
        runtime: Runtime,
    ) -> HandlerResult:
        """Handle an inbound AP Delete for a comment by marking all Discord messages deleted.

        Edits each delivery message to '*deleted by creator*' rather than deleting it,
        so the conversation thread structure is preserved in Discord.
        Resolves message group via ap_object_id. Returns 'skipped' if not found.
        """
        from ..activitypub_handlers import HandlerResult as _HandlerResult

        message_group = get_message_group_for_ap_object(self.database, event.object.ap_id)
        if message_group is None:
            logger.info(
                "Comment delete for %s — no message group found, skipping",
                event.object.ap_id,
            )
            return _HandlerResult(status="skipped", detail="comment not yet mapped")

        all_deliveries = self.database.get_message_deliveries(message_group.id)
        # Only edit messages the bot itself wrote — source messages are user-authored.
        deliveries = get_inbound_comment_edit_deliveries(all_deliveries)

        bot = self.bot or runtime.bot

        await propagate_inbound_comment_delete(bot=bot, deliveries=deliveries)

        return _HandlerResult(status="processed", detail="comment deleted")

def _ignored_result(reason: str) -> PublishResult:
    """Build a PublishResult for events that are intentionally skipped.

    Used when handle_discord_thread_create detects a duplicate and exits early
    without touching the AP gateway or creating any DB rows.
    """
    from ..discord_publish_service import PublishResult
    return PublishResult(status="ignored", reason=reason)
