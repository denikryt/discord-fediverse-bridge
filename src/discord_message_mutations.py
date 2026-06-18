"""Raw Discord edit/delete admission and echo suppression boundary.

This module keeps Discord platform callback details out of BridgeBot while
preserving the existing mutation routing contracts owned by DiscordEventRouter
and the downstream runtimes. It owns only volatile, process-local echo state and
partial raw-edit reconciliation at the Discord SDK boundary.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Callable, Protocol

import discord

from .db import Database
from .discord_event_router import DiscordEventRouter

logger = logging.getLogger(__name__)

ECHO_SUPPRESSION_SECONDS = 5.0


class DiscordMessageFetcher(Protocol):
    """Fetch Discord channel/message state needed by partial raw edit events.

    Implementations are expected to use the Discord client's normal cache-first
    behavior and API fallback. The protocol deliberately exposes only the two
    operations needed at this raw mutation boundary.
    """

    def get_channel(self, channel_id: int) -> object | None:
        """Return a cached Discord channel if it is already available."""

    async def fetch_channel(self, channel_id: int) -> object:
        """Fetch a Discord channel when the local cache does not contain it."""


class DiscordMessageMutationHandler:
    """Handle raw Discord edit/delete events before domain mutation routing.

    The handler owns raw-event admission, process-local echo suppression,
    partial edit reconciliation, runtime lookup, and exception containment. It
    does not decide personal/local/remote mutation semantics; admitted events are
    delegated unchanged to DiscordEventRouter.
    """

    def __init__(
        self,
        *,
        database: Database,
        event_router: DiscordEventRouter,
        discord_fetcher: DiscordMessageFetcher,
        runtime_provider: Callable[[], object | None],
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Initialise the handler with only raw-boundary collaborators.

        No secret-bearing settings, gateway clients, signing material, or broad
        Runtime object is accepted here; runtime is resolved lazily through the
        narrow provider to preserve the current Stage 3 composition behavior.
        """
        self.database = database
        self.event_router = event_router
        self.discord_fetcher = discord_fetcher
        self._runtime_provider = runtime_provider
        self._clock = clock
        # Echo state is intentionally volatile and process-local. Durable
        # mutation idempotency remains in repositories and downstream services.
        self._recent_edits: dict[int, float] = {}
        self._recent_deletes: dict[int, float] = {}

    def track_message_edit(self, message_id: int) -> None:
        """Record one bridge-originated Discord edit for raw-event suppression."""
        self._recent_edits[message_id] = self._clock()

    def track_message_delete(self, message_id: int) -> None:
        """Record one bridge-originated Discord delete for raw-event suppression."""
        self._recent_deletes[message_id] = self._clock()

    async def handle_raw_message_edit(
        self, payload: discord.RawMessageUpdateEvent
    ) -> None:
        """Admit and route a raw Discord message edit event.

        The control flow intentionally mirrors the previous BridgeBot callback:
        echo check, personal/local/remote admission, role guard, content and
        timestamp extraction, Discord fetch reconciliation, runtime lookup, then
        DiscordEventRouter delegation with exception containment.
        """
        logger.debug("[on_raw_message_edit] msg=%s", payload.message_id)

        if self._was_recent(self._recent_edits, payload.message_id):
            edit_age = self._clock() - self._recent_edits[payload.message_id]
            logger.debug(
                "[on_raw_message_edit] skipping recent edit (age=%.1fs)", edit_age
            )
            return

        delivery, is_local_message, is_personal_message = self._classify_message(
            payload.message_id
        )
        if delivery is None and not is_local_message and not is_personal_message:
            logger.debug(
                "[on_raw_message_edit] no delivery found for msg=%s",
                payload.message_id,
            )
            return

        # Only source deliveries represent user-authored remote-community edits.
        # Mirrors and inbound AP copies are mutated through their owning paths.
        if delivery is not None and delivery.role != "source":
            logger.debug(
                "[on_raw_message_edit] role=%s — skipping (not source)",
                delivery.role,
            )
            return

        new_content = payload.data.get("content", "")
        logger.debug(
            "[on_raw_message_edit] extracted content from payload: len=%d",
            len(new_content) if new_content else 0,
        )
        if not new_content:
            logger.debug(
                "[on_raw_message_edit] no content in payload for msg=%s",
                payload.message_id,
            )
            return

        edited_timestamp_raw = payload.data.get("edited_timestamp")
        edited_timestamp = None
        if isinstance(edited_timestamp_raw, str):
            edited_timestamp = datetime.fromisoformat(
                edited_timestamp_raw.replace("Z", "+00:00")
            )
        if edited_timestamp is None:
            try:
                current_message = await self._fetch_current_message(
                    channel_id=payload.channel_id,
                    message_id=payload.message_id,
                )
                edited_timestamp = getattr(current_message, "edited_at", None)
                if edited_timestamp is None:
                    return
                new_content = getattr(current_message, "content", new_content)
            except discord.HTTPException as exc:
                logger.exception(
                    "[on_raw_message_edit] failed to reconcile partial edit msg=%s",
                    payload.message_id,
                )
                self.database.content_mutations.request_discord_reconciliation(
                    channel_id=payload.channel_id,
                    message_id=payload.message_id,
                    error=str(exc),
                )
                runtime = self._runtime_provider()
                if runtime is not None:
                    runtime.user_actor_delivery_worker.wake()
                return

        # Author data is payload-local and used only for downstream mirror header
        # attribution. Missing names preserve the previous empty-string behavior.
        author_data = payload.data.get("author") or {}
        author_display_name = (
            author_data.get("global_name") or author_data.get("username") or ""
        )

        runtime = self._runtime_provider()
        if runtime is None:
            logger.warning(
                "[on_raw_message_edit] no runtime available for msg=%s",
                payload.message_id,
            )
            return

        logger.info(
            "Discord message edit msg=%s thread=%s",
            payload.message_id,
            payload.channel_id,
        )
        try:
            await self.event_router.handle_message_edit(
                message_id=payload.message_id,
                new_content=new_content,
                author_display_name=author_display_name,
                runtime=runtime,
                edited_timestamp=edited_timestamp,
                guild_id=getattr(payload, "guild_id", None),
            )
        except Exception:
            logger.exception(
                "[on_raw_message_edit] exception in handle_discord_message_edit for msg=%s",
                payload.message_id,
            )

    async def handle_raw_message_delete(
        self, payload: discord.RawMessageDeleteEvent
    ) -> None:
        """Admit and route a raw Discord message delete event.

        Deletes have no Discord ordering timestamp, so the handler preserves the
        previous terminal-intent behavior: once admitted and runtime is present,
        the event is delegated to DiscordEventRouter.
        """
        logger.debug("[on_raw_message_delete] msg=%s", payload.message_id)

        if self._was_recent(self._recent_deletes, payload.message_id):
            delete_age = self._clock() - self._recent_deletes[payload.message_id]
            logger.debug(
                "[on_raw_message_delete] skipping recent delete (age=%.1fs)",
                delete_age,
            )
            return

        delivery, is_local_message, is_personal_message = self._classify_message(
            payload.message_id
        )
        if delivery is None and not is_local_message and not is_personal_message:
            logger.debug(
                "[on_raw_message_delete] no delivery found for msg=%s",
                payload.message_id,
            )
            return

        # Inbound AP messages are deleted via AP events; raw Discord deletes are
        # only meaningful for Discord-originated source/mirror deliveries.
        if delivery is not None and delivery.role == "inbound":
            logger.debug(
                "[on_raw_message_delete] inbound message — skipping (handled via AP events)"
            )
            return

        runtime = self._runtime_provider()
        if runtime is None:
            logger.warning(
                "[on_raw_message_delete] no runtime available for msg=%s",
                payload.message_id,
            )
            return

        logger.info(
            "Discord message delete msg=%s thread=%s",
            payload.message_id,
            payload.channel_id,
        )
        try:
            await self.event_router.handle_message_delete(
                message_id=payload.message_id,
                runtime=runtime,
                guild_id=getattr(payload, "guild_id", None),
            )
        except Exception:
            logger.exception(
                "[on_raw_message_delete] exception in handle_discord_message_delete for msg=%s",
                payload.message_id,
            )

    def _classify_message(self, message_id: int) -> tuple[object | None, bool, bool]:
        """Return remote delivery and local/personal admission flags.

        The lookup order preserves the existing boundary contract: personal-feed
        mappings and local-community surfaces bypass remote fanout delivery lookup
        so those modes do not depend on remote-community rows.
        """
        personal_mapping = (
            self.database.message_mappings.get_message_mapping_by_discord_message_id(
                message_id
            )
        )
        is_personal_message = (
            personal_mapping is not None
            and personal_mapping.publication_context == "personal_feed"
        )
        is_local_message = self.event_router.is_local_community_message(message_id)
        delivery = (
            None
            if is_local_message or is_personal_message
            else self.database.discord_fanout_groups.get_message_delivery_by_message(
                message_id
            )
        )
        return delivery, is_local_message, is_personal_message

    async def _fetch_current_message(self, *, channel_id: int, message_id: int) -> object:
        """Fetch current Discord message state for partial raw edit reconciliation."""
        channel = self.discord_fetcher.get_channel(
            channel_id
        ) or await self.discord_fetcher.fetch_channel(channel_id)
        return await channel.fetch_message(message_id)

    def _was_recent(self, seen: dict[int, float], message_id: int) -> bool:
        """Return whether a message id is inside the echo suppression window."""
        now = self._clock()
        self._drop_expired(seen, now)
        timestamp = seen.get(message_id)
        return timestamp is not None and now - timestamp < ECHO_SUPPRESSION_SECONDS

    def _drop_expired(self, seen: dict[int, float], now: float) -> None:
        """Prune only echo entries older than the suppression window."""
        expired = [
            message_id
            for message_id, timestamp in seen.items()
            if now - timestamp >= ECHO_SUPPRESSION_SECONDS
        ]
        for message_id in expired:
            # Removing expired entries bounds process memory without changing the
            # 5-second suppression contract used to break mirror echo loops.
            del seen[message_id]
