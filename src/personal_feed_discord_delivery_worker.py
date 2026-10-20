"""Bounded Discord renderer for durable personal-feed inbound comments."""

from __future__ import annotations

import asyncio
import logging

import discord

from .runtime_ports import DiscordChannelLookupPort

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
        first_send_started = False
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
                prefix = f"**{claim.actor_name or 'Remote user'}:** "
                # MULTIPLATFORM_STAGE03_TEMP_PERSONAL_FEED_SEND_GUARD: Persist possible first-send intent before Discord HTTP send; Stage 04 must replace this guard with durable generic delivery intent reconciliation.
                if not self.database.personal_feed_conversations.mark_create_started(
                    claim.delivery_id
                ):
                    return
                first_send_started = True
                message = await thread.send(
                    prefix + claim.content_markdown,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
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
            if first_send_started and not isinstance(exc, (discord.Forbidden, discord.NotFound)):
                # A timeout/transport error after Discord create cannot prove
                # the message was not physically committed. Keep it parked.
                self.database.personal_feed_conversations.mark_uncertain_create(
                    claim.delivery_id
                )
            else:
                self.database.personal_feed_conversations.record_discord_attempt(
                    claim.delivery_id,
                    success=False,
                    claimed_revision_fingerprint=claim.claimed_revision_fingerprint,
                    claimed_deleted=claim.deleted,
                    error=str(exc),
                )
            return
        self.database.personal_feed_conversations.record_discord_attempt(
            claim.delivery_id,
            success=True,
            claimed_revision_fingerprint=claim.claimed_revision_fingerprint,
            claimed_deleted=claim.deleted,
            discord_message_id=result_message_id,
        )

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
