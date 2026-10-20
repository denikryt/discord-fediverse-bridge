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

from ..inbound_activity_outcomes import InboundActivityOutcome
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
from .inbound_mapping import (
    get_accepted_subscriptions,
    get_parent_message_group,
    needs_backfill,
)
from .reply_mapping import (
    resolve_inbound_reference, resolve_normalized_reply_context, resolve_reply_context,
)
from ..discord_legacy_projection import discord_id_to_legacy_int
from ..platform_types import (
    ChannelRef, ContentDocument, CreateAttempt, DeliveryPresentation,
    MessageCreated, MessageRef, MessageView, PlatformOperationError, ThreadCreated, ThreadRef,
)
from ..runtime_ports import DiscordDeliveryClientPort, DiscordMutationContext, PlatformDeliveryPort

if TYPE_CHECKING:
    import discord  # Only legacy SDK-shaped annotations; typed paths need no SDK.
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

    def _authorized_normalized_source(self, *, forum_id: int, guild_id: str | None) -> object | None:
        """Authorize a physical Discord source using its current accepted forum claim.

        An untrusted DTO cannot choose a federation community or bypass an
        updated subscription/guild policy. The author is checked by the shared
        ContentPublishService at its own publication boundary.
        """
        if guild_id is None or not guild_id.isdecimal() or int(guild_id) <= 0:
            return None
        subscription = self.database.remote_subscriptions.get_subscription_by_channel(forum_id)
        if (subscription is None or subscription.status != 'accepted'
            or subscription.discord_guild_id != int(guild_id)):
            return None
        policy = self.content_publish_service.bridge_policy_service
        if (not policy.is_discord_guild_allowed(int(guild_id))
            or not policy.federation_decision(subscription.lemmy_community_actor_id).allowed):
            return None
        return subscription

    async def handle_normalized_thread_create(
        self, event: ThreadCreated, starter: MessageView, *, adapter: PlatformDeliveryPort,
    ) -> PublishResult:
        """Publish one verified remote-community forum starter and persist all receipts.

        Source/mirror ownership remains the existing CommunityThreadGroup
        schema; physical sends use only the typed adapter. The legacy SDK path
        stays unbound until the complete forum ingress cutover.
        """
        if (event.connector_key != 'discord-primary'
            or event.thread.channel.kind != 'forum'
            or event.starter is None or starter.message != event.starter
            or starter.message.external_container_id != event.thread.external_thread_id
            or event.thread.channel.external_guild_id != event.external_guild_id):
            raise PlatformOperationError('invalid_request', 'unverified Community forum starter')
        thread_id = discord_id_to_legacy_int(event.thread)
        forum_id = discord_id_to_legacy_int(event.thread, component='parent')
        discord_id_to_legacy_int(starter.message)
        subscription = self._authorized_normalized_source(
            forum_id=forum_id, guild_id=event.external_guild_id,
        )
        if subscription is None:
            return _ignored_result('no_active_subscription_or_policy')
        # The group lookup includes mirror/inbound deliveries: a duplicate
        # thread notice must not republish an existing physical mirror.
        if get_thread_group_for_any_thread(self.database, thread_id) is not None:
            return _ignored_result('duplicate_discord_thread')
        result = await self.content_publish_service.publish_normalized_post_to_community(
            event=event, starter=starter,
            community_actor_url=subscription.lemmy_community_actor_id,
            publish_call=self.content_publish_service.fedify_gateway.publish_content,
            notice_adapter=adapter,
        )
        if result.status != 'published':
            return result
        group = self.database.discord_fanout_groups.create_thread_group(
            community_actor_id=subscription.lemmy_community_actor_id,
            source_channel_id=forum_id, source_thread_id=thread_id,
            source_starter_message_id=discord_id_to_legacy_int(starter.message),
            ap_activity_id=result.activity_id, ap_object_id=result.object_id,
        )
        self.database.discord_fanout_groups.add_thread_delivery(
            thread_group_id=group.id, discord_channel_id=forum_id,
            discord_thread_id=thread_id,
            discord_starter_message_id=discord_id_to_legacy_int(starter.message),
            role='source',
        )
        if self.discord_fanout is not None:
            siblings = [row.discord_channel_id for row in
                        self.database.remote_subscriptions.list_discord_accepted_subscriptions_for_community(
                            subscription.lemmy_community_actor_id)
                        if row.discord_channel_id != forum_id]
            copies = await self.discord_fanout.mirror_normalized_thread_to_siblings(
                adapter=adapter, source=event, starter=starter,
                community_actor_id=subscription.lemmy_community_actor_id,
                sibling_channel_ids=siblings,
            )
            for copy in copies:
                # Persist confirmed physical receipts; a failed sibling cannot
                # corrupt the source group or other successfully delivered rows.
                self.database.discord_fanout_groups.add_thread_delivery(
                    thread_group_id=group.id, discord_channel_id=copy.channel_id,
                    discord_thread_id=copy.thread_id,
                    discord_starter_message_id=copy.starter_message_id,
                    role='mirror',
                )
        return result

    async def handle_normalized_message_create(
        self, event: MessageCreated, *, adapter: PlatformDeliveryPort,
    ) -> PublishResult:
        """Publish a typed forum reply with canonical ownership and per-target references.

        New authored messages in a mirrored forum are permitted; a message
        already recorded as a delivery is an echo and never republished.
        """
        thread = event.thread_context
        if (event.connector_key != 'discord-primary' or thread is None
            or event.parent_channel is None or event.parent_channel.kind != 'forum'
            or thread.channel != event.parent_channel
            or thread.channel.external_guild_id != event.external_guild_id
            or event.message.external_container_id != thread.external_thread_id):
            raise PlatformOperationError('invalid_request', 'unverified Community forum reply')
        message_id = discord_id_to_legacy_int(event.message)
        thread_id = discord_id_to_legacy_int(thread)
        forum_id = discord_id_to_legacy_int(thread, component='parent')
        subscription = self._authorized_normalized_source(
            forum_id=forum_id, guild_id=event.external_guild_id,
        )
        if subscription is None:
            return _ignored_result('no_active_subscription_or_policy')
        if get_message_group_for_delivered_message(self.database, message_id) is not None:
            return _ignored_result('duplicate_discord_message')
        group = get_thread_group_for_any_thread(self.database, thread_id)
        if group is None or group.ap_object_id is None:
            return _ignored_result('no_post_context')
        if group.community_actor_id != subscription.lemmy_community_actor_id:
            raise PlatformOperationError('invalid_request', 'source forum belongs to another community')
        physical = self.database.discord_fanout_groups.get_thread_delivery_by_thread(thread_id)
        if (physical is None or physical.thread_group_id != group.id
            or physical.discord_channel_id != forum_id):
            raise PlatformOperationError('invalid_request', 'source thread physical ownership mismatch')
        # An opening/starter message already belongs to its parent post,
        # independent of whether the source has the canonical 'source' role.
        if physical.discord_starter_message_id == message_id:
            return _ignored_result('starter_message_already_handled')
        siblings = get_sibling_thread_deliveries(
            self.database, thread_group_id=group.id, source_thread_id=thread_id,
        )
        context = resolve_normalized_reply_context(self.database, event, group, siblings)
        target = group.ap_object_id
        if context.parent_message_group_id is not None:
            parent = self.database.discord_fanout_groups.get_message_group_by_id(
                context.parent_message_group_id
            )
            if parent is not None and parent.ap_object_id is not None:
                target = parent.ap_object_id
        result = await self.content_publish_service.publish_normalized_comment_to_community(
            event=event, community_actor_url=subscription.lemmy_community_actor_id,
            parent_object_id=target,
            publish_call=self.content_publish_service.fedify_gateway.publish_content,
            notice_adapter=adapter,
        )
        if result.status != 'published':
            return result
        message_group = self.database.discord_fanout_groups.create_message_group(
            community_actor_id=group.community_actor_id, thread_group_id=group.id,
            source_channel_id=forum_id, source_thread_id=thread_id,
            source_message_id=message_id,
            ap_activity_id=result.activity_id, ap_object_id=result.object_id,
            parent_message_group_id=context.parent_message_group_id,
        )
        self.database.discord_fanout_groups.add_message_delivery(
            message_group_id=message_group.id, discord_channel_id=forum_id,
            discord_thread_id=thread_id, discord_message_id=message_id,
            role='source',
            parent_discord_message_id=(
                discord_id_to_legacy_int(event.reply_to)
                if event.reply_to is not None else None
            ),
        )
        if self.discord_fanout is not None and siblings:
            copies = await self.discord_fanout.mirror_normalized_message_to_siblings(
                adapter=adapter, source=event,
                community_actor_id=group.community_actor_id,
                sibling_thread_deliveries=siblings, reply_context=context,
            )
            for copy in copies:
                self.database.discord_fanout_groups.add_message_delivery(
                    message_group_id=message_group.id,
                    discord_channel_id=copy.channel_id,
                    discord_thread_id=copy.thread_id,
                    discord_message_id=copy.message_id,
                    role='mirror', parent_discord_message_id=copy.parent_message_id,
                )
        return result

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
        if result.status != "published":
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
        all_subs = self.database.remote_subscriptions.list_discord_accepted_subscriptions_for_community(
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
            parent_discord_message_id=getattr(
                getattr(message, "reference", None), "message_id", None
            ),
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
                    parent_discord_message_id=mirror.parent_message_id,
                )

        return result

    async def handle_normalized_inbound_post(
        self, event: ActivityPubEvent, *, adapter: PlatformDeliveryPort,
        recover_missing: bool = False,
    ) -> HandlerResult:
        """Deliver one remote AP post via the typed port, preserving shared groups.

        The existing repository remains the canonical mapping owner; each
        physical result is checked against the subscribed forum before the
        actual Discord thread and starter IDs cross the legacy integer boundary.
        A parent-post backfill may create a previously absent group, but cannot
        retry gaps in an existing group without verified negative receipts.
        """
        from ..activitypub_handlers import HandlerResult as _HandlerResult
        from ..formatting import (
            format_lemmy_post_for_discord, format_thread_title_for_discord,
            normalize_text,
        )

        existing = get_thread_group_for_ap_object(self.database, event.object.ap_id)
        if existing is not None:
            if existing.community_actor_id != event.community_actor_id:
                # An identical object URL never transfers ownership between
                # communities, including during parent-post recovery.
                return _HandlerResult(
                    status='skipped', outcome=InboundActivityOutcome.IGNORED_UNMAPPED_CONTEXT,
                    detail='parent post community mismatch',
                )
            # A missing delivery row does not prove the target Discord Create
            # was never transmitted. Even a freshly verified ActivityPub post
            # cannot supply a negative physical receipt after a restart. Until
            # local subscriber publishing can reconcile durable intents, do not re-post to gaps in
            # an already persisted group, including in the parent-backfill path.
            if recover_missing:
                return _HandlerResult(
                    status='skipped', outcome=InboundActivityOutcome.IGNORED_ALREADY_APPLIED,
                    detail='existing post has unresolved physical target receipts',
                )
            return _HandlerResult(
                status='skipped', outcome=InboundActivityOutcome.IGNORED_ALREADY_APPLIED,
                detail='post already mapped',
            )

        subscriptions = get_accepted_subscriptions(self.database, event.community_actor_id)
        if not subscriptions:
            return _HandlerResult(
                status='skipped', outcome=InboundActivityOutcome.IGNORED_NO_SUBSCRIPTION,
                detail='no subscriptions for this community',
            )

        # A historical subscription with no verified guild cannot authorize
        # a new physical copy; do not manufacture guild identity from its ID.
        allowed = [s for s in subscriptions if (
            type(s.discord_guild_id) is int and s.discord_guild_id > 0
        )]
        if not allowed:
            return _HandlerResult(
                status='skipped', outcome=InboundActivityOutcome.IGNORED_UNMAPPED_CONTEXT,
                detail='no subscriptions with verified Discord guilds',
            )

        # Only a new canonical group reaches physical creation. An existing
        # group was already handled above, regardless of recovery mode.
        group = self.database.discord_fanout_groups.create_thread_group(
            community_actor_id=event.community_actor_id,
            source_channel_id=None, source_thread_id=None,
            source_starter_message_id=None,
            ap_activity_id=event.delivery_id, ap_object_id=event.object.ap_id,
        )
        delivered_forums = {
            row.discord_channel_id
            for row in self.database.discord_fanout_groups.get_thread_deliveries(group.id)
        }
        title = format_thread_title_for_discord(event.object.title or 'Untitled Lemmy Post')
        body = ContentDocument(format_lemmy_post_for_discord(
            event.object.author_name, event.object.title or 'Untitled Lemmy Post',
            normalize_text(event.object.body_markdown), event.object.url,
            actor_id=event.actor_id,
        ))
        delivered = 0
        first_error: Exception | None = None
        for subscription in allowed:
            if subscription.discord_channel_id in delivered_forums:
                # An existing physical receipt is durable proof that this
                # destination must not receive another Create POST.
                continue
            # Follow/Undo and channel claims are mutable; recheck their current
            # mapping immediately before each physical create attempt.
            current = self.database.remote_subscriptions.get_subscription_by_channel(
                subscription.discord_channel_id
            )
            if (current is None or current.status != 'accepted'
                or current.lemmy_community_actor_id != event.community_actor_id
                or current.discord_guild_id != subscription.discord_guild_id):
                continue
            forum = ChannelRef('discord-primary', str(subscription.discord_channel_id),
                               'forum', str(subscription.discord_guild_id))
            attempt = CreateAttempt(
                key=f'inbound-ap:post:{event.object.ap_id}:discord-primary:{subscription.discord_channel_id}',
                connector_key='discord-primary', operation='forum_thread', destination=forum,
            )
            # The adapter owns ACL and the physical POST. An unknown receipt is
            # never guessed from the thread title or an arbitrary SDK message.
            try:
                created = await adapter.create_forum_thread(
                    forum, body, title=title, presentation=DeliveryPresentation(),
                    attempt=attempt,
                )
                if (created.thread.channel != forum
                    or created.thread.channel.external_guild_id != forum.external_guild_id
                    or created.starter.external_container_id != created.thread.external_thread_id
                    or created.starter.connector_key != forum.connector_key):
                    raise PlatformOperationError('unknown_delivery', 'inbound thread receipt does not match forum')
                self.database.discord_fanout_groups.add_thread_delivery(
                    thread_group_id=group.id,
                    discord_channel_id=discord_id_to_legacy_int(forum),
                    discord_thread_id=discord_id_to_legacy_int(created.thread),
                    discord_starter_message_id=discord_id_to_legacy_int(created.starter),
                    role='inbound',
                )
                delivered += 1
            except Exception as exc:
                if first_error is None:
                    first_error = exc
                # Physical Create may already have reached Discord. Never
                # infer a receipt or blindly retry, but continue to healthy
                # subscribers even during the original (non-backfill) event.
                # A later backfill must not equate a missing row with a safe retry.
                logger.exception('Remote post delivery failed for forum %s', forum.external_channel_id)

        # An entirely failed initial fan-out must not acknowledge the event as
        # delivered; retain the first concrete exception for the caller.
        if delivered == 0 and first_error is not None and not recover_missing:
            raise first_error
        logger.info('Delivered inbound post %s to %d Discord forums', event.object.ap_id, delivered)
        return _HandlerResult(
            status='processed', outcome=InboundActivityOutcome.APPLIED,
            detail='post created',
        )

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
        if (
            get_thread_group_for_ap_object(self.database, event.object.ap_id)
            is not None
        ):
            logger.info(
                "Post %s already mapped to a thread group — skipping",
                event.object.ap_id,
            )
            return _HandlerResult(
                status="skipped",
                outcome=InboundActivityOutcome.IGNORED_ALREADY_APPLIED,
                detail="post already mapped",
            )

        accepted = get_accepted_subscriptions(self.database, event.community_actor_id)
        if not accepted:
            return _HandlerResult(
                status="skipped",
                outcome=InboundActivityOutcome.IGNORED_NO_SUBSCRIPTION,
                detail="no subscriptions for this community",
            )

        # Use self.bot when available; fall back to runtime.bot for backward compat
        # with callers that pass the full Runtime (e.g. existing integration tests).
        bot = self.bot or runtime.bot
        await bot.wait_until_bridge_ready()

        # Create the canonical thread group. source_* fields are None because
        # inbound AP events have no single source Discord channel.
        # delivery_id is the closest equivalent to an activity_id for inbound events.
        thread_group = self.database.discord_fanout_groups.create_thread_group(
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
            forum_channel = await bot.fetch_forum_channel(
                subscription.discord_channel_id
            )
            thread_id, starter_message_id = await _create_inbound_discord_thread(
                forum_channel=forum_channel,
                event=event,
            )
            self.database.discord_fanout_groups.add_thread_delivery(
                thread_group_id=thread_group.id,
                discord_channel_id=subscription.discord_channel_id,
                discord_thread_id=thread_id,
                discord_starter_message_id=starter_message_id,
                role="inbound",
            )

        logger.info(
            "Delivered inbound post %s into %d subscribed channel(s)",
            event.object.ap_id,
            len(accepted),
        )
        return _HandlerResult(
            status="processed",
            outcome=InboundActivityOutcome.APPLIED,
            detail="post created",
        )

    async def handle_normalized_inbound_comment_with_backfill(
        self, event: ActivityPubEvent, *, adapter: PlatformDeliveryPort,
    ) -> HandlerResult:
        """Recover a missing AP parent post before sending the typed comment.

        A wholly missing parent is recovered before comment delivery. An
        existing group with a missing recipient receipt must not be backfilled
        blindly: confirmed target threads can receive replies independently,
        while unresolved recipients remain excluded until local subscriber publishing recovery.
        """
        from ..activitypub_handlers import HandlerResult as _HandlerResult
        from .backfill import backfill_normalized_post_as_thread_group

        # Duplicate comment events never need to fetch the parent or produce
        # another physical create, even if subscriptions later changed.
        if get_message_group_for_ap_object(self.database, event.object.ap_id) is not None:
            return _HandlerResult(
                'skipped', InboundActivityOutcome.IGNORED_ALREADY_APPLIED,
                'comment already mapped',
            )
        post_id = event.object.post_ap_id or ''
        thread_group = get_thread_group_for_ap_object(self.database, post_id)
        if thread_group is None:
            # A partial canonical group is not negative proof that its missing
            # recipient never received a parent thread. Do not refetch/create
            # those missing parents; route the child only to verified copies.
            thread_group = await backfill_normalized_post_as_thread_group(
                post_ap_id=post_id, community_actor_id=event.community_actor_id,
                delivery_id=event.delivery_id, database=self.database,
                community_runtime=self, adapter=adapter,
            )
            if thread_group is None:
                # First backfill may have persisted a successful subset before
                # failing another target. Only the persisted receipts may be
                # used for this child; never re-POST the missing parent.
                thread_group = get_thread_group_for_ap_object(self.database, post_id)
            if thread_group is None:
                return _HandlerResult(
                    'deferred', InboundActivityOutcome.DEFERRED_MISSING_DEPENDENCY,
                    'parent post could not be safely backfilled',
                )
        return await self.handle_normalized_inbound_comment(event, adapter=adapter)

    async def handle_normalized_inbound_comment(
        self, event: ActivityPubEvent, *, adapter: PlatformDeliveryPort,
    ) -> HandlerResult:
        """Deliver a mapped remote comment through neutral per-thread message sends.

        A missing canonical parent remains deferred. A partial parent group
        delivers only to confirmed, currently authorized target threads; an
        absent recipient mapping never authorizes physical parent recreation.
        """
        from ..activitypub_handlers import HandlerResult as _HandlerResult
        from ..formatting import format_lemmy_comment_for_discord, normalize_text

        if get_message_group_for_ap_object(self.database, event.object.ap_id) is not None:
            return _HandlerResult(
                'skipped', InboundActivityOutcome.IGNORED_ALREADY_APPLIED,
                'comment already mapped',
            )
        post_id = event.object.post_ap_id or ''
        thread_group = get_thread_group_for_ap_object(self.database, post_id)
        if thread_group is None:
            return _HandlerResult(
                'deferred', InboundActivityOutcome.DEFERRED_MISSING_DEPENDENCY,
                'parent post has no canonical mapping; backfill required',
            )
        if thread_group.community_actor_id != event.community_actor_id:
            return _HandlerResult(
                'skipped', InboundActivityOutcome.IGNORED_UNMAPPED_CONTEXT,
                'parent post community mismatch',
            )
        parent_group = get_parent_message_group(self.database, event.object.parent_ap_id)
        if parent_group is not None and parent_group.thread_group_id != thread_group.id:
            # Do not reinterpret a known foreign canonical comment as an
            # unknown parent and silently flatten the forged reply.
            return _HandlerResult(
                'skipped', InboundActivityOutcome.IGNORED_UNMAPPED_CONTEXT,
                'comment parent belongs to another canonical post',
            )
        # Select only stored physical parent receipts for currently accepted
        # forums. Missing recipients are not safe to backfill, and a stale or
        # revoked receipt is not an authorized destination for a new comment.
        eligible = {
            sub.discord_channel_id: sub
            for sub in get_accepted_subscriptions(self.database, event.community_actor_id)
        }
        confirmed = []
        for physical in self.database.discord_fanout_groups.get_thread_deliveries(thread_group.id):
            current = self.database.remote_subscriptions.get_subscription_by_channel(
                physical.discord_channel_id
            )
            expected = eligible.get(physical.discord_channel_id)
            if (expected is None or current is None or current.status != 'accepted'
                or current.lemmy_community_actor_id != event.community_actor_id
                or type(current.discord_guild_id) is not int or current.discord_guild_id <= 0
                or current.discord_guild_id != expected.discord_guild_id):
                continue
            confirmed.append(physical)
        if not confirmed:
            # No authorized confirmed parent: do not consume the canonical
            # comment identity or create a child in a guessed destination.
            return _HandlerResult(
                'deferred', InboundActivityOutcome.DEFERRED_MISSING_DEPENDENCY,
                'no authorized confirmed parent threads for comment',
            )
        # Record the provenance before any physical POST, as the established
        # inbound admission path does. A source mismatch cannot authorize send.
        admission = self.database.personal_feed_conversations.retain_community_create(
            activity_id=event.source_activity_id or event.delivery_id,
            object_id=event.object.ap_id,
            root_object_id=post_id,
            parent_object_id=event.object.parent_ap_id,
            actor_id=event.actor_id,
            actor_name=event.object.author_name,
            content_markdown=event.object.body_markdown or '',
            published_at=event.object.published_at,
            source_activity=event.source_activity_json,
        )
        if admission.status not in ('created', 'duplicate'):
            return _HandlerResult(
                'skipped', InboundActivityOutcome.IGNORED_UNMAPPED_CONTEXT,
                f'comment provenance: {admission.status}',
            )
        msg_group = self.database.discord_fanout_groups.create_message_group(
            community_actor_id=event.community_actor_id,
            thread_group_id=thread_group.id,
            source_channel_id=None, source_thread_id=None, source_message_id=None,
            ap_activity_id=event.delivery_id, ap_object_id=event.object.ap_id,
            parent_message_group_id=parent_group.id if parent_group is not None else None,
        )
        content = ContentDocument(format_lemmy_comment_for_discord(
            event.object.author_name, normalize_text(event.object.body_markdown),
            event.object.url, actor_id=event.actor_id,
        ))
        delivered = 0
        first_error: Exception | None = None
        for physical in confirmed:
            # Re-read subscription at the physical-send boundary so Follow
            # revocation during earlier targets cannot authorize later writes.
            current = self.database.remote_subscriptions.get_subscription_by_channel(
                physical.discord_channel_id
            )
            expected = eligible.get(physical.discord_channel_id)
            if (expected is None or current is None or current.status != 'accepted'
                or current.lemmy_community_actor_id != event.community_actor_id
                or type(current.discord_guild_id) is not int or current.discord_guild_id <= 0
                or current.discord_guild_id != expected.discord_guild_id):
                continue
            forum = ChannelRef('discord-primary', str(physical.discord_channel_id),
                               'forum', str(current.discord_guild_id))
            thread = ThreadRef(forum, str(physical.discord_thread_id))
            parent_id = None
            if parent_group is not None:
                parent_delivery = self.database.discord_fanout_groups.get_message_delivery_in_thread(
                    parent_group.id, physical.discord_thread_id,
                )
                if parent_delivery is not None:
                    parent_id = parent_delivery.discord_message_id
            reply_to = (
                MessageRef('discord-primary', str(physical.discord_thread_id), str(parent_id))
                if parent_id is not None else None
            )
            attempt = CreateAttempt(
                key=f'inbound-ap:comment:{event.object.ap_id}:discord-primary:'
                    f'{physical.discord_channel_id}:{physical.discord_thread_id}',
                connector_key='discord-primary', operation='message', destination=thread,
            )
            try:
                created = await adapter.send_message(
                    thread, content, reply_to=reply_to,
                    presentation=DeliveryPresentation(), attempt=attempt,
                )
                if (created.message.connector_key != forum.connector_key
                    or created.message.external_container_id != thread.external_thread_id):
                    raise PlatformOperationError('unknown_delivery', 'inbound comment receipt thread mismatch')
                self.database.discord_fanout_groups.add_message_delivery(
                    message_group_id=msg_group.id,
                    discord_channel_id=discord_id_to_legacy_int(forum),
                    discord_thread_id=discord_id_to_legacy_int(thread),
                    discord_message_id=discord_id_to_legacy_int(created.message),
                    role='inbound', parent_discord_message_id=parent_id,
                )
                delivered += 1
            except Exception as exc:
                if first_error is None:
                    first_error = exc
                # A missing receipt is not permission to resend an uncertain
                # Discord Create. Preserve the canonical group and deliver to
                # other eligible threads without claiming this physical copy.
                logger.exception('Remote comment delivery failed for thread %s',
                                 thread.external_thread_id)
        # Preserve the caller's failure signal if no target acknowledged a
        # physical message, without undoing any already committed receipts.
        if delivered == 0 and first_error is not None:
            raise first_error
        logger.info('Delivered inbound comment %s to %d mapped threads', event.object.ap_id, delivered)
        return _HandlerResult('processed', InboundActivityOutcome.APPLIED, 'comment created')

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
            event.object.ap_id,
            event.object.post_ap_id,
            event.object.parent_ap_id,
        )

        # Dedup: if a message group already exists for this AP object, skip.
        if (
            get_message_group_for_ap_object(self.database, event.object.ap_id)
            is not None
        ):
            logger.info("Comment %s already mapped — skipping", event.object.ap_id)
            return _HandlerResult(
                status="skipped",
                outcome=InboundActivityOutcome.IGNORED_ALREADY_APPLIED,
                detail="comment already mapped",
            )

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
                    event.object.ap_id,
                    event.object.post_ap_id,
                )
                return _HandlerResult(
                    status="deferred",
                    outcome=InboundActivityOutcome.DEFERRED_MISSING_DEPENDENCY,
                    detail="parent post not mapped and fetch failed",
                )

        if thread_group is None:
            logger.info(
                "Skipping comment %s — parent post %s is not mapped and no backfill is needed",
                event.object.ap_id,
                event.object.post_ap_id,
            )
            return _HandlerResult(
                status="skipped",
                outcome=InboundActivityOutcome.IGNORED_UNMAPPED_CONTEXT,
                detail="parent post not mapped",
            )

        logger.debug(
            "[handle_inbound_comment] thread_group=%s deliveries=%s",
            thread_group.id,
            [
                d.discord_thread_id
                for d in self.database.discord_fanout_groups.get_thread_deliveries(
                    thread_group.id
                )
            ],
        )

        self.database.personal_feed_conversations.retain_community_create(
            activity_id=event.source_activity_id or event.delivery_id,
            object_id=event.object.ap_id,
            root_object_id=event.object.post_ap_id or "",
            parent_object_id=event.object.parent_ap_id,
            actor_id=event.actor_id,
            actor_name=event.object.author_name,
            content_markdown=event.object.body_markdown or "",
            published_at=event.object.published_at,
            source_activity=event.source_activity_json,
        )

        bot = self.bot or runtime.bot
        await bot.wait_until_bridge_ready()

        # Resolve the parent message group when this is a reply to a prior comment.
        # get_message_group_by_ap_object returns None when parent_ap_id is the post
        # itself (root comment), which is the correct flat-send fallback.
        parent_group = get_parent_message_group(
            self.database, event.object.parent_ap_id
        )

        message_group = self.database.discord_fanout_groups.create_message_group(
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

        eligible = {
            s.discord_channel_id
            for s in get_accepted_subscriptions(self.database, event.community_actor_id)
        }
        # Historical copies remain queryable; their presence does not grant
        # a current subscription permission to receive a new comment.
        thread_deliveries = [
            d
            for d in self.database.discord_fanout_groups.get_thread_deliveries(
                thread_group.id
            )
            if d.discord_channel_id in eligible
        ]
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
            self.database.discord_fanout_groups.add_message_delivery(
                message_group_id=message_group.id,
                discord_channel_id=thread_delivery.discord_channel_id,
                discord_thread_id=thread_delivery.discord_thread_id,
                discord_message_id=message.id,
                role="inbound",
                parent_discord_message_id=(
                    reference.message_id if reference is not None else None
                ),
            )

        logger.info(
            "Delivered inbound comment %s into %d thread(s)",
            event.object.ap_id,
            len(thread_deliveries),
        )
        return _HandlerResult(
            status="processed",
            outcome=InboundActivityOutcome.APPLIED,
            detail="comment created",
        )

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
            user_actor_delivery_service=getattr(
                runtime, "user_actor_delivery_service", None
            ),
            user_actor_delivery_wake=getattr(
                runtime, "user_actor_delivery_worker", _NoopWake()
            ),
            fedify_gateway=getattr(runtime, "fedify_gateway", None),
        )

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
                community_actor_id=message_group.community_actor_id,
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
                mirror_deliveries=mirror_deliveries,
                community_actor_id=message_group.community_actor_id,
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

        thread_group = get_thread_group_for_ap_object(self.database, event.object.ap_id)
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

        thread_deliveries = self.database.discord_fanout_groups.get_thread_deliveries(
            thread_group.id
        )
        if not thread_deliveries:
            return _HandlerResult(
                status="skipped",
                outcome=InboundActivityOutcome.IGNORED_UNMAPPED_CONTEXT,
                detail="no thread deliveries",
            )

        adapter = getattr(runtime, 'discord_adapter', None)
        bot = None if adapter is not None else (self.bot or runtime.bot)
        new_content = event.object.body_markdown or ""

        await propagate_inbound_post_update(
            adapter=adapter,
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

        thread_group = get_thread_group_for_ap_object(self.database, event.object.ap_id)
        if thread_group is None:
            logger.info(
                "Post delete for %s — no thread group found, skipping",
                event.object.ap_id,
            )
            return _HandlerResult(
                status="skipped",
                outcome=InboundActivityOutcome.IGNORED_UNMAPPED_CONTEXT,
                detail="post not yet mapped",
            )

        thread_deliveries = self.database.discord_fanout_groups.get_thread_deliveries(
            thread_group.id
        )

        adapter = getattr(runtime, 'discord_adapter', None)
        bot = None if adapter is not None else (self.bot or runtime.bot)

        await propagate_inbound_post_delete(
            adapter=adapter,
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
            self.database, event.object.ap_id
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

        adapter = getattr(runtime, 'discord_adapter', None)
        bot = None if adapter is not None else (self.bot or runtime.bot)
        new_content = event.object.body_markdown or ""

        await propagate_inbound_comment_update(
            adapter=adapter,
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
            self.database, event.object.ap_id
        )
        if message_group is None:
            logger.info(
                "Comment delete for %s — no message group found, skipping",
                event.object.ap_id,
            )
            return _HandlerResult(
                status="skipped",
                outcome=InboundActivityOutcome.IGNORED_UNMAPPED_CONTEXT,
                detail="comment not yet mapped",
            )

        all_deliveries = self.database.discord_fanout_groups.get_message_deliveries(
            message_group.id
        )
        # Only edit messages the bot itself wrote — source messages are user-authored.
        deliveries = get_inbound_comment_edit_deliveries(all_deliveries)

        adapter = getattr(runtime, 'discord_adapter', None)
        await propagate_inbound_comment_delete(
            adapter=adapter, bot=None if adapter is not None else (self.bot or runtime.bot),
            deliveries=deliveries,
        )

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
