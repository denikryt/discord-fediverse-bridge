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
from .runtime_ports import DiscordMutationContext, PlatformAdapterPort
from .platform_types import MessageDeleted, MessageUpdated, PlatformOperationError, MessageRef
from .discord_legacy_projection import discord_id_to_legacy_int

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
    partial edit reconciliation, mutation-context lookup, and exception containment. It
    does not decide personal/local/remote mutation semantics; admitted events are
    delegated unchanged to DiscordEventRouter.
    """

    def __init__(
        self,
        *,
        database: Database,
        event_router: DiscordEventRouter,
        discord_fetcher: DiscordMessageFetcher,
        mutation_context_provider: Callable[[], DiscordMutationContext | None],
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Initialise the handler with only raw-boundary collaborators.

        No secret-bearing settings, signing material, BridgeBot, or broad Runtime
        object is accepted here; the mutation context exposes only submit, wake,
        policy, and gateway fallback capabilities used by downstream routing.
        """
        self.database = database
        self.event_router = event_router
        self.discord_fetcher = discord_fetcher
        self._mutation_context_provider = mutation_context_provider
        self._clock = clock
        # Echo state is intentionally volatile and process-local. Durable
        # mutation idempotency remains in repositories and downstream services.
        self._recent_edits: dict[int, float] = {}
        self._recent_deletes: dict[int, float] = {}

    @staticmethod
    def _validate_platform_identity(
        event: MessageUpdated | MessageDeleted, reader: PlatformAdapterPort,
    ) -> tuple[int, int, int | None]:
        """Validate a typed event at the first Discord-only integer projection."""
        # Old Discord tables still use integer snowflakes. A second connector
        # must not silently collide with them before durable delivery generalizes keys.
        if event.connector_key != reader.connector.key or event.connector_key != "discord-primary":
            raise PlatformOperationError("invalid_request", "Discord mutation connector mismatch")
        ref = event.message
        container_id = discord_id_to_legacy_int(ref, component='container')
        message_id = discord_id_to_legacy_int(ref)
        guild_id = event.external_guild_id
        if guild_id is not None and (not guild_id.isdecimal() or int(guild_id) <= 0):
            raise PlatformOperationError("invalid_request", "invalid Discord mutation guild")
        return container_id, message_id, int(guild_id) if guild_id is not None else None

    async def handle_platform_message_edit(
        self, event: MessageUpdated, reader: PlatformAdapterPort,
    ) -> None:
        """Admit a normalized edit through the existing canonical mutation router.

        A partial edit is reconciled via an adapter-owned neutral read; never
        pass a Discord SDK message through to the federation runtime.
        """
        # Community mutations are admitted by exact source identity before any
        # Discord-integer projection. Only the independent Personal Feed route
        # retains the old numeric ID boundary until its own neutral migration.
        if hasattr(self.database, "activitypub_objects"):
            await self._handle_typed_mutation(event, reader, is_delete=False)
            return
        # MULTIPLATFORM_STAGE04_TEMP_PERSONAL_FEED_DISCORD_MUTATION: Only Personal Feed retains Discord-integer mutation routing; community mutations use typed connector-scoped canonical source and copy lookups.
        channel_id, message_id, guild_id = self._validate_platform_identity(event, reader)
        if self._was_recent(self._recent_edits, message_id):
            return
        delivery, is_local, is_personal = self._classify_message(message_id)
        if delivery is None and not is_local and not is_personal:
            return
        if delivery is not None and delivery.role != "source":
            return
        content = event.content
        edited_at = event.edited_at
        # A missing content field is not an empty-string edit; a missing
        # timestamp cannot establish causal order. Read authoritative state.
        if content is None or edited_at is None:
            try:
                view = await reader.fetch_message(event.message)
            except PlatformOperationError as exc:
                # Preserve existing durable reconciliation instead of losing
                # a partial edit during transient Discord read failures.
                self.database.content_mutations.request_discord_reconciliation(
                    channel_id=channel_id, message_id=message_id, error=str(exc),
                )
                context = self._mutation_context_provider()
                if context is not None:
                    context.user_actor_delivery_wake.wake()
                return
            if view.message != event.message:
                raise PlatformOperationError("invalid_request", "Discord read returned another message")
            content, edited_at = view.content, view.edited_at
        if content is None or not content.body or edited_at is None:
            return
        context = self._mutation_context_provider()
        if context is None:
            return
        try:
            await self.event_router.handle_message_edit(
                message_id=message_id, new_content=content.body,
                author_display_name=event.author.display_name if event.author else "",
                mutation_context=context, edited_timestamp=edited_at, guild_id=guild_id,
            )
        except Exception:
            logger.exception("[platform_message_edit] failed for message %s", message_id)

    async def handle_platform_message_delete(
        self, event: MessageDeleted, reader: PlatformAdapterPort,
    ) -> None:
        """Admit ID-only normalized deletion without fetching an SDK message."""
        if hasattr(self.database, "activitypub_objects"):
            await self._handle_typed_mutation(event, reader, is_delete=True)
            return
        # MULTIPLATFORM_STAGE04_TEMP_PERSONAL_FEED_DISCORD_MUTATION: Only Personal Feed retains Discord-integer mutation routing; community mutations use typed connector-scoped canonical source and copy lookups.
        _, message_id, guild_id = self._validate_platform_identity(event, reader)
        if self._was_recent(self._recent_deletes, message_id):
            return
        delivery, is_local, is_personal = self._classify_message(message_id)
        if delivery is None and not is_local and not is_personal:
            return
        if delivery is not None and delivery.role == "inbound":
            return
        context = self._mutation_context_provider()
        if context is None:
            return
        try:
            await self.event_router.handle_message_delete(
                message_id=message_id, mutation_context=context, guild_id=guild_id,
            )
        except Exception:
            logger.exception("[platform_message_delete] failed for message %s", message_id)

    async def _handle_typed_mutation(
        self, event: MessageUpdated | MessageDeleted,
        reader: PlatformAdapterPort, *, is_delete: bool,
    ) -> None:
        """Route exact Community source refs; retain only Personal Feed legacy routing.

        Read-only source and current surface validation happen before any
        content fetch or author mutation reservation. A numeric collision in
        another thread must never fall through to the old Community router.
        """
        if event.connector_key != reader.connector.key or event.connector_key != "discord-primary":
            raise PlatformOperationError("invalid_request", "mutation connector mismatch")
        seen = self._recent_deletes if is_delete else self._recent_edits
        if self._was_recent(seen, event.message):
            return
        if event.external_guild_id is not None and not event.external_guild_id.isdecimal():
            raise PlatformOperationError("invalid_request", "invalid mutation guild")

        published = self.database.activitypub_objects.get_by_source_ref(event.message)
        if published is None:
            # An unrecognized source is not a reason to mutate a numeric-ID
            # Community delivery in another thread. Only legacy Personal Feed
            # mappings are eligible for the separate integer-owned path.
            try:
                legacy = self.database.message_mappings.get_message_mapping_by_discord_message_id(
                    int(event.message.external_message_id)
                ) if event.message.external_message_id.isdecimal() else None
            except ValueError:
                return
            if legacy is None or legacy.publication_context != "personal_feed":
                return
            await self._handle_personal_feed_mutation(event, legacy.object_id, reader, is_delete=is_delete)
            return
        if published.publication_context == "personal_feed":
            await self._handle_personal_feed_mutation(event, published.object_id, reader, is_delete=is_delete)
            return
        if published.publication_context != "community":
            return
        from .content_sync.surface_resolution import resolve_community_mutation_source

        actor = event.actor if is_delete else event.author
        if actor is not None and (actor.kind != "human" or actor.is_bridge_owned):
            return
        with self.database.session() as session:
            proof = resolve_community_mutation_source(
                session, event.message,
                external_guild_id=event.external_guild_id,
                actor_external_id=actor.external_actor_id if actor else None,
            )
        if proof is None or not self.event_router._is_guild_allowed(proof.guild_id):
            return
        context = self._mutation_context_provider()
        if context is None or context.user_actor_delivery_service is None:
            return
        try:
            if is_delete:
                outcome = await context.user_actor_delivery_service.submit_delete(
                    object_id=proof.object_id, source_message=event.message,
                )
            else:
                content = event.content
                edited_at = event.edited_at
                if content is None or edited_at is None:
                    try:
                        view = await reader.fetch_message(event.message)
                    except PlatformOperationError as exc:
                        # The durable exact-source flag is not shared by an
                        # unrelated message with the same Discord snowflake.
                        self.database.content_mutations.request_discord_reconciliation_by_source_ref(
                            source=event.message, error=str(exc),
                        )
                        context.user_actor_delivery_wake.wake()
                        return
                    if view.message != event.message:
                        raise PlatformOperationError("invalid_request", "Discord read returned another message")
                    if (view.author is None or view.author.kind != "human"
                        or view.author.is_bridge_owned):
                        return
                    with self.database.session() as session:
                        if resolve_community_mutation_source(
                            session, event.message,
                            external_guild_id=event.external_guild_id,
                            actor_external_id=view.author.external_actor_id,
                        ) is None:
                            return
                    content, edited_at = view.content, view.edited_at
                if content is None or not content.body or edited_at is None:
                    return
                outcome = await context.user_actor_delivery_service.submit_update(
                    object_id=proof.object_id,
                    title=proof.title,
                    body_markdown=content.body,
                    edited_timestamp=edited_at,
                    source_message=event.message,
                )
            if outcome == "accepted":
                context.user_actor_delivery_wake.wake()
        except PlatformOperationError:
            raise
        except Exception:
            # The author mutation must stay in its persisted desired state;
            # do not retry any physical effect through the old SDK router.
            logger.exception("[typed_community_mutation] failed for %s", event.message)

    async def _handle_personal_feed_mutation(
        self, event: MessageUpdated | MessageDeleted, object_id: str,
        reader: PlatformAdapterPort, *, is_delete: bool,
    ) -> None:
        """Reserve a Personal Feed mutation without enabling numeric Community fanout."""
        context = self._mutation_context_provider()
        if context is None or context.user_actor_delivery_service is None:
            return
        if is_delete:
            outcome = await context.user_actor_delivery_service.submit_delete(object_id=object_id)
        else:
            content, edited_at = event.content, event.edited_at
            if content is None or edited_at is None:
                try:
                    view = await reader.fetch_message(event.message)
                except PlatformOperationError:
                    return  # Personal Feed legacy reconciliation has its own worker.
                if view.message != event.message:
                    raise PlatformOperationError("invalid_request", "wrong Personal Feed message")
                content, edited_at = view.content, view.edited_at
            if content is None or edited_at is None or not content.body:
                return
            outcome = await context.user_actor_delivery_service.submit_update(
                object_id=object_id, title=None,
                body_markdown=content.body, edited_timestamp=edited_at,
            )
        if outcome == "accepted":
            context.user_actor_delivery_wake.wake()

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
                context = self._mutation_context_provider()
                if context is not None:
                    context.user_actor_delivery_wake.wake()
                return

        # Author data is payload-local and used only for downstream mirror header
        # attribution. Missing names preserve the previous empty-string behavior.
        author_data = payload.data.get("author") or {}
        author_display_name = (
            author_data.get("global_name") or author_data.get("username") or ""
        )

        context = self._mutation_context_provider()
        if context is None:
            logger.warning(
                "[on_raw_message_edit] no mutation context available for msg=%s",
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
                mutation_context=context,
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

        context = self._mutation_context_provider()
        if context is None:
            logger.warning(
                "[on_raw_message_delete] no mutation context available for msg=%s",
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
                mutation_context=context,
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
