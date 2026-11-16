"""Bounded Discord renderer for durable personal-feed inbound comments."""

from __future__ import annotations

import asyncio
import logging

import discord

from .runtime_ports import DiscordChannelLookupPort
from .conversation_parent_resolver import ConversationParentResolver

logger = logging.getLogger(__name__)


class PersonalFeedDiscordDeliveryWorker:
    """Own retry scheduling and Discord side effects for retained inbound comments."""

    def __init__(
        self, *, database: object, bot: DiscordChannelLookupPort, personal_feed_service: object
    ) -> None:
        """Bind durable state, Discord adapter, and active-binding resolver."""
        self.database = database
        self.bot = bot
        self.personal_feed_service = personal_feed_service
        self.parent_resolver = ConversationParentResolver(database)
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._stopping = False

    def start(self) -> None:
        """Recover interrupted claims and start one process-local scheduler."""
        if self._task is not None:
            return
        self.database.personal_feed_conversations.recover_dispatching()
        self._stopping = False
        self._task = asyncio.create_task(
            self._run(), name="personal-feed-discord-delivery"
        )

    def wake(self) -> None:
        """Wake the scheduler after canonical admission or parent arrival."""
        self._wake.set()

    async def stop(self) -> None:
        """Cancel in-flight Discord work and leave claimed rows restart-recoverable."""
        self._stopping = True
        self._wake.set()
        task = self._task
        self._task = None
        if task is None:
            return
        # Discord API awaits can stall indefinitely during network loss.  The
        # scheduler owns those awaits, so shutdown must cancel that task before
        # the bot and database it depends on are closed.
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        # A cancelled delivery may have been claimed but not attempted.  Reset
        # process-local dispatching state while the database is still open so a
        # restarted worker can resume it without consuming retry budget.
        self.database.personal_feed_conversations.recover_dispatching()

    async def _run(self) -> None:
        """Drain due rows while keeping Discord calls outside DB transactions."""
        while not self._stopping:
            claims = self.database.personal_feed_conversations.claim_due()
            if not claims:
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=5.0)
                except TimeoutError:
                    pass
                continue
            for claim in claims:
                await self._deliver(claim)

    async def _deliver(self, claim: object) -> None:
        """Create, revise, or remove one retained Discord comment."""
        root_user = self.database.users.get_user_by_actor_url(claim.root_actor_url)
        if not self._is_active(root_user, claim):
            self.database.personal_feed_conversations.suppress_delivery(
                claim.delivery_id, "inactive personal-feed binding"
            )
            return
        try:
            thread = self.bot.get_channel(claim.root_message_id)
            if claim.discord_message_id is not None:
                if thread is None:
                    thread = await self.bot.fetch_channel(claim.root_message_id)
                if not self._is_active(root_user, claim):
                    self.database.personal_feed_conversations.suppress_delivery(
                        claim.delivery_id, "inactive personal-feed binding"
                    )
                    return
                self._require_history_permission(thread)
                message = await thread.fetch_message(claim.discord_message_id)
                if claim.deleted:
                    await message.delete()
                    result_message_id = claim.discord_message_id
                else:
                    prefix = f"**{claim.actor_name or 'Remote user'}:** "
                    await message.edit(
                        content=prefix + claim.content_markdown,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    result_message_id = claim.discord_message_id
            else:
                if claim.deleted:
                    self.database.personal_feed_conversations.suppress_delivery(
                        claim.delivery_id, "deleted before Discord render"
                    )
                    return
                if thread is None:
                    channel = self.bot.get_channel(
                        claim.root_channel_id
                    ) or await self.bot.fetch_channel(claim.root_channel_id)
                    self._require_history_permission(channel)
                    root_message = await channel.fetch_message(claim.root_message_id)
                    try:
                        thread = await root_message.create_thread(
                            name=self._thread_name(claim.content_markdown)
                        )
                    except discord.HTTPException:
                        thread = self.bot.get_channel(
                            claim.root_message_id
                        ) or await self.bot.fetch_channel(claim.root_message_id)
                    self.database.personal_feed_conversations.adopt_thread(
                        root_object_id=claim.root_object_id,
                        channel_id=claim.root_channel_id,
                        root_message_id=claim.root_message_id,
                        thread_id=claim.root_message_id,
                    )
                self.database.personal_feed_conversations.set_delivery_thread(
                    claim.delivery_id, claim.root_message_id
                )
                if not self._is_active(root_user, claim):
                    self.database.personal_feed_conversations.suppress_delivery(
                        claim.delivery_id, "inactive personal-feed binding"
                    )
                    return
                self._require_history_permission(thread)
                parent_id = self.parent_resolver.resolve_remote_reply(
                    root_object_id=claim.root_object_id,
                    thread_id=claim.root_message_id,
                    parent_object_id=claim.parent_object_id,
                )
                if claim.parent_object_id not in (None, claim.root_object_id):
                    if parent_id is None:
                        # The canonical AP parent exists but is not yet a
                        # Discord message here; do not flatten the child.
                        self.database.personal_feed_conversations.defer_pending_parent(
                            claim.delivery_id
                        )
                        return
                    reference = discord.MessageReference(
                        message_id=parent_id,
                        channel_id=claim.root_message_id,
                        fail_if_not_exists=True,
                    )
                else:
                    reference = None
                prefix = f"**{claim.actor_name or 'Remote user'}:** "
                send_options = {
                    "allowed_mentions": discord.AllowedMentions.none(),
                    "suppress_embeds": True,
                }
                if reference is not None:
                    send_options["reference"] = reference
                message = await thread.send(prefix + claim.content_markdown, **send_options)
                result_message_id = int(message.id)
        except asyncio.CancelledError:
            # Cancellation is lifecycle control, not a Discord failure.  Do not
            # increment the attempt budget or convert the durable claim into an
            # error outcome; stop() recovers dispatching rows for restart.
            raise
        except Exception as exc:
            logger.warning(
                "Personal-feed Discord delivery failed object=%s", claim.object_id
            )
            self.database.personal_feed_conversations.record_discord_attempt(
                claim.delivery_id, success=False, error=str(exc)
            )
            return
        self.database.personal_feed_conversations.record_discord_attempt(
            claim.delivery_id,
            success=True,
            discord_message_id=result_message_id,
        )
        # Child work may have been waiting for this exact parent placement.
        self.database.personal_feed_conversations.wake_children(claim.object_id)

    @staticmethod
    def _require_history_permission(channel: object) -> None:
        """Reject Discord history reads/replies when guild permissions deny them.

        Runtime channels expose guild.me and permissions_for(); small test
        adapters without a guild are not real Discord permission boundaries.
        """
        guild = getattr(channel, "guild", None)
        if guild is None:
            return
        member = getattr(guild, "me", None)
        permissions_for = getattr(channel, "permissions_for", None)
        if member is None or not callable(permissions_for):
            raise PermissionError("bot channel permission context is unavailable")
        permissions = permissions_for(member)
        if not getattr(permissions, "read_message_history", False):
            raise PermissionError("bot needs Read Message History for Discord replies")

    def _is_active(self, root_user: object | None, claim: object) -> bool:
        return bool(
            root_user is not None
            and self.personal_feed_service.is_active_personal_feed_channel(
                root_user.id, claim.root_channel_id
            )
        )

    @staticmethod
    def _thread_name(content: str) -> str:
        """Build a bounded plain-text thread name from untrusted remote content."""
        normalized = " ".join(content.split()).replace("@", "＠")
        return normalized[:90] or "Federated replies"
