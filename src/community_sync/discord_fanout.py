"""Local Discord fanout for mirror thread delivery.

DiscordFanout creates mirror copies of Discord threads and messages in sibling
subscribed channels. It does not touch ActivityPub and does not decide whether
to mirror — CommunityRuntime makes that decision and calls DiscordFanout with
the already-resolved sibling targets.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

import discord

from ..bridge_policy import BridgePolicyService
from ..db import Database

if TYPE_CHECKING:
    from ..models import CommunityMessageGroupDelivery, CommunityThreadGroupDelivery
    from ..discord_bot import BridgeBot

logger = logging.getLogger(__name__)


def _valid_discord_guild_id(value: object) -> int | None:
    """Return a valid positive Discord guild id, otherwise ``None``."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


class DiscordMutationTracker(Protocol):
    """Record bridge-originated Discord mutations for raw-event deduplication."""

    def track_message_edit(self, message_id: int) -> None:
        """Record one successfully edited Discord message."""

    def track_message_delete(self, message_id: int) -> None:
        """Record one successfully deleted Discord message."""


@dataclass(slots=True)
class MirrorResult:
    """Describe one successfully created mirror thread in a sibling channel."""

    channel_id: int
    thread_id: int
    starter_message_id: int


@dataclass(slots=True)
class MirrorMessageResult:
    """Describe one successfully delivered mirror message into a sibling thread."""

    channel_id: int
    thread_id: int
    message_id: int


def _make_discord_reference(
    thread: discord.Thread, message_id: int | None
) -> discord.MessageReference | None:
    """Build a Discord MessageReference for thread.send, or None for a flat send.

    fail_if_not_exists=False prevents the send from raising if the referenced
    message was deleted between mirror creation and this send call. The message
    still arrives in the thread; Discord will just not render the reply banner.
    """
    if message_id is None:
        return None
    return discord.MessageReference(
        message_id=message_id,
        channel_id=thread.id,
        fail_if_not_exists=False,
    )


def _format_mirror_body(message: discord.Message | object, *, author_label: str | None = None) -> str:
    """Build the mirror thread body from the source starter message.

    Format:
        `{author_display_name}`

        {content}

    Uses the same backtick-quoted header format as inbound Lemmy messages so
    apply_edit_to_discord_message can preserve the attribution line on edits.
    """
    author = getattr(message, "author", None)
    display_name = author_label or getattr(author, "display_name", None) or getattr(author, "name", "unknown")
    content = getattr(message, "content", "") or ""
    return f"`{display_name}`\n\n{content}"


def _unpack_thread_result(result: object) -> tuple[object, object]:
    """Extract (thread, message) from discord.py's create_thread return value.

    discord.py returns either a (thread, message) tuple or a SimpleNamespace-like
    object with .thread and .message attributes. This helper normalises both
    shapes, matching the pattern already used in bridge_lemmy_to_discord.py.
    """
    thread = getattr(result, "thread", None)
    message = getattr(result, "message", None)
    if thread is None and isinstance(result, tuple):
        thread = result[0]
    if message is None and isinstance(result, tuple):
        message = result[1]
    if thread is None:
        raise RuntimeError("create_thread did not return a thread object")
    if message is None:
        raise RuntimeError("create_thread did not return a starter message")
    return thread, message


class DiscordFanout:
    """Deliver local Discord mirror copies of threads and messages.

    DiscordFanout does not touch AP and does not decide whether to mirror.
    CommunityRuntime makes that decision and calls DiscordFanout with the
    already-resolved sibling targets.
    """

    def __init__(
        self,
        *,
        bot: BridgeBot,
        mutation_tracker: DiscordMutationTracker,
        database: Database,
        policy_service: BridgePolicyService,
    ) -> None:
        """Initialise fanout with explicit Discord API and mutation-tracking dependencies."""
        self.bot = bot
        self.mutation_tracker = mutation_tracker
        self.database = database
        self.policy_service = policy_service

    def _channel_is_allowed(self, channel_id: int) -> bool:
        """Fail closed when a target channel cannot be tied to an allowed guild."""
        try:
            subscription = self.database.remote_subscriptions.get_subscription_by_channel(channel_id)
            if subscription is None:
                logger.warning(
                    "Skipping Discord fanout target with missing subscription channel_id=%s",
                    channel_id,
                )
                return False
            guild_id = _valid_discord_guild_id(
                getattr(subscription, "discord_guild_id", None)
            )
            if guild_id is None:
                logger.warning(
                    "Skipping Discord fanout target with invalid guild metadata channel_id=%s guild_id=%r",
                    channel_id,
                    getattr(subscription, "discord_guild_id", None),
                )
                return False
            return self.policy_service.is_discord_guild_allowed(guild_id)
        except Exception:
            logger.exception(
                "Failed to validate Discord fanout routing metadata channel_id=%s",
                channel_id,
            )
            return False

    def _author_label(self, message: object) -> str | None:
        """Resolve a registered local handle through the bot's shared services."""
        author = getattr(message, "author", None)
        database = getattr(self.bot, "database", None)
        settings = getattr(self.bot, "settings", None)
        if author is None or database is None or settings is None:
            return None
        user = database.users.get_user_by_discord_user_id(str(getattr(author, "id")))
        if user is None:
            return None
        from ..user_bans import canonical_local_user_handle
        return canonical_local_user_handle(username=str(user.activitypub_username), settings=settings)

    async def mirror_thread_to_siblings(
        self,
        *,
        source_thread: discord.Thread | object,
        source_starter_message: discord.Message | object,
        sibling_channel_ids: list[int],
    ) -> list[MirrorResult]:
        """Create one mirror thread in each sibling channel.

        Returns one MirrorResult per successfully created mirror thread.
        A sibling channel that fails does not block the others — the error is
        logged and that channel is skipped. This preserves partial-success
        behaviour so source publish is never rolled back for a mirror failure.
        """
        results: list[MirrorResult] = []
        title = getattr(source_thread, "name", "Untitled")
        body = _format_mirror_body(source_starter_message, author_label=self._author_label(source_starter_message))

        for channel_id in sibling_channel_ids:
            if not self._channel_is_allowed(channel_id):
                continue
            try:
                forum_channel = await self.bot.fetch_forum_channel(channel_id)
                result = await forum_channel.create_thread(name=title, content=body)
                thread, message = _unpack_thread_result(result)
                results.append(MirrorResult(
                    channel_id=channel_id,
                    thread_id=thread.id,
                    starter_message_id=message.id,
                ))
                logger.info(
                    "Mirrored thread %s into channel %s as thread %s",
                    getattr(source_thread, "id", "?"), channel_id, thread.id,
                )
            except Exception:
                logger.exception(
                    "Failed to mirror thread %s into channel %s",
                    getattr(source_thread, "id", "?"), channel_id,
                )

        return results

    async def mirror_message_to_siblings(
        self,
        *,
        source_message: object,
        sibling_thread_deliveries: list[CommunityThreadGroupDelivery],
        reply_context: object,
    ) -> list[MirrorMessageResult]:
        """Deliver one mirror copy of source_message into each sibling thread.

        Iterates sibling_thread_deliveries (all role='mirror' entries for the
        thread group), fetches each target thread via bot.get_thread_by_id, and
        sends the formatted mirror body with the resolved Discord reference.

        reply_context is a duck-typed object with get_reference_for_thread(thread_id)
        that returns the Discord message ID to use as a reference, or None for a
        flat send. ReplyContext from `reply_mapping.py` satisfies this contract.

        Returns one MirrorMessageResult per successfully delivered mirror. A
        sibling thread that fails does not block the others — the error is logged
        and that thread is skipped, preserving partial-success behaviour so the
        source AP publish is never rolled back.
        """
        results: list[MirrorMessageResult] = []
        content = _format_mirror_body(source_message, author_label=self._author_label(source_message))
        for delivery in sibling_thread_deliveries:
            if not self._channel_is_allowed(delivery.discord_channel_id):
                continue
            try:
                thread = await self.bot.get_thread_by_id(delivery.discord_thread_id)
                # Resolve the Discord reference for this specific mirror thread.
                # None means flat send (no reply banner); passing reference=None
                # to thread.send is identical to omitting it.
                reference = _make_discord_reference(
                    thread,
                    reply_context.get_reference_for_thread(delivery.discord_thread_id),
                )
                sent = await thread.send(content=content, reference=reference)
                results.append(MirrorMessageResult(
                    channel_id=delivery.discord_channel_id,
                    thread_id=delivery.discord_thread_id,
                    message_id=sent.id,
                ))
                logger.info(
                    "Mirrored message %s into thread %s as message %s",
                    getattr(source_message, "id", "?"),
                    delivery.discord_thread_id,
                    sent.id,
                )
            except Exception:
                logger.exception(
                    "Failed to mirror message %s into thread %s",
                    getattr(source_message, "id", "?"),
                    delivery.discord_thread_id,
                )
        return results

    async def propagate_edit(
        self,
        *,
        mirror_deliveries: list[CommunityMessageGroupDelivery],
        new_content: str,
        author_display_name: str = "",
    ) -> None:
        """Edit all mirror Discord messages concurrently with the new content.

        Uses asyncio.gather with return_exceptions=True so individual mirror
        failures are collected and logged without blocking the remaining edits.
        The caller is responsible for sending the AP Update regardless of
        whether any individual mirror edit failed.

        author_display_name is used to build a header when the mirror message
        has no existing backtick-quoted attribution line (e.g. messages created
        before the header format was introduced).
        """
        async def _edit_one(delivery: CommunityMessageGroupDelivery) -> None:
            if not self._channel_is_allowed(delivery.discord_channel_id):
                return
            try:
                from ..formatting import apply_edit_to_discord_message
                thread = await self.bot.get_thread_by_id(delivery.discord_thread_id)
                message = await thread.fetch_message(delivery.discord_message_id)
                # Preserve the author header from the current message when present.
                # Fall back to building a fresh header from author_display_name so
                # older mirror messages (created before the header format) also get
                # proper attribution after an edit.
                updated = apply_edit_to_discord_message(
                    message.content,
                    new_content,
                    fallback_header=author_display_name,
                )
                await message.edit(content=updated)
                # Record only successful mutations so raw Discord events can suppress echoes.
                self.mutation_tracker.track_message_edit(delivery.discord_message_id)
                logger.info(
                    "Edited mirror message %s in thread %s",
                    delivery.discord_message_id,
                    delivery.discord_thread_id,
                )
            except Exception:
                logger.exception(
                    "Failed to edit mirror message %s in thread %s",
                    delivery.discord_message_id,
                    delivery.discord_thread_id,
                )

        # Run all mirror edits concurrently; return_exceptions=True ensures one
        # Discord API failure does not cancel remaining concurrent edits.
        await asyncio.gather(*[_edit_one(d) for d in mirror_deliveries], return_exceptions=True)

    async def propagate_delete(
        self,
        *,
        mirror_deliveries: list[CommunityMessageGroupDelivery],
    ) -> None:
        """Delete all mirror Discord messages concurrently.

        Uses asyncio.gather with return_exceptions=True so individual mirror
        failures are collected and logged without blocking the remaining deletes.
        The caller is responsible for sending the AP Delete regardless of
        whether any individual mirror delete failed.
        """
        async def _delete_one(delivery: CommunityMessageGroupDelivery) -> None:
            if not self._channel_is_allowed(delivery.discord_channel_id):
                return
            try:
                thread = await self.bot.get_thread_by_id(delivery.discord_thread_id)
                message = await thread.fetch_message(delivery.discord_message_id)
                await message.delete()
                # Record only successful mutations so raw Discord events can suppress echoes.
                self.mutation_tracker.track_message_delete(delivery.discord_message_id)
                logger.info(
                    "Deleted mirror message %s in thread %s",
                    delivery.discord_message_id,
                    delivery.discord_thread_id,
                )
            except Exception:
                logger.exception(
                    "Failed to delete mirror message %s in thread %s",
                    delivery.discord_message_id,
                    delivery.discord_thread_id,
                )

        # Run all mirror deletes concurrently; return_exceptions=True ensures one
        # Discord API failure does not cancel remaining concurrent deletes.
        await asyncio.gather(*[_delete_one(d) for d in mirror_deliveries], return_exceptions=True)
