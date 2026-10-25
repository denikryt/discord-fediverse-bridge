"""Bounded Discord renderer for durable personal-feed inbound comments."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from .platform_types import (
    ChannelRef, ContentDocument, CreateAttempt, DeliveryPresentation, MessageRef,
    PlatformOperationError, ThreadRef,
)
from .runtime_ports import PlatformAdapterPort

logger = logging.getLogger(__name__)


class PersonalFeedDiscordDeliveryWorker:
    """Own retry scheduling and Discord side effects for retained inbound comments."""

    def __init__(
        self, *, database: object, adapter: PlatformAdapterPort, personal_feed_service: object
    ) -> None:
        """Bind canonical state, a typed physical adapter, and active binding."""
        self.database = database
        self.adapter = adapter
        self.personal_feed_service = personal_feed_service
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._stopping = False
        self._extra_drains: list[Callable[[], Awaitable[object]]] = []

    def add_drain(self, drain: Callable[[], Awaitable[object]]) -> None:
        """Register an SDK-ready durable physical recovery pass before start.

        The existing personal-feed worker is the sole Discord delivery owner;
        adding a drain does not create another task or an unjoined SDK user.
        """
        if self._task is not None:
            raise RuntimeError('cannot register community recovery after worker startup')
        self._extra_drains.append(drain)

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
        """Cancel and join Discord delivery before resetting dispatching claims.

        A second shutdown cancellation must not detach a delivery while SDK/DB
        owners are about to close. Preserve the cancellation after the task's
        async cleanup and recovery of non-terminal claims have both completed.
        """
        self._stopping = True
        self._wake.set()
        task = self._task
        if task is None:
            return
        task.cancel()
        interrupted = False
        try:
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    # A stopped worker normally ends with CancelledError, but
                    # a cancellation of *this* stop task must still join it.
                    if not task.done():
                        interrupted = True
            if not task.cancelled():
                task.result()
        finally:
            # Release the worker reference only once its physical task really
            # finished. Otherwise Runtime.close() could miss a live SDK user.
            if task.done():
                self._task = None
                self.database.personal_feed_conversations.recover_dispatching()
        if interrupted:
            raise asyncio.CancelledError

    async def _run(self) -> None:
        """Drain due rows while keeping Discord calls outside DB transactions."""
        while not self._stopping:
            # Clear before reading SQLite; a wake during either drain remains
            # visible and starts a subsequent pass instead of getting lost.
            self._wake.clear()
            claims = self.database.personal_feed_conversations.claim_due()
            for claim in claims:
                await self._deliver(claim)
            # Both types of work use the same SDK-ready lifecycle; one failed
            # target must not prevent another community's safe pending work.
            for drain in self._extra_drains:
                try:
                    await drain()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception('Durable community physical recovery failed')
            if not claims:
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=5.0)
                except TimeoutError:
                    pass

    async def _deliver(self, claim: object) -> None:
        """Apply one immutable inbound AP revision to an exact Discord receipt.

        SDK I/O and its uncertainty are owned by the adapter; the repository
        owns the durable pre-create intent, revision and retry transitions.
        """
        root_user = self.database.users.get_user_by_actor_url(claim.root_actor_url)
        if not self._is_active(root_user, claim):
            self.database.personal_feed_conversations.suppress_delivery(
                claim.delivery_id, "inactive personal-feed binding"
            )
            return
        connector = self.adapter.connector.key
        channel_ref = ChannelRef(connector, str(claim.root_channel_id))
        root_ref = MessageRef(
            connector, str(claim.root_channel_id), str(claim.root_message_id)
        )
        first_send_started = False
        try:
            if claim.discord_message_id is not None:
                # A confirmed physical ID is the only authority for later
                # edits/deletes. Never submit another Create for this object.
                message_ref = MessageRef(
                    connector, str(claim.root_message_id),
                    str(claim.discord_message_id),
                )
                await self.adapter.fetch_message(message_ref)
                if not self._is_active(root_user, claim):
                    self.database.personal_feed_conversations.suppress_delivery(
                        claim.delivery_id, "inactive personal-feed binding"
                    )
                    return
                if claim.deleted:
                    await self.adapter.delete_message(message_ref)
                else:
                    await self.adapter.edit_message(
                        message_ref, ContentDocument(
                            f"**{claim.actor_name or 'Remote user'}:** "
                            + claim.content_markdown,
                            format="portable_markdown",
                        ),
                    )
                result_message_id = claim.discord_message_id
            else:
                if claim.deleted:
                    self.database.personal_feed_conversations.suppress_delivery(
                        claim.delivery_id, "deleted before Discord render"
                    )
                    return
                # Personal Feed roots live in text/announcement channels only.
                # Adapter metadata, not a numeric Discord ID, proves its kind.
                channel_view = await self.adapter.fetch_channel(channel_ref)
                if channel_view.channel.kind not in ("text", "announcement"):
                    raise PlatformOperationError(
                        "invalid_request", "personal feed root is not a text channel"
                    )
                if not self._is_active(root_user, claim):
                    self.database.personal_feed_conversations.suppress_delivery(
                        claim.delivery_id, "inactive personal-feed binding"
                    )
                    return
                requested_thread = ThreadRef(
                    channel_view.channel, str(claim.root_message_id)
                )
                try:
                    thread_view = await self.adapter.fetch_thread(requested_thread)
                    destination = thread_view.thread
                except PlatformOperationError as exc:
                    if exc.reason != "not_found":
                        raise
                    # An ordinary absence allows a first anchored thread
                    # Create; unknown delivery never becomes permission to
                    # guess or create an unrelated forum/thread copy.
                    thread_attempt = CreateAttempt(
                        key=f"personal-feed-root-thread:{claim.root_object_id}",
                        connector_key=connector,
                        operation="reply_thread",
                        destination=root_ref,
                    )
                    destination = await self.adapter.create_reply_thread(
                        root_ref, title=self._thread_name(claim.content_markdown),
                        attempt=thread_attempt,
                    )
                if (destination.connector_key != connector
                    or destination.external_thread_id != str(claim.root_message_id)
                    or destination.channel.external_channel_id != str(claim.root_channel_id)):
                    raise PlatformOperationError(
                        "invalid_request", "root-anchored thread receipt mismatch"
                    )
                self.database.personal_feed_conversations.adopt_thread(
                    root_object_id=claim.root_object_id,
                    channel_id=claim.root_channel_id,
                    root_message_id=claim.root_message_id,
                    thread_id=int(destination.external_thread_id),
                )
                self.database.personal_feed_conversations.set_delivery_thread(
                    claim.delivery_id, int(destination.external_thread_id)
                )
                if not self._is_active(root_user, claim):
                    self.database.personal_feed_conversations.suppress_delivery(
                        claim.delivery_id, "inactive personal-feed binding"
                    )
                    return
                # MULTIPLATFORM_DISCORD_ADAPTER_TEMP_PERSONAL_FEED_SEND_GUARD: Persist possible first-send intent before Discord HTTP send; typed User Actor namespace must replace this guard with durable generic delivery intent reconciliation.
                if not self.database.personal_feed_conversations.mark_create_started(
                    claim.delivery_id
                ):
                    return
                first_send_started = True
                attempt = CreateAttempt(
                    key=f"personal-feed-comment:{claim.object_id}",
                    connector_key=connector,
                    operation="message",
                    destination=destination,
                )
                receipt = await self.adapter.send_message(
                    destination,
                    ContentDocument(
                        f"**{claim.actor_name or 'Remote user'}:** "
                        + claim.content_markdown,
                        format="portable_markdown",
                    ),
                    reply_to=None,
                    presentation=DeliveryPresentation("bot"),
                    attempt=attempt,
                )
                result_message_id = int(receipt.message.external_message_id)
        except asyncio.CancelledError:
            # Stop/cancellation cannot prove that the Discord POST failed.
            # recover_dispatching() parks a marked first Create on restart.
            raise
        except PlatformOperationError as exc:
            logger.warning(
                "Personal-feed Discord operation failed object=%s reason=%s",
                claim.object_id, exc.reason,
            )
            if first_send_started and exc.reason == "unknown_delivery":
                self.database.personal_feed_conversations.mark_uncertain_create(
                    claim.delivery_id
                )
            else:
                self.database.personal_feed_conversations.record_discord_attempt(
                    claim.delivery_id, success=False,
                    claimed_revision_fingerprint=claim.claimed_revision_fingerprint,
                    claimed_deleted=claim.deleted, error=exc.reason,
                )
            return
        except Exception:
            logger.exception(
                "Personal-feed Discord operation has an unknown result object=%s",
                claim.object_id,
            )
            if first_send_started:
                self.database.personal_feed_conversations.mark_uncertain_create(
                    claim.delivery_id
                )
            else:
                self.database.personal_feed_conversations.record_discord_attempt(
                    claim.delivery_id, success=False,
                    claimed_revision_fingerprint=claim.claimed_revision_fingerprint,
                    claimed_deleted=claim.deleted,
                    error="Discord operation failed before message Create",
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
