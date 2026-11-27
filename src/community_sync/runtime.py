"""Central orchestration entry point for shared community sync.

CommunityRuntime is the single call boundary for all thread/message events in
both directions (Discord→AP and AP→Discord). Phase 2+ owns thread-group creation,
AP publish, and Discord fanout to sibling subscribed channels. Phase 5 adds
direct inbound AP delivery onto shared group tables, replacing the legacy
PostLink/CommentLink path. Phase 8 adds edit and delete propagation in both
directions, using the same delivery row tables for reverse-lookup.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord

from ..inbound_activity_outcomes import InboundActivityOutcome
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
    inbound_mutation_denial,
    canonicalize_remote_delete_kind,
    get_outbound_delete_deliveries,
    get_outbound_edit_deliveries,
    propagate_inbound_comment_delete,
    propagate_inbound_comment_update,
    propagate_inbound_post_delete,
    propagate_inbound_post_update,
    resolve_actor_username,
)
from .inbound_mapping import (
    get_accepted_subscriptions,
    get_parent_message_group,
)
from .reply_mapping import resolve_inbound_reference, resolve_reply_context
from ..runtime_ports import DiscordDeliveryClientPort, DiscordMutationContext

if TYPE_CHECKING:
    from ..activitypub_handlers import HandlerResult
    from ..activitypub_models import ActivityPubEvent
    from ..db import Database
    from ..content_publish_service import ContentPublishService, PublishResult
    from ..runtime import Runtime
    from .discord_fanout import DiscordFanout

logger = logging.getLogger(__name__)


class _NoopWake:
    """Ignore wake calls for legacy direct runtime tests without a worker."""

    def wake(self) -> None:
        """Preserve old direct-gateway test behavior."""


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
        discord_fanout: DiscordFanout | None = None,
        bot: DiscordDeliveryClientPort | None = None,
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
        self.content_publish_service = content_publish_service
        if self.content_publish_service is None:
            raise ValueError("CommunityRuntime requires a content publish service")
        self.discord_fanout = discord_fanout
        self.bot = bot

    def bind_discord_fanout(self, discord_fanout: DiscordFanout) -> None:
        """Attach the fanout adapter once after the bot/fanout cycle is complete."""
        if self.discord_fanout is not None:
            raise RuntimeError("CommunityRuntime Discord fanout is already configured")
        self.discord_fanout = discord_fanout

    def bind_discord_client(self, bot: DiscordDeliveryClientPort) -> None:
        """Attach the Discord delivery client once for inbound AP delivery paths."""
        if self.bot is not None:
            raise RuntimeError("CommunityRuntime Discord client is already configured")
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
            logger.info(
                "Thread %s already has a thread group — skipping duplicate", thread.id
            )
            return _ignored_result("duplicate_discord_thread")

        # Publish to AP via existing service; return early on non-publish outcomes.
        result = await self.content_publish_service.publish_thread_starter(
            thread=thread,
            starter_message=starter_message,
        )
        # A committed queued Create is locally accepted even when its remote
        # Group transport has not run. Keep canonical source and siblings.
        if result.status != "published" and not getattr(result, "locally_accepted", False):
            return result

        # Resolve subscription for the source channel to get community_actor_id.
        # subscription is guaranteed non-None here: publish_thread_starter already
        # validated it and returned status="published" only when it was accepted.
        subscription = self.database.remote_subscriptions.get_subscription_by_channel(
            thread.parent_id
        )

        # Create the canonical thread group for this source event.
        thread_group = self.database.discord_fanout_groups.create_thread_group(
            community_actor_id=subscription.lemmy_community_actor_id,
            source_channel_id=thread.parent_id,
            source_thread_id=thread.id,
            source_starter_message_id=starter_message.id,
            ap_activity_id=result.activity_id,
            ap_object_id=result.object_id,
        )

        # Record source delivery row for this channel.
        self.database.discord_fanout_groups.add_thread_delivery(
            thread_group_id=thread_group.id,
            discord_channel_id=thread.parent_id,
            discord_thread_id=thread.id,
            discord_starter_message_id=starter_message.id,
            role="source",
        )

        # Resolve sibling subscriptions: all accepted subscriptions for the same
        # community except the source channel.
        all_subs = self.database.remote_subscriptions.get_subscriptions_by_community(
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
                self.database.discord_fanout_groups.add_thread_delivery(
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
        ContentPublishService, creates the canonical message-group row, records
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
            logger.info(
                "Message %s already has a message group — skipping duplicate",
                message.id,
            )
            return _ignored_result("duplicate_discord_message")

        # AP publish via existing service; return early on non-publish outcomes.
        result = await self.content_publish_service.publish_thread_message(
            message=message
        )
        # A committed queued Create is locally accepted even when its remote
        # Group transport has not run. Keep canonical source and siblings.
        if result.status != "published" and not getattr(result, "locally_accepted", False):
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
        message_group = self.database.discord_fanout_groups.create_message_group(
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
        self.database.discord_fanout_groups.add_message_delivery(
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
                self.database.discord_fanout_groups.add_message_delivery(
                    message_group_id=message_group.id,
                    discord_channel_id=mirror.channel_id,
                    discord_thread_id=mirror.thread_id,
                    discord_message_id=mirror.message_id,
                    role="mirror",
                )

        return result

    async def handle_inbound_post(
        self, event: ActivityPubEvent, runtime: Runtime
    ) -> HandlerResult:
        """Reserve a verified remote root, then claim each subscribed Discord forum.

        The canonical owner is immutable and the per-forum attempt is durable
        before any Discord mutation. Only known pre-send failures may be retried.
        """
        from ..activitypub_handlers import HandlerResult as _HandlerResult
        from ..db.repositories.discord_fanout_groups import RemoteCommunityGroupConflict
        from ..bridge_lemmy_to_discord import (
            _create_inbound_discord_thread, DiscordContentPreflightError, prepare_community_create,
        )

        try:
            owner = self._inbound_owner(event)
        except ValueError:
            return _HandlerResult("skipped", InboundActivityOutcome.IGNORED_UNMAPPED_CONTEXT,
                                  "inbound post author conflict")
        accepted = get_accepted_subscriptions(self.database, event.community_actor_id)
        if not accepted:
            return _HandlerResult("skipped", InboundActivityOutcome.IGNORED_NO_SUBSCRIPTION,
                                  "no subscriptions for this community")

        repository = self.database.discord_fanout_groups
        existing = repository.get_thread_group_by_ap_object(
            event.object.ap_id, community_actor_id=event.community_actor_id
        )
        # Historical groups without a proven owner may identify known deliveries,
        # but cannot authorize additional placement from a fresh envelope.
        if existing is not None and existing.source_owner_actor_id is None:
            return _HandlerResult("skipped", InboundActivityOutcome.IGNORED_UNMAPPED_CONTEXT,
                                  "legacy remote root has no verified owner")
        try:
            thread_group = repository.get_or_create_remote_thread_group(
                community_actor_id=event.community_actor_id,
                ap_object_id=event.object.ap_id, source_owner_actor_id=owner,
                ap_activity_id=event.source_activity_id or event.delivery_id,
                first_object_json=event.object.model_dump(mode="json"),
            )
        except RemoteCommunityGroupConflict:
            return _HandlerResult("skipped", InboundActivityOutcome.IGNORED_UNMAPPED_CONTEXT,
                                  "remote post Group or original owner conflict")
        # The original Create proves provenance, while the writer-reserved
        # Group view owns the content for each specific outbound attempt.
        if thread_group.inbound_create_snapshot_json is None:
            return _HandlerResult("skipped", InboundActivityOutcome.IGNORED_UNMAPPED_CONTEXT,
                                  "remote root lacks immutable first Create content")
        bot = self.bot or runtime.bot
        delivered = 0
        for subscription in accepted:
            channel = subscription.discord_channel_id
            # The claim transaction, not this earlier candidate list, owns
            # the final binding/tenant admission decision before Discord REST.
            reservation = repository.reserve_remote_target(
                kind="post", group_id=thread_group.id, discord_channel_id=channel
            )
            if reservation.state != "reserved":
                continue
            attempt = reservation.attempt_id
            # Fetching a channel is read-only; failure is provably before create_thread.
            try:
                forum = await bot.fetch_forum_channel(channel)
            except Exception:
                repository.mark_remote_target(kind="post", group_id=thread_group.id,
                    discord_channel_id=channel, attempt_id=attempt, status="failed")
                logger.exception("Remote post Discord channel unavailable: group=%s target=%s",
                                 thread_group.id, channel)
                continue
            try:
                # Readiness is an entirely pre-REST check. A failure here is
                # provably unsent and may be retried on a later valid event.
                await bot.wait_until_bridge_ready()
            except Exception:
                repository.mark_remote_target(kind="post", group_id=thread_group.id,
                    discord_channel_id=channel, attempt_id=attempt, status="failed")
                logger.exception("Remote post Discord bot not ready: group=%s target=%s",
                                 thread_group.id, channel)
                continue
            # Media retrieval is part of read-only preflight. The same claim
            # must be fenced against the latest live view after every await.
            try:
                prepared = await prepare_community_create(repository, event=event,
                    reservation=reservation, kind="post", group_id=thread_group.id,
                    channel_id=channel, destination=forum)
            except (TypeError, ValueError):
                repository.mark_remote_target(kind="post", group_id=thread_group.id,
                    discord_channel_id=channel, attempt_id=attempt, status="failed")
                logger.warning("Invalid remote Group root before POST: group=%s target=%s",
                               thread_group.id, channel)
                continue
            if prepared is None:
                continue
            canonical_event, images = prepared
            try:
                thread_id, starter_id = await _create_inbound_discord_thread(
                    forum_channel=forum, event=canonical_event, images=images
                )
                repository.finish_remote_target(kind="post", group_id=thread_group.id,
                    discord_channel_id=channel, attempt_id=attempt,
                    discord_thread_id=thread_id, discord_object_id=starter_id)
                delivered += 1
            except DiscordContentPreflightError:
                repository.mark_remote_target(kind="post", group_id=thread_group.id,
                    discord_channel_id=channel, attempt_id=attempt, status="failed")
                logger.exception("Remote post was invalid before Discord REST: group=%s target=%s",
                                 thread_group.id, channel)
            except Exception:
                # Even if the response was lost, the remote Discord call may have
                # succeeded. Never classify this outcome as safely retryable.
                repository.mark_remote_target(kind="post", group_id=thread_group.id,
                    discord_channel_id=channel, attempt_id=attempt, status="unknown")
                logger.exception("Unresolved remote post Discord delivery: group=%s target=%s attempt=%s",
                                 thread_group.id, channel, attempt)
        if delivered:
            return _HandlerResult("processed", InboundActivityOutcome.APPLIED, "post created")
        return _HandlerResult("skipped", InboundActivityOutcome.IGNORED_ALREADY_APPLIED,
                              "no safe new post deliveries")

    @staticmethod
    def _inbound_owner(event: ActivityPubEvent) -> str:
        """Retain the original author from the trusted gateway event, not its distributor.

        CP13 handles the Group-signed cross-origin provenance verification;
        here conflicting normalized authors are rejected before canonical writes.
        """
        owner = event.object.attributed_to_actor_id or event.actor_id
        if not owner or (event.object.attributed_to_actor_id
                         and event.object.attributed_to_actor_id != event.actor_id):
            raise ValueError("inbound Group object owner is inconsistent")
        return owner

    async def handle_inbound_comment(
        self, event: ActivityPubEvent, runtime: Runtime
    ) -> HandlerResult:
        """Reserve one tenant-scoped reply and deliver only claimable thread targets.

        An unknown or foreign root/parent must never select a Discord thread in
        another Group. Historical parent fetch is deferred until the signed Group
        membership check is implemented with CP13.
        """
        from ..activitypub_handlers import HandlerResult as _HandlerResult
        from ..db.repositories.discord_fanout_groups import RemoteCommunityGroupConflict
        from ..bridge_lemmy_to_discord import (
            _send_inbound_comment, DiscordContentPreflightError, prepare_community_create,
        )

        try:
            owner = self._inbound_owner(event)
        except ValueError:
            return _HandlerResult("skipped", InboundActivityOutcome.IGNORED_UNMAPPED_CONTEXT,
                                  "inbound reply author conflict")
        repository = self.database.discord_fanout_groups
        accepted = get_accepted_subscriptions(self.database, event.community_actor_id)
        if not accepted:
            return _HandlerResult("skipped", InboundActivityOutcome.IGNORED_NO_SUBSCRIPTION,
                                  "no subscriptions for this community")
        post_ap_id = event.object.post_ap_id or ""
        thread_group = repository.get_thread_group_by_ap_object(
            post_ap_id, community_actor_id=event.community_actor_id
        )
        if thread_group is None:
            # Seeing another Group's same root ID is a contradiction, not proof
            # of membership in the current Group. Never cross that boundary.
            if repository.get_thread_group_by_ap_object(post_ap_id) is not None:
                return _HandlerResult("skipped", InboundActivityOutcome.IGNORED_UNMAPPED_CONTEXT,
                                      "reply root belongs to another Group")
            return _HandlerResult("deferred", InboundActivityOutcome.DEFERRED_MISSING_DEPENDENCY,
                                  "verified root membership required before backfill")
        # A persisted tenant-scoped root is authoritative membership evidence
        # for replies even when legacy root author provenance is incomplete.
        # Update/Delete still require a separately verified original owner.

        parent = None
        parent_ap_id = event.object.parent_ap_id
        if parent_ap_id and parent_ap_id != post_ap_id:
            parent = repository.get_message_group_by_ap_object(
                parent_ap_id, community_actor_id=event.community_actor_id
            )
            if parent is None:
                return _HandlerResult("deferred", InboundActivityOutcome.DEFERRED_MISSING_DEPENDENCY,
                                      "verified parent reply not mapped")
            if parent.thread_group_id != thread_group.id:
                return _HandlerResult("skipped", InboundActivityOutcome.IGNORED_UNMAPPED_CONTEXT,
                                      "reply parent belongs to another root")

        # A missing root Discord surface cannot be reconstructed from the reply's
        # context alone. CP13 will validate official Group outbox proof for it.
        delivered_threads = repository.get_thread_deliveries(thread_group.id)
        if not delivered_threads:
            return _HandlerResult("deferred", InboundActivityOutcome.DEFERRED_MISSING_DEPENDENCY,
                                  "no confirmed root Discord surfaces")
        existing = repository.get_message_group_by_ap_object(
            event.object.ap_id, community_actor_id=event.community_actor_id
        )
        if existing is not None and existing.source_owner_actor_id is None:
            return _HandlerResult("skipped", InboundActivityOutcome.IGNORED_UNMAPPED_CONTEXT,
                                  "legacy reply has no verified owner")
        try:
            message_group = repository.get_or_create_remote_message_group(
                community_actor_id=event.community_actor_id, ap_object_id=event.object.ap_id,
                source_owner_actor_id=owner, ap_activity_id=event.source_activity_id or event.delivery_id,
                thread_group_id=thread_group.id, parent_message_group_id=parent.id if parent else None,
                first_object_json=event.object.model_dump(mode="json"),
            )
        except RemoteCommunityGroupConflict:
            return _HandlerResult("skipped", InboundActivityOutcome.IGNORED_UNMAPPED_CONTEXT,
                                  "remote reply Group, root, parent or owner conflict")
        if message_group.inbound_create_snapshot_json is None:
            return _HandlerResult("skipped", InboundActivityOutcome.IGNORED_UNMAPPED_CONTEXT,
                                  "remote reply lacks immutable first Create content")
        bot = self.bot or runtime.bot
        delivered = 0
        allowed_channels = {s.discord_channel_id for s in accepted}
        for delivery in delivered_threads:
            channel = delivery.discord_channel_id
            # Only initially eligible root surfaces are candidates. The writer
            # transaction revalidates the current accepted Group binding.
            if channel not in allowed_channels:
                continue
            reservation = repository.reserve_remote_target(
                kind="comment", group_id=message_group.id, discord_channel_id=channel
            )
            if reservation.state != "reserved":
                continue
            attempt = reservation.attempt_id
            try:
                thread = await bot.get_thread_by_id(delivery.discord_thread_id)
            except Exception:
                repository.mark_remote_target(kind="comment", group_id=message_group.id,
                    discord_channel_id=channel, attempt_id=attempt, status="failed")
                logger.exception("Remote reply Discord thread unavailable: group=%s channel=%s",
                                 message_group.id, channel)
                continue
            try:
                # Resolving a reference and awaiting readiness do not invoke
                # Discord send. A failure is safe to retry with the same claim.
                await bot.wait_until_bridge_ready()
                reference = resolve_inbound_reference(self.database, parent, delivery)
            except Exception:
                repository.mark_remote_target(kind="comment", group_id=message_group.id,
                    discord_channel_id=channel, attempt_id=attempt, status="failed")
                logger.exception("Remote reply unavailable before Discord REST: group=%s channel=%s",
                                 message_group.id, channel)
                continue
            # A parent lookup or media download can race with a Group mutation.
            try:
                prepared = await prepare_community_create(repository, event=event,
                    reservation=reservation, kind="comment", group_id=message_group.id,
                    channel_id=channel, destination=thread)
            except (TypeError, ValueError):
                repository.mark_remote_target(kind="comment", group_id=message_group.id,
                    discord_channel_id=channel, attempt_id=attempt, status="failed")
                logger.warning("Invalid remote Group reply before POST: group=%s target=%s",
                               message_group.id, channel)
                continue
            if prepared is None:
                continue
            canonical_event, images = prepared
            try:
                message = await _send_inbound_comment(thread=thread, event=canonical_event,
                    reference=reference, images=images)
                repository.finish_remote_target(kind="comment", group_id=message_group.id,
                    discord_channel_id=channel, attempt_id=attempt,
                    discord_thread_id=delivery.discord_thread_id, discord_object_id=message.id)
                delivered += 1
            except DiscordContentPreflightError:
                repository.mark_remote_target(kind="comment", group_id=message_group.id,
                    discord_channel_id=channel, attempt_id=attempt, status="failed")
                logger.exception("Remote reply was invalid before Discord REST: group=%s channel=%s",
                                 message_group.id, channel)
            except Exception:
                repository.mark_remote_target(kind="comment", group_id=message_group.id,
                    discord_channel_id=channel, attempt_id=attempt, status="unknown")
                logger.exception("Unresolved remote reply Discord delivery: group=%s channel=%s attempt=%s",
                                 message_group.id, channel, attempt)
        if delivered:
            # A canonical reply may be an attachment point for a later AP reply
            # even when one of its Discord targets remains unresolved.
            self.database.personal_feed_conversations.retain_community_create(
                activity_id=event.source_activity_id or event.delivery_id,
                object_id=canonical_event.object.ap_id, root_object_id=canonical_event.object.post_ap_id or "",
                parent_object_id=canonical_event.object.parent_ap_id, actor_id=event.actor_id,
                actor_name=canonical_event.object.author_name,
                content_markdown=canonical_event.object.body_markdown or "",
                published_at=canonical_event.object.published_at, source_activity=event.source_activity_json,
            )
            return _HandlerResult("processed", InboundActivityOutcome.APPLIED, "comment created")
        return _HandlerResult("skipped", InboundActivityOutcome.IGNORED_ALREADY_APPLIED,
                              "no safe new reply deliveries")


    def _require_mutation_context(
        self, *, mutation_context: DiscordMutationContext | None, runtime: object | None
    ) -> DiscordMutationContext:
        """Return the narrow mutation context, adapting legacy direct tests."""
        if mutation_context is not None:
            return mutation_context
        if runtime is None:
            raise RuntimeError("Discord mutation context is not configured")
        # Production passes DiscordMutationContext directly. This adapter keeps
        # older runtime-path tests focused on behavior while call sites migrate.
        return DiscordMutationContext(
            bridge_policy_service=getattr(runtime, "bridge_policy_service"),
            user_actor_delivery_service=getattr(runtime, "user_actor_delivery_service", None),
            user_actor_delivery_wake=getattr(
                runtime, "user_actor_delivery_worker", _NoopWake()
            ),
            fedify_gateway=getattr(runtime, "fedify_gateway", None),
        )

    async def _handle_discord_source_starter_edit(
        self, *, message_id: int, new_content: str,
        edited_timestamp: object, mutation_context: DiscordMutationContext,
    ) -> bool:
        """Submit one native Community starter revision using its saved author.

        Return True when a real source starter was classified, including
        rejected edits. Never fall through to the reply publisher after a
        rejected starter; that could create a second canonical Note.
        """
        repository = self.database.discord_fanout_groups
        source = repository.get_source_starter_delivery_by_message(message_id)
        if source is None:
            return False
        group = repository.get_thread_group_by_id(source.thread_group_id)
        if (group is None or edited_timestamp is None or not group.ap_object_id
                or not group.ap_activity_id or group.source_channel_id != source.discord_channel_id
                or group.source_thread_id != source.discord_thread_id):
            return True
        subscription = self.database.remote_subscriptions.get_subscription_by_channel(
            group.source_channel_id
        )
        if (subscription is None or subscription.status != 'accepted'
                or subscription.lemmy_community_actor_id != group.community_actor_id
                or not mutation_context.bridge_policy_service.federation_decision(
                    group.community_actor_id).allowed):
            return True
        published = self.database.activitypub_objects.get_published_activity_object_by_object_id(
            group.ap_object_id
        )
        mapping = self.database.message_mappings.get_message_mapping_by_discord_message_id(
            message_id
        )
        if (published is None or published.deleted_at is not None
                or published.kind != 'post' or published.publication_context != 'community'
                or published.object_id != group.ap_object_id
                or published.activity_id != group.ap_activity_id
                or published.community_actor_url != group.community_actor_id
                or published.discord_message_id != message_id
                or mapping is None or mapping.object_id != published.object_id
                or mapping.actor_url != published.actor_url):
            return True
        author = self.database.users.get_user_by_actor_url(published.actor_url)
        if author is None or author.activitypub_username != published.actor_username:
            return True
        # The durable mutation writer orders successive edits and preserves
        # this published object's original AP actor, title and recipients.
        outcome = await mutation_context.user_actor_delivery_service.submit_update(
            object_id=published.object_id, title=published.title,
            body_markdown=new_content, edited_timestamp=edited_timestamp,
        )
        if outcome != 'accepted':
            return True
        mutation_context.user_actor_delivery_wake.wake()
        if self.discord_fanout is not None:
            mirrors = [d for d in repository.get_thread_deliveries(group.id)
                       if d.role == 'mirror']
            if mirrors:
                await self.discord_fanout.propagate_thread_starter_edit(
                    mirror_deliveries=mirrors, new_content=new_content,
                    fallback_author=published.actor_username,
                )
        return True

    async def handle_discord_message_edit(
        self,
        message_id: int,
        new_content: str,
        mutation_context: DiscordMutationContext | None = None,
        runtime: object | None = None,
        author_display_name: str = "",
        edited_timestamp=None,
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

        mutation_context = self._require_mutation_context(
            mutation_context=mutation_context, runtime=runtime
        )

        if await self._handle_discord_source_starter_edit(
            message_id=message_id, new_content=new_content,
            edited_timestamp=edited_timestamp, mutation_context=mutation_context,
        ):
            return

        # Reverse-lookup the message group by the Discord message ID.
        # Covers both source and mirror deliveries via the delivery table.
        message_group = get_message_group_for_delivered_message(
            self.database, message_id
        )
        if message_group is None:
            return

        # Mirror deliveries are the bot-owned copies that need to be updated.
        # Only role="mirror" — inbound deliveries are owned by the AP sender,
        # not the bot, so editing them would result in a 403 Forbidden.
        all_deliveries = self.database.discord_fanout_groups.get_message_deliveries(
            message_group.id
        )
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
            decision = mutation_context.bridge_policy_service.federation_decision(
                message_group.community_actor_id
            )
            if not decision.allowed:
                return
            # Resolve the actor username from the source thread's publish record.
            # The actor owns the AP object and must be the one who sends Update.
            actor_username = await resolve_actor_username(self.database, message_group)
            if actor_username:
                try:
                    published = self.database.activitypub_objects.get_published_activity_object_by_object_id(
                        message_group.ap_object_id
                    )
                    if (
                        published is None
                        or published.actor_username != actor_username
                        or edited_timestamp is None
                    ):
                        return
                    outcome = await mutation_context.user_actor_delivery_service.submit_update(
                        object_id=published.object_id,
                        title=published.title,
                        body_markdown=new_content,
                        edited_timestamp=edited_timestamp,
                    )
                    if outcome == "accepted":
                        mutation_context.user_actor_delivery_wake.wake()
                except Exception:
                    logger.exception("Failed to send message edit to AP gateway")

    async def handle_discord_message_delete(
        self,
        message_id: int,
        mutation_context: DiscordMutationContext | None = None,
        runtime: object | None = None,
    ) -> None:
        """Propagate a Discord source-message delete to all mirror messages and to AP.

        Resolves the message group by the deleted message ID, deletes all mirror
        deliveries via DiscordFanout, then sends one AP Delete to the gateway.
        Returns silently if no delivery row exists for message_id (unknown message).

        If a mirror delete fails, the error is logged and the AP Delete is still sent —
        individual mirror failures do not abort the outbound AP propagation.
        """

        mutation_context = self._require_mutation_context(
            mutation_context=mutation_context, runtime=runtime
        )
        message_group = get_message_group_for_delivered_message(
            self.database, message_id
        )
        if message_group is None:
            return

        all_deliveries = self.database.discord_fanout_groups.get_message_deliveries(
            message_group.id
        )
        mirror_deliveries = get_outbound_delete_deliveries(all_deliveries)

        if self.discord_fanout is not None and mirror_deliveries:
            await self.discord_fanout.propagate_delete(
                mirror_deliveries=mirror_deliveries
            )

        if message_group.ap_object_id and message_group.community_actor_id:
            decision = mutation_context.bridge_policy_service.federation_decision(
                message_group.community_actor_id
            )
            if not decision.allowed:
                return
            actor_username = await resolve_actor_username(self.database, message_group)
            if actor_username:
                try:
                    published = self.database.activitypub_objects.get_published_activity_object_by_object_id(
                        message_group.ap_object_id
                    )
                    if published is None or published.actor_username != actor_username:
                        return
                    outcome = await mutation_context.user_actor_delivery_service.submit_delete(
                        object_id=published.object_id
                    )
                    if outcome == "accepted":
                        mutation_context.user_actor_delivery_wake.wake()
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

        thread_group = get_thread_group_for_ap_object(self.database, event.object.ap_id, community_actor_id=event.community_actor_id)
        if thread_group is None:
            logger.info(
                "Post update for %s — no thread group found, skipping",
                event.object.ap_id,
            )
            return _HandlerResult(
                status="skipped",
                outcome=InboundActivityOutcome.IGNORED_UNMAPPED_CONTEXT,
                detail="post not yet mapped",
            )

        denial = inbound_mutation_denial(event, thread_group)
        if denial is not None:
            return _HandlerResult(
                status="skipped", outcome=InboundActivityOutcome.IGNORED_UNAUTHORIZED_MUTATION,
                detail=denial,
            )

        # A shared Group can carry the same AP ID for a root and a reply.
        # No mutation may choose one when canonical kind is ambiguous.
        if get_message_group_for_ap_object(self.database, event.object.ap_id,
                                    community_actor_id=event.community_actor_id) is not None:
            return _HandlerResult(
                status="skipped", outcome=InboundActivityOutcome.IGNORED_UNAUTHORIZED_MUTATION,
                detail="ambiguous canonical object kind in Group",
            )
        if not get_accepted_subscriptions(self.database, event.community_actor_id):
            return _HandlerResult(
                status="skipped", outcome=InboundActivityOutcome.IGNORED_NO_SUBSCRIPTION,
                detail="no accepted Group subscription for mutation",
            )

        thread_deliveries = self.database.discord_fanout_groups.get_thread_deliveries(
            thread_group.id
        )
        if not thread_deliveries:
            return _HandlerResult(
                status="skipped",
                outcome=InboundActivityOutcome.IGNORED_UNMAPPED_CONTEXT,
                detail="no thread deliveries",
            )

        bot = self.bot or runtime.bot
        new_content = event.object.body_markdown or ""

        await propagate_inbound_post_update(
            bot=bot, thread_deliveries=thread_deliveries, new_content=new_content
        )

        return _HandlerResult(
            status="processed",
            outcome=InboundActivityOutcome.APPLIED,
            detail="post updated",
        )

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

        thread_group = get_thread_group_for_ap_object(self.database, event.object.ap_id, community_actor_id=event.community_actor_id)
        if thread_group is None:
            # The gateway cannot derive NodeBB Note kind from /post/{pid}:
            # both topic starters and replies share this URL shape. Consult
            # canonical DB identity rather than treating a reply as a root.
            if get_message_group_for_ap_object(
                self.database, event.object.ap_id,
                community_actor_id=event.community_actor_id,
            ) is not None:
                canonical_event = canonicalize_remote_delete_kind(self.database, event)
                return await self.handle_inbound_comment_delete(canonical_event, runtime)
            logger.info(
                "Post delete for %s — no thread group found, skipping",
                event.object.ap_id,
            )
            return _HandlerResult(
                status="skipped",
                outcome=InboundActivityOutcome.IGNORED_UNMAPPED_CONTEXT,
                detail="post not yet mapped",
            )

        denial = inbound_mutation_denial(event, thread_group)
        if denial is not None:
            return _HandlerResult(
                status="skipped", outcome=InboundActivityOutcome.IGNORED_UNAUTHORIZED_MUTATION,
                detail=denial,
            )

        # A shared Group can carry the same AP ID for a root and a reply.
        # No mutation may choose one when canonical kind is ambiguous.
        if get_message_group_for_ap_object(self.database, event.object.ap_id,
                                    community_actor_id=event.community_actor_id) is not None:
            return _HandlerResult(
                status="skipped", outcome=InboundActivityOutcome.IGNORED_UNAUTHORIZED_MUTATION,
                detail="ambiguous canonical object kind in Group",
            )
        if not get_accepted_subscriptions(self.database, event.community_actor_id):
            return _HandlerResult(
                status="skipped", outcome=InboundActivityOutcome.IGNORED_NO_SUBSCRIPTION,
                detail="no accepted Group subscription for mutation",
            )

        thread_deliveries = self.database.discord_fanout_groups.get_thread_deliveries(
            thread_group.id
        )

        bot = self.bot or runtime.bot

        await propagate_inbound_post_delete(
            bot=bot, thread_deliveries=thread_deliveries
        )

        return _HandlerResult(
            status="processed",
            outcome=InboundActivityOutcome.APPLIED,
            detail="post deleted",
        )

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

        message_group = get_message_group_for_ap_object(
            self.database, event.object.ap_id, community_actor_id=event.community_actor_id
        )
        if message_group is None:
            logger.info(
                "Comment update for %s — no message group found, skipping",
                event.object.ap_id,
            )
            return _HandlerResult(
                status="skipped",
                outcome=InboundActivityOutcome.IGNORED_UNMAPPED_CONTEXT,
                detail="comment not yet mapped",
            )

        root = self.database.discord_fanout_groups.get_thread_group_by_id(
            message_group.thread_group_id
        )
        parent = (self.database.discord_fanout_groups.get_message_group_by_id(
            message_group.parent_message_group_id
        ) if message_group.parent_message_group_id is not None else None)
        # The canonical reply has no autonomous Discord placement; its
        # originally verified root must still exist in this Group.
        denial = ("original reply root is missing" if root is None else
                  inbound_mutation_denial(event, message_group, root=root, parent=parent))
        if denial is not None:
            return _HandlerResult(
                status="skipped", outcome=InboundActivityOutcome.IGNORED_UNAUTHORIZED_MUTATION,
                detail=denial,
            )

        # A shared Group can carry the same AP ID for a root and a reply.
        # No mutation may choose one when canonical kind is ambiguous.
        if get_thread_group_for_ap_object(self.database, event.object.ap_id,
                                    community_actor_id=event.community_actor_id) is not None:
            return _HandlerResult(
                status="skipped", outcome=InboundActivityOutcome.IGNORED_UNAUTHORIZED_MUTATION,
                detail="ambiguous canonical object kind in Group",
            )
        if not get_accepted_subscriptions(self.database, event.community_actor_id):
            return _HandlerResult(
                status="skipped", outcome=InboundActivityOutcome.IGNORED_NO_SUBSCRIPTION,
                detail="no accepted Group subscription for mutation",
            )

        all_deliveries = self.database.discord_fanout_groups.get_message_deliveries(
            message_group.id
        )
        # Only edit messages the bot itself wrote: inbound (created by bot from AP)
        # and mirror (bot-owned copies in sibling channels).
        # Source messages are user-authored — editing them returns 403 Forbidden.
        deliveries = get_inbound_comment_edit_deliveries(all_deliveries)
        if not deliveries:
            return _HandlerResult(
                status="skipped",
                outcome=InboundActivityOutcome.IGNORED_UNMAPPED_CONTEXT,
                detail="no message deliveries",
            )

        bot = self.bot or runtime.bot
        new_content = event.object.body_markdown or ""

        await propagate_inbound_comment_update(
            bot=bot, deliveries=deliveries, new_content=new_content
        )

        return _HandlerResult(
            status="processed",
            outcome=InboundActivityOutcome.APPLIED,
            detail="comment updated",
        )

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

        message_group = get_message_group_for_ap_object(
            self.database, event.object.ap_id, community_actor_id=event.community_actor_id
        )
        if message_group is None:
            if get_thread_group_for_ap_object(
                self.database, event.object.ap_id,
                community_actor_id=event.community_actor_id,
            ) is not None:
                canonical_event = canonicalize_remote_delete_kind(self.database, event)
                return await self.handle_inbound_post_delete(canonical_event, runtime)
            logger.info(
                "Comment delete for %s — no message group found, skipping",
                event.object.ap_id,
            )
            return _HandlerResult(
                status="skipped",
                outcome=InboundActivityOutcome.IGNORED_UNMAPPED_CONTEXT,
                detail="comment not yet mapped",
            )

        root = self.database.discord_fanout_groups.get_thread_group_by_id(
            message_group.thread_group_id
        )
        parent = (self.database.discord_fanout_groups.get_message_group_by_id(
            message_group.parent_message_group_id
        ) if message_group.parent_message_group_id is not None else None)
        # The canonical reply has no autonomous Discord placement; its
        # originally verified root must still exist in this Group.
        denial = ("original reply root is missing" if root is None else
                  inbound_mutation_denial(event, message_group, root=root, parent=parent))
        if denial is not None:
            return _HandlerResult(
                status="skipped", outcome=InboundActivityOutcome.IGNORED_UNAUTHORIZED_MUTATION,
                detail=denial,
            )

        # A shared Group can carry the same AP ID for a root and a reply.
        # No mutation may choose one when canonical kind is ambiguous.
        if get_thread_group_for_ap_object(self.database, event.object.ap_id,
                                    community_actor_id=event.community_actor_id) is not None:
            return _HandlerResult(
                status="skipped", outcome=InboundActivityOutcome.IGNORED_UNAUTHORIZED_MUTATION,
                detail="ambiguous canonical object kind in Group",
            )
        if not get_accepted_subscriptions(self.database, event.community_actor_id):
            return _HandlerResult(
                status="skipped", outcome=InboundActivityOutcome.IGNORED_NO_SUBSCRIPTION,
                detail="no accepted Group subscription for mutation",
            )

        all_deliveries = self.database.discord_fanout_groups.get_message_deliveries(
            message_group.id
        )
        # Only edit messages the bot itself wrote — source messages are user-authored.
        deliveries = get_inbound_comment_edit_deliveries(all_deliveries)

        bot = self.bot or runtime.bot

        await propagate_inbound_comment_delete(bot=bot, deliveries=deliveries)

        return _HandlerResult(
            status="processed",
            outcome=InboundActivityOutcome.APPLIED,
            detail="comment deleted",
        )


def _ignored_result(reason: str) -> PublishResult:
    """Build a PublishResult for events that are intentionally skipped.

    Used when handle_discord_thread_create detects a duplicate and exits early
    without touching the AP gateway or creating any DB rows.
    """
    from ..content_publish_service import PublishResult

    return PublishResult(status="ignored", reason=reason)
