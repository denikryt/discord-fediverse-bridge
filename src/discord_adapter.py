"""Discord physical transport implementing the connector-scoped Stage 02 port.

The SDK client is borrowed from BridgeBot, not owned here. Process-local create
receipts prevent concurrent duplicate POSTs but are not a durable outbox.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from functools import wraps
from typing import Awaitable, Callable

from .discord_gateway_events import DiscordGatewayNormalizer
from .runtime_ports import PlatformInboundSinkPort

from .platform_types import (
    AuthorRef, ChannelRef, ChannelView, ConnectorRef, ContentDocument, CreateAttempt,
    CreatedMessage, CreatedThread, DeliveryPresentation, MessageRef, MessageView,
    PlatformAction, PlatformCapabilities, PlatformOperationError, PermissionResult,
    ThreadRef, ThreadView, validate_create_attempt, validate_created_receipt,
    validate_message_reply,
)


async def _cancel_and_join(tasks: list[asyncio.Task]) -> None:
    """Cancel SDK/ingress tasks and join their finalizers even if drain is cancelled.

    TaskGroup may cancel the shutdown task twice. Shielding the gather prevents
    the second cancellation from turning an outstanding callback into a
    detached user of the soon-to-be-closed database or Discord client.
    The caller still observes cancellation once every callback has finished.
    """
    if not tasks:
        return
    for task in tasks:
        task.cancel()
    completion = asyncio.gather(*tasks, return_exceptions=True)
    interrupted = False
    while not completion.done():
        try:
            await asyncio.shield(completion)
        except asyncio.CancelledError:
            # Cancellation of *this* shutdown task must not cancel the join.
            interrupted = True
    if interrupted:
        raise asyncio.CancelledError


@dataclass(frozen=True, slots=True)
class DiscordReconciliationMessage:
    """SDK-free authoritative Discord state for a durable User Actor mutation."""

    content: str
    edited_at: datetime | None


@dataclass
class _CreateFlight:
    """An immutable request signature plus its one possibly executing attempt."""

    fingerprint: tuple[object, ...]
    future: asyncio.Future[object]


def _physical_identity(value: object) -> object:
    """Retain known channel metadata hidden by ChannelRef equality semantics."""
    if isinstance(value, ChannelRef):
        return (value.connector_key, value.external_channel_id, value.kind,
                value.external_guild_id)
    if isinstance(value, ThreadRef):
        return (_physical_identity(value.channel), value.external_thread_id)
    if isinstance(value, MessageRef):
        return (value.connector_key, value.external_container_id,
                value.external_message_id)
    return value


def _tracked_io(operation: Callable) -> Callable:
    """Register one outer SDK operation before its first await, including reads.

    Nested port calls use the outer task's existing admission; quiesce can
    cancel and join every SDK user before its borrowed client is destroyed.
    """
    @wraps(operation)
    async def call(self, *args, **kwargs):
        """Keep SDK I/O task ownership until all awaited child calls complete."""
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError('Discord SDK I/O requires an asyncio Task')
        nested = task in self._io_tasks
        if not nested:
            if not self._io_admission_open:
                raise PlatformOperationError('unavailable', 'adapter SDK I/O is closing')
            self._io_tasks.add(task)
        try:
            return await operation(self, *args, **kwargs)
        finally:
            if not nested:
                self._io_tasks.discard(task)

    return call


class DiscordAdapter:
    """Own all physical Discord reads/writes, with no canonical federation state."""

    def __init__(self, *, connector: ConnectorRef, bot: object,
                 max_create_attempts: int = 100_000) -> None:
        """Borrow a Discord client and retain attempts for the process lifetime."""
        if connector.platform != 'discord' or not 0 < max_create_attempts:
            raise ValueError('DiscordAdapter requires a Discord connector and a positive capacity')
        self._connector = connector
        self._bot = bot
        self._max_create_attempts = max_create_attempts
        self._create_lock = asyncio.Lock()
        self._creates: dict[tuple[str, str], _CreateFlight] = {}
        self._io_admission_open = True
        self._io_tasks: set[asyncio.Task] = set()
        self._ingress_admission_open = True
        self._ingress_tasks: set[asyncio.Task] = set()
        self._sink: PlatformInboundSinkPort | None = None

    def bind_ingress_sink(self, sink: PlatformInboundSinkPort) -> None:
        """Bind the only normalized event consumer before Gateway startup."""
        if self._sink is not None or not isinstance(sink, PlatformInboundSinkPort):
            raise RuntimeError('Discord normalized ingress is already bound or invalid')
        self._sink = sink

    def _normalizer(self) -> DiscordGatewayNormalizer:
        """Read the SDK's currently authenticated bot identity for provenance."""
        actor = getattr(getattr(self._bot, 'user', None), 'id', None)
        return DiscordGatewayNormalizer(
            self.connector.key, owned_bot_id=str(actor) if actor is not None else None,
        )

    async def ingest_message_created(self, message: object) -> None:
        """Admit a verified text/announcement root through one normalized sink.

        A genuine parentless Discord root must have an observed channel kind;
        a cached thread with missing parent is never guessed to be a root.
        """
        if self._sink is None:
            raise RuntimeError('Discord normalized ingress is not bound')
        # Root publication currently supports text only; dropping attached
        # media on conversion would silently change the author's payload.
        if getattr(message, 'attachments', ()):
            raise PlatformOperationError('unsupported', 'Discord root attachments require supported transport')
        physical = getattr(message, 'channel', None)
        channel = ChannelRef(self.connector.key, str(getattr(physical, 'id', '')))
        verified = self._validate_channel(channel, physical)
        if verified.kind not in ('text', 'announcement'):
            raise PlatformOperationError('invalid_request', 'not a Discord root text channel')
        event = self._normalizer().message_created(message, channel_kind=verified.kind)
        if event is not None:
            await self._sink.handle_platform_event(event)

    async def ingest_thread_message_created(self, message: object) -> None:
        """Admit a message inside a verified physical thread without SDK leaks.

        The domain router decides whether this container belongs to a Personal
        Feed or a community. The adapter must not infer parent, guild, starter
        identity, or silently discard attachments during normalization.
        """
        if self._sink is None:
            raise RuntimeError('Discord normalized ingress is not bound')
        if getattr(message, 'attachments', ()):
            raise PlatformOperationError(
                'unsupported', 'Discord thread message attachments require supported transport',
            )
        container = getattr(message, 'channel', None)
        if not isinstance(container, self._sdk().Thread):
            raise PlatformOperationError('invalid_request', 'expected Discord thread container')
        parent = getattr(container, 'parent', None)
        parent_id = getattr(container, 'parent_id', None)
        message_guild_id = getattr(getattr(message, 'guild', None), 'id', None)
        if parent_id is None:
            raise PlatformOperationError('invalid_request', 'thread parent not verified')
        if parent is None:
            # Discord may omit Thread.parent when the channel cache is cold.
            # Resolve the exact parent through the same verified physical
            # adapter lookup; never silently treat this reply as a root.
            parent = await self._get_sdk_channel(ChannelRef(
                self.connector.key, str(parent_id),
                external_guild_id=(str(message_guild_id) if message_guild_id is not None else None),
            ))
        if str(getattr(parent, 'id', '')) != str(parent_id):
            raise PlatformOperationError('invalid_request', 'thread parent not verified')
        # All three independently observed guild references must agree. A
        # mismatched cached parent cannot authorize community publication.
        observed_guilds = (
            getattr(getattr(message, 'guild', None), 'id', None),
            getattr(getattr(container, 'guild', None), 'id', None),
            getattr(getattr(parent, 'guild', None), 'id', None),
        )
        if any(value is None for value in observed_guilds) or len(set(map(str, observed_guilds))) != 1:
            raise PlatformOperationError('invalid_request', 'thread message guild not verified')
        verified = self._validate_channel(
            ChannelRef(self.connector.key, str(parent_id), external_guild_id=str(observed_guilds[0])),
            parent,
        )
        event = self._normalizer().message_created(
            message, channel_kind='unknown', parent_kind=verified.kind,
            verified_parent=parent,
        )
        if event is not None:
            await self._sink.handle_platform_event(event)

    async def ingest_thread_created(self, thread: object) -> None:
        """Normalize one verified thread and publish its actual forum starter.

        The observed starter must be in the exact physical thread. Discord may
        not cache it; only fetch the exact root ID, never an arbitrary history
        message whose author and content could belong to a later reply.
        """
        if self._sink is None:
            raise RuntimeError('Discord normalized ingress is not bound')
        if not isinstance(thread, self._sdk().Thread):
            raise PlatformOperationError('invalid_request', 'expected Discord thread')
        parent = getattr(thread, 'parent', None)
        parent_id = getattr(thread, 'parent_id', None)
        if parent_id is None:
            raise PlatformOperationError('invalid_request', 'thread parent not verified')
        guild = getattr(getattr(thread, 'guild', None), 'id', None)
        if parent is None:
            # Cold-cache thread parents must be fetched by exact ID and guild.
            parent = await self._get_sdk_channel(ChannelRef(
                self.connector.key, str(parent_id),
                external_guild_id=(str(guild) if guild is not None else None),
            ))
        if str(getattr(parent, 'id', '')) != str(parent_id):
            raise PlatformOperationError('invalid_request', 'thread parent not verified')
        parent_guild = getattr(getattr(parent, 'guild', None), 'id', None)
        if guild is None or parent_guild is None or str(guild) != str(parent_guild):
            raise PlatformOperationError('invalid_request', 'thread guild not verified')
        verified = self._validate_channel(
            ChannelRef(self.connector.key, str(parent_id), external_guild_id=str(guild)),
            parent,
        )
        starter = None
        if verified.kind == 'forum':
            starter = await self._fetch_exact_forum_starter(thread)
            if starter is None:
                return  # Missing precise starter evidence; allow a later replay.
            if (str(getattr(getattr(starter, 'channel', None), 'id', '')) != str(thread.id)
                or not str(getattr(starter, 'id', '')).isdecimal()):
                raise PlatformOperationError('invalid_request', 'forum starter identity mismatch')
            if getattr(starter, 'attachments', ()):
                raise PlatformOperationError('unsupported', 'forum starter attachments are unsupported')
            author = self._normalizer()._author(
                getattr(starter, 'author', None), getattr(starter, 'webhook_id', None),
            )
            if author.kind != 'human':
                return  # Bot/webhook starter is not a new canonical post.
        event = self._normalizer().thread_created(
            thread, parent_kind=verified.kind, verified_parent=parent,
            verified_starter=starter,
        )
        if event is not None:
            await self._sink.handle_platform_event(event)

    @_tracked_io
    async def _fetch_exact_forum_starter(self, thread: object) -> object | None:
        """Resolve an observed starter ID with SDK admission; never scan history.

        Discord may supply a cached starter with an ID different from thread.id.
        When the cache is empty, only an exact thread-ID message may be tried.
        Failure to find that message is not proof that another message is root.
        """
        cached = getattr(thread, 'starter_message', None)
        if cached is not None:
            return cached
        try:
            return await thread.fetch_message(thread.id)
        except Exception as exc:
            failure = self._read_error(exc)
            if failure.reason == 'not_found':
                return None
            raise failure from exc

    async def ingest_raw_message_edit(self, payload: object) -> None:
        """Normalize one raw edit and deliver it exactly once to typed ingress."""
        if self._sink is None:
            raise RuntimeError('Discord normalized ingress is not bound')
        await self._sink.handle_platform_event(self._normalizer().raw_message_updated(payload))

    async def ingest_raw_message_delete(self, payload: object) -> None:
        """Normalize one cache-independent delete without a physical lookup."""
        if self._sink is None:
            raise RuntimeError('Discord normalized ingress is not bound')
        await self._sink.handle_platform_event(self._normalizer().raw_message_deleted(payload))

    async def ingest_raw_thread_update(self, payload: object) -> None:
        """Report one raw thread metadata change, without canonical AP mutation."""
        if self._sink is None:
            raise RuntimeError('Discord normalized ingress is not bound')
        await self._sink.handle_platform_event(self._normalizer().raw_thread_updated(payload))

    async def ingest_raw_thread_delete(self, payload: object) -> None:
        """Report a raw thread removal without assuming it deletes an AP post."""
        if self._sink is None:
            raise RuntimeError('Discord normalized ingress is not bound')
        await self._sink.handle_platform_event(self._normalizer().raw_thread_deleted(payload))

    @property
    def connector(self) -> ConnectorRef:
        """Return the immutable physical connector, not an ActivityPub identity."""
        return self._connector

    def _check_connector(self, connector_key: str) -> None:
        """Reject cross-connector identity before looking up any SDK object."""
        if connector_key != self.connector.key:
            raise PlatformOperationError('invalid_request', 'wrong physical connector')

    @staticmethod
    def _validate_outbound_content(content: ContentDocument) -> None:
        """Reject uploads and invalid message sizes without silently dropping data."""
        if not isinstance(content, ContentDocument):
            raise PlatformOperationError('invalid_request', 'expected neutral content')
        if content.attachments:
            # MULTIPLATFORM_STAGE03_TEMP_ATTACHMENTS: Preserve an explicit refusal for unsupported attachment delivery; remove only when verified Discord upload/download and size/SSRF policy exists at the adapter boundary.
            raise PlatformOperationError('unsupported', 'Discord attachment transport is not verified')
        if not content.body:
            raise PlatformOperationError('invalid_request', 'empty Discord message body')
        if len(content.body) > 2000:
            raise PlatformOperationError('invalid_request', 'Discord message exceeds limit')

    @staticmethod
    def _validate_presentation(presentation: DeliveryPresentation) -> None:
        """Reject a presentation mode this adapter cannot physically honour."""
        if not isinstance(presentation, DeliveryPresentation):
            raise PlatformOperationError('invalid_request', 'invalid presentation')
        if presentation.mode == 'webhook':
            # MULTIPLATFORM_STAGE03_TEMP_NO_WEBHOOK: Discord physical delivery is bot-only until a verified webhook send/edit/delete implementation can preserve explicit requested delivery mode without downgrade.
            raise PlatformOperationError('unsupported', 'Discord webhook send not implemented')

    async def _begin_create_attempt(
        self, attempt: CreateAttempt, *, operation: str,
        destination: ChannelRef | ThreadRef | MessageRef,
        content: ContentDocument | None, presentation: DeliveryPresentation | None,
        title: str | None, reply_to: MessageRef | None,
        send: Callable[[], Awaitable[CreatedMessage | CreatedThread | ThreadRef]],
    ) -> CreatedMessage | CreatedThread | ThreadRef:
        """Share one physical POST and its verified/ambiguous outcome by immutable key.

        Owner cancellation leaves an unknown outcome; a waiting caller can be
        cancelled without cancelling the owner or discarding its receipt.
        """
        validate_create_attempt(attempt, operation, destination)
        self._check_connector(attempt.connector_key)
        signature = (attempt.connector_key, operation, _physical_identity(destination),
                     title, content, _physical_identity(reply_to), presentation)
        key = (attempt.connector_key, attempt.key)
        async with self._create_lock:
            existing = self._creates.get(key)
            if existing is not None:
                if existing.fingerprint != signature:
                    raise PlatformOperationError('invalid_request', 'create attempt payload changed')
                owner = False
                flight = existing
            else:
                if not self._io_admission_open:
                    raise PlatformOperationError('unavailable', 'adapter is closing')
                if len(self._creates) >= self._max_create_attempts:
                    # MULTIPLATFORM_STAGE03_TEMP_CREATE_ATTEMPT_CAPACITY: Fail closed when process-local verified or unknown create receipts fill the bounded registry; Stage 04 must replace this with durable keyed intent and receipt lookup before permitting more creates.
                    raise PlatformOperationError('unavailable', 'create receipt registry full')
                flight = _CreateFlight(signature, asyncio.get_running_loop().create_future())
                self._creates[key] = flight
                owner = True
        if not owner:
            outcome = await asyncio.shield(flight.future)
            if isinstance(outcome, PlatformOperationError):
                raise outcome
            return outcome
        try:
            receipt = await send()
        except asyncio.CancelledError:
            # The SDK POST may already have committed. Never interpret task
            # cancellation as proof that Discord did not create the copy.
            flight.future.set_result(PlatformOperationError('unknown_delivery'))
            raise
        except PlatformOperationError as exc:
            if exc.reason in ('invalid_request', 'forbidden', 'not_found', 'unsupported', 'rate_limited'):
                # Only explicit pre-send failures may be retried with this key.
                # Physical send helpers map ambiguous HTTP failures to unknown.
                async with self._create_lock:
                    self._creates.pop(key, None)
            flight.future.set_result(exc)
            raise
        except Exception as exc:
            unknown = PlatformOperationError('unknown_delivery', str(exc))
            flight.future.set_result(unknown)
            raise unknown from exc
        # The physical call already returned. Any failure to validate its
        # receipt is post-send uncertainty, NOT a proven pre-send rejection.
        # In particular an `invalid_request` raised by the receipt validator
        # must never remove this key and permit another physical POST.
        try:
            validate_created_receipt(attempt, receipt, presentation=presentation)
        except Exception as exc:
            unknown = PlatformOperationError('unknown_delivery', 'Discord create receipt could not be verified')
            flight.future.set_result(unknown)
            raise unknown from exc
        else:
            flight.future.set_result(receipt)
            return receipt

    @_tracked_io
    async def reconcile_create(
        self, attempt: CreateAttempt,
    ) -> CreatedThread | ThreadRef | CreatedMessage | None:
        """Return only receipts proven by this process; never perform a POST."""
        self._check_connector(attempt.connector_key)
        async with self._create_lock:
            flight = self._creates.get((attempt.connector_key, attempt.key))
        if flight is not None:
            if not flight.future.done():
                return None
            outcome = flight.future.result()
            if not isinstance(outcome, PlatformOperationError):
                return outcome
        # MULTIPLATFORM_STAGE03_TEMP_UNKNOWN_CREATE: Discord has no verified durable receipt lookup for these attempts; return unresolved without replay until Stage 04 owns durable intents and receipt reconciliation.
        return None

    @staticmethod
    def _sdk() -> object:
        """Import discord.py solely at the transport boundary, not in core."""
        import discord  # pylint: disable=import-outside-toplevel
        return discord

    @staticmethod
    def _id(value: str) -> int:
        """Resolve the Discord-only snowflake representation without guessing IDs."""
        if not value.isdecimal() or int(value) <= 0:
            raise PlatformOperationError('invalid_request', 'invalid Discord snowflake')
        return int(value)

    @staticmethod
    def _read_error(exc: Exception) -> PlatformOperationError:
        """Translate SDK read exceptions without leaking transport-specific types."""
        kind = type(exc).__name__
        if kind == 'NotFound':
            return PlatformOperationError('not_found', str(exc))
        if kind == 'Forbidden':
            return PlatformOperationError('forbidden', str(exc))
        if getattr(exc, 'status', None) == 429:
            return PlatformOperationError('rate_limited', str(exc))
        return PlatformOperationError('unavailable', str(exc))

    @staticmethod
    def _create_error(exc: Exception) -> PlatformOperationError:
        """Classify only definite SDK API rejections; keep ambiguous writes parked.

        Discord 403/404/429 responses mean this HTTP Create was not accepted.
        Timeouts, socket failures and unrecognized exceptions may happen after
        transmission and must retain unknown_delivery for the attempt key.
        """
        classified = DiscordAdapter._read_error(exc)
        if classified.reason in ('forbidden', 'not_found', 'rate_limited'):
            return classified
        return PlatformOperationError('unknown_delivery', str(exc))

    def _validate_channel(self, expected: ChannelRef, physical: object) -> ChannelRef:
        """Check ID, guild and channel kind against independently observed SDK data."""
        self._check_connector(expected.connector_key)
        if str(getattr(physical, 'id', '')) != expected.external_channel_id:
            raise PlatformOperationError('invalid_request', 'fetched channel identity mismatch')
        discord = self._sdk()
        if isinstance(physical, discord.ForumChannel):
            kind = 'forum'
        elif isinstance(physical, discord.TextChannel):
            kind = 'announcement' if physical.is_news() else 'text'
        else:
            raise PlatformOperationError('unsupported', 'unsupported Discord channel kind')
        guild = getattr(physical, 'guild', None)
        guild_id = str(guild.id) if guild is not None else None
        if (expected.kind not in ('unknown', kind) or
            expected.external_guild_id is not None and expected.external_guild_id != guild_id):
            raise PlatformOperationError('invalid_request', 'Discord channel metadata conflicts')
        return ChannelRef(self.connector.key, str(physical.id), kind, guild_id)

    async def _get_sdk_channel(self, channel: ChannelRef) -> object:
        """Resolve exact physical channel using cache then fetch with read-only checks."""
        self._check_connector(channel.connector_key)
        channel_id = self._id(channel.external_channel_id)
        try:
            target = self._bot.get_channel(channel_id)
            if target is None:
                target = await self._bot.fetch_channel(channel_id)
        except Exception as exc:
            raise self._read_error(exc) from exc
        self._validate_channel(channel, target)
        return target

    @_tracked_io
    async def fetch_channel(self, channel: ChannelRef) -> ChannelView:
        """Expose verified neutral channel metadata without DB ownership claims."""
        sdk_channel = await self._get_sdk_channel(channel)
        return ChannelView(self._validate_channel(channel, sdk_channel),
                           getattr(sdk_channel, 'name', None))

    async def _get_sdk_thread(self, thread: ThreadRef) -> object:
        """Reject a same-ID thread under the wrong parent or guild."""
        self._check_connector(thread.connector_key)
        target_id = self._id(thread.external_thread_id)
        try:
            target = self._bot.get_channel(target_id)
            if target is None:
                target = await self._bot.fetch_channel(target_id)
        except Exception as exc:
            raise self._read_error(exc) from exc
        discord = self._sdk()
        if not isinstance(target, discord.Thread):
            raise PlatformOperationError('invalid_request', 'destination is not a Discord thread')
        if str(getattr(target, 'parent_id', '')) != thread.channel.external_channel_id:
            raise PlatformOperationError('invalid_request', 'thread under a different parent')
        # Resolve the parent independently: a stale Thread.parent cache alone
        # cannot authorize sending through an unrelated community surface.
        await self._get_sdk_channel(thread.channel)
        guild = getattr(target, 'guild', None)
        if (thread.channel.external_guild_id is not None and
            (guild is None or str(guild.id) != thread.channel.external_guild_id)):
            raise PlatformOperationError('invalid_request', 'thread guild does not match')
        return target

    @_tracked_io
    async def fetch_thread(self, thread: ThreadRef) -> ThreadView:
        """Return verified parent and only independently observed starter identity."""
        sdk_thread = await self._get_sdk_thread(thread)
        parent = self._validate_channel(thread.channel, await self._get_sdk_channel(thread.channel))
        actual_thread = ThreadRef(parent, str(sdk_thread.id))
        starter = getattr(sdk_thread, 'starter_message', None)
        observed = (MessageRef(self.connector.key, str(sdk_thread.id), str(starter.id))
                    if starter is not None and getattr(starter, 'id', None) is not None else None)
        return ThreadView(actual_thread, getattr(sdk_thread, 'name', None), observed,
                          'forum' if parent.kind == 'forum' else 'message')

    async def _get_message(self, message: MessageRef) -> object:
        """Read one exact message from its immediate container, never its forum."""
        self._check_connector(message.connector_key)
        container_id = self._id(message.external_container_id)
        message_id = self._id(message.external_message_id)
        try:
            container = self._bot.get_channel(container_id)
            if container is None:
                container = await self._bot.fetch_channel(container_id)
            if isinstance(container, self._sdk().Thread):
                # A physical thread must still have an independently resolvable parent.
                parent_id = getattr(container, 'parent_id', None)
                if parent_id is None:
                    raise PlatformOperationError('invalid_request', 'thread has no parent')
                await self._get_sdk_thread(ThreadRef(
                    ChannelRef(self.connector.key, str(parent_id)), str(container_id)))
            else:
                self._validate_channel(ChannelRef(self.connector.key, str(container_id)), container)
            return await container.fetch_message(message_id)
        except PlatformOperationError:
            raise
        except Exception as exc:
            raise self._read_error(exc) from exc

    def _author(self, physical: object) -> AuthorRef | None:
        """Translate observed Discord author without fabricating bridge ownership."""
        user = getattr(physical, 'author', None)
        if user is None or getattr(user, 'id', None) is None:
            return None
        is_webhook = getattr(physical, 'webhook_id', None) is not None
        is_bot = bool(getattr(user, 'bot', False))
        owned = (getattr(getattr(self._bot, 'user', None), 'id', None) == user.id
                 and not is_webhook)
        return AuthorRef(self.connector.key, str(user.id),
                         'webhook' if is_webhook else 'bot' if is_bot else 'human',
                         getattr(user, 'display_name', '') or '', bool(owned and is_bot))

    @_tracked_io
    async def fetch_message_state(
        self, channel_id: int, message_id: int,
    ) -> tuple[str, DiscordReconciliationMessage | None]:
        """Read an exact persisted Discord message for User Actor recovery.

        A verified 404 is a deletion signal; an inconsistent receipt, unreadable
        payload or transient transport failure must never synthesize a Delete.
        """
        container = self._id(str(channel_id))
        identifier = self._id(str(message_id))
        ref = MessageRef(self.connector.key, str(container), str(identifier))
        try:
            physical = await self._get_message(ref)
        except PlatformOperationError as exc:
            if exc.reason == 'not_found':
                return 'missing', None
            raise
        # The durable reconciliation can rewrite an ActivityPub object. Check
        # the *physical* receipt before it crosses into canonical state.
        actual_channel = getattr(getattr(physical, 'channel', None), 'id', None)
        if (str(getattr(physical, 'id', '')) != ref.external_message_id
            or str(actual_channel) != ref.external_container_id):
            raise PlatformOperationError('invalid_request', 'Discord reconciliation receipt mismatch')
        content = getattr(physical, 'content', None)
        if not isinstance(content, str) or getattr(physical, 'attachments', ()):
            raise PlatformOperationError('unsupported', 'Discord reconciliation requires plain text')
        edited_at = getattr(physical, 'edited_at', None)
        if edited_at is not None and not isinstance(edited_at, datetime):
            raise PlatformOperationError('invalid_request', 'Discord edited timestamp is invalid')
        return 'present', DiscordReconciliationMessage(content, edited_at)

    @_tracked_io
    async def send_owner_notice(self, discord_user_id: int, content: str) -> None:
        """Send one Discord-only registered User Actor owner notice without retry.

        The caller owns the durable at-most-once claim. A lost HTTP response
        may mean Discord already created the DM: never retry in this method.
        """
        user_id = self._id(str(discord_user_id))
        if not isinstance(content, str) or not content or len(content) > 2000:
            raise PlatformOperationError('invalid_request', 'invalid Discord notice content')
        try:
            user = await self._bot.fetch_user(user_id)
        except Exception as exc:
            raise self._read_error(exc) from exc
        if str(getattr(user, 'id', '')) != str(user_id):
            raise PlatformOperationError('invalid_request', 'Discord user lookup identity mismatch')
        try:
            await user.send(content, allowed_mentions=self._sdk().AllowedMentions.none())
        except Exception as exc:
            raise self._create_error(exc) from exc

    @_tracked_io
    async def fetch_message(self, message: MessageRef) -> MessageView:
        """Resolve actual physical content and author for missing Gateway fields."""
        physical = await self._get_message(message)
        if getattr(physical, 'attachments', ()):
            raise PlatformOperationError('unsupported', 'fetching attached Discord message as text is unsupported')
        edited_at = getattr(physical, 'edited_at', None)
        return MessageView(message, self._author(physical),
                           ContentDocument(str(getattr(physical, 'content', ''))), edited_at)

    @_tracked_io
    async def capabilities(self, channel: ChannelRef) -> PlatformCapabilities:
        """Report platform features independently of current member permissions."""
        actual = (await self.fetch_channel(channel)).channel
        return PlatformCapabilities(forum_posts=actual.kind == 'forum',
                                    message_threads=actual.kind in ('text', 'announcement'),
                                    thread_replies=actual.kind in ('text', 'announcement', 'forum'),
                                    message_edit_delete=True)

    @_tracked_io
    async def check_permissions(
        self, principal: AuthorRef, channel: ChannelRef,
        actions: frozenset[PlatformAction],
    ) -> dict[PlatformAction, PermissionResult]:
        """Return unknown for unverified principals, not a false ACL grant."""
        self._check_connector(principal.connector_key)
        target = await self._get_sdk_channel(channel)
        result: dict[PlatformAction, PermissionResult] = {}
        guild = getattr(target, 'guild', None)
        member = (guild.get_member(self._id(principal.external_actor_id))
                  if guild is not None and principal.external_actor_id is not None else None)
        permissions = target.permissions_for(member) if member is not None else None
        mapping = {
            'view_channel': 'view_channel', 'read_history': 'read_message_history',
            'create_forum_post': 'send_messages',
            'start_message_thread': 'create_public_threads',
            'send_message': 'send_messages',
            'edit_message': 'manage_messages', 'delete_message': 'manage_messages',
        }
        for action in actions:
            permission = mapping.get(action)
            if permission is None:
                result[action] = 'unsupported'
            elif ((action == 'create_forum_post' and channel.kind != 'forum') or
                  (action == 'start_message_thread' and channel.kind == 'forum')):
                result[action] = 'unsupported'
            elif permissions is None:
                result[action] = 'unknown'
            else:
                result[action] = ('allowed' if bool(getattr(permissions, permission, False))
                                  else 'denied')
        return result

    def _bot_principal(self) -> AuthorRef:
        """Identify the authenticated bot for write preflight without impersonation."""
        user = getattr(self._bot, 'user', None)
        if user is None or getattr(user, 'id', None) is None:
            raise PlatformOperationError('unavailable', 'Discord bot identity not ready')
        return AuthorRef(self.connector.key, str(user.id), 'bot',
                         getattr(user, 'display_name', '') or '', True)

    async def _require_bot_permission(self, channel: ChannelRef, action: PlatformAction,
                                      *, sdk_target: object | None = None) -> None:
        """Prove current bot permissions before transmitting a physical write."""
        bot = self._bot_principal()
        target = sdk_target or await self._get_sdk_channel(channel)
        guild = getattr(target, 'guild', None)
        member = guild.get_member(self._id(bot.external_actor_id)) if guild is not None else None
        if member is None:
            raise PlatformOperationError('forbidden', 'bot guild membership unresolved')
        permissions = target.permissions_for(member)
        flag = {'create_forum_post': 'send_messages',
                'start_message_thread': 'create_public_threads',
                'send_message': 'send_messages_in_threads' if hasattr(target, 'parent_id') else 'send_messages',
                'edit_message': 'manage_messages', 'delete_message': 'manage_messages'}[action]
        if not getattr(permissions, flag, False):
            raise PlatformOperationError('forbidden', f'Discord permission missing: {flag}')

    @_tracked_io
    async def create_forum_thread(
        self, forum: ChannelRef, content: ContentDocument, *, title: str,
        presentation: DeliveryPresentation, attempt: CreateAttempt,
    ) -> CreatedThread:
        """Create one forum post and return observed, distinct thread/starter IDs."""
        validate_create_attempt(attempt, 'forum_thread', forum)
        self._validate_outbound_content(content)
        self._validate_presentation(presentation)
        if not title.strip() or len(title) > 100:
            raise PlatformOperationError('invalid_request', 'invalid Discord forum title')

        async def physical() -> CreatedThread:
            """Resolve and authorize the forum before the sole HTTP write."""
            channel = await self._get_sdk_channel(forum)
            actual = self._validate_channel(forum, channel)
            if actual.kind != 'forum':
                raise PlatformOperationError('unsupported', 'not a forum')
            await self._require_bot_permission(actual, 'create_forum_post', sdk_target=channel)
            if bool(getattr(channel, 'flags', None) and getattr(channel.flags, 'require_tag', False)):
                raise PlatformOperationError('invalid_request', 'forum requires an explicit tag')
            try:
                discord = self._sdk()
                response = await channel.create_thread(
                    name=title, content=content.body, allowed_mentions=discord.AllowedMentions.none())
            except Exception as exc:
                raise self._create_error(exc) from exc
            thread = getattr(response, 'thread', None) or (response[0] if isinstance(response, tuple) else None)
            starter = getattr(response, 'message', None) or (response[1] if isinstance(response, tuple) and len(response) > 1 else None)
            if thread is None or starter is None or getattr(starter, 'id', None) is None:
                raise PlatformOperationError('unknown_delivery', 'Discord omitted thread/starter receipt')
            if str(getattr(thread, 'parent_id', '')) != actual.external_channel_id:
                raise PlatformOperationError('unknown_delivery', 'Discord forum thread parent mismatch')
            return CreatedThread(ThreadRef(actual, str(thread.id)),
                                 MessageRef(self.connector.key, str(thread.id), str(starter.id)), 'bot')

        return await self._begin_create_attempt(
            attempt, operation='forum_thread', destination=forum,
            content=content, presentation=presentation, title=title, reply_to=None,
            send=physical,
        )

    @_tracked_io
    async def create_reply_thread(self, root: MessageRef, *, title: str,
                                  attempt: CreateAttempt) -> ThreadRef:
        """Start a thread attached to an exact text/announcement root message."""
        validate_create_attempt(attempt, 'reply_thread', root)
        if not title.strip():
            raise PlatformOperationError('invalid_request', 'thread title is empty')

        async def physical() -> ThreadRef:
            """Only adopt a root-anchored reply thread with a verified parent."""
            channel = ChannelRef(self.connector.key, root.external_container_id)
            view = await self.fetch_channel(channel)
            if view.channel.kind not in ('text', 'announcement'):
                raise PlatformOperationError('unsupported', 'root is not a text channel')
            sdk_root = await self._get_message(root)
            await self._require_bot_permission(view.channel, 'start_message_thread')
            try:
                thread = await sdk_root.create_thread(name=title[:90].replace('@', '@\u200b'))
            except Exception as exc:
                # A failed create may have succeeded on Discord. Never guess by
                # a thread name or send a second POST from this error path.
                raise self._create_error(exc) from exc
            if (str(getattr(thread, 'parent_id', '')) != view.channel.external_channel_id
                or str(getattr(thread, 'id', '')) != root.external_message_id):
                raise PlatformOperationError('unknown_delivery', 'unverified root/thread association')
            return ThreadRef(view.channel, str(thread.id))

        return await self._begin_create_attempt(
            attempt, operation='reply_thread', destination=root, content=None,
            presentation=None, title=title, reply_to=None, send=physical,
        )

    @_tracked_io
    async def send_message(
        self, destination: ThreadRef | ChannelRef, content: ContentDocument, *,
        reply_to: MessageRef | None, presentation: DeliveryPresentation,
        attempt: CreateAttempt,
    ) -> CreatedMessage:
        """Send a single physical message without implicit reply-parent fallback."""
        validate_create_attempt(attempt, 'message', destination)
        validate_message_reply(destination, reply_to)
        self._validate_outbound_content(content)
        self._validate_presentation(presentation)

        async def physical() -> CreatedMessage:
            """Preflight exact channel/thread, membership and reference before POST."""
            if isinstance(destination, ThreadRef):
                sdk_target = await self._get_sdk_thread(destination)
                parent = destination.channel
            else:
                sdk_target = await self._get_sdk_channel(destination)
                parent = destination
            await self._require_bot_permission(parent, 'send_message', sdk_target=sdk_target)
            discord = self._sdk()
            kwargs: dict[str, object] = {
                'content': content.body,
                'allowed_mentions': discord.AllowedMentions.none(),
            }
            if reply_to is not None:
                kwargs['reference'] = discord.MessageReference(
                    message_id=self._id(reply_to.external_message_id),
                    channel_id=self._id(reply_to.external_container_id), fail_if_not_exists=True)
            try:
                created = await sdk_target.send(**kwargs)
            except Exception as exc:
                raise self._create_error(exc) from exc
            container = (destination.external_thread_id if isinstance(destination, ThreadRef)
                         else destination.external_channel_id)
            if str(getattr(getattr(created, 'channel', None), 'id', '')) != container:
                raise PlatformOperationError('unknown_delivery', 'Discord returned a different container')
            return CreatedMessage(MessageRef(self.connector.key, container, str(created.id)), 'bot')

        return await self._begin_create_attempt(
            attempt, operation='message', destination=destination,
            content=content, presentation=presentation, title=None,
            reply_to=reply_to, send=physical,
        )

    async def _require_message_mutation_permission(self, message: MessageRef, sdk_message: object) -> None:
        """Verify bot ownership or an explicit manage-messages grant before mutation."""
        bot = self._bot_principal()
        author = getattr(sdk_message, 'author', None)
        if author is not None and str(getattr(author, 'id', '')) == bot.external_actor_id:
            return
        container = getattr(sdk_message, 'channel', None)
        guild = getattr(container, 'guild', None)
        member = guild.get_member(self._id(bot.external_actor_id)) if guild is not None else None
        perms = container.permissions_for(member) if member is not None else None
        if perms is None or not bool(getattr(perms, 'manage_messages', False)):
            raise PlatformOperationError('forbidden', 'message is not bot-owned and manage_messages is missing')

    @_tracked_io
    async def edit_message(self, message: MessageRef, content: ContentDocument) -> None:
        """Edit an exact physical message; never mutate its containing thread."""
        self._validate_outbound_content(content)
        sdk_message = await self._get_message(message)
        await self._require_message_mutation_permission(message, sdk_message)
        try:
            discord = self._sdk()
            await sdk_message.edit(content=content.body, allowed_mentions=discord.AllowedMentions.none())
        except Exception as exc:
            raise self._read_error(exc) from exc

    @_tracked_io
    async def delete_message(self, message: MessageRef) -> None:
        """Delete an exact physical message, not a thread or canonical object."""
        sdk_message = await self._get_message(message)
        await self._require_message_mutation_permission(message, sdk_message)
        try:
            await sdk_message.delete()
        except Exception as exc:
            raise self._read_error(exc) from exc

    def enter_ingress(self) -> bool:
        """Register a callback task synchronously before it touches shared state."""
        if not self._ingress_admission_open:
            return False
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError('Gateway ingress must execute within an asyncio Task')
        self._ingress_tasks.add(task)
        return True

    def leave_ingress(self) -> None:
        """Unregister the current callback even when it was cancelled."""
        task = asyncio.current_task()
        if task is not None:
            self._ingress_tasks.discard(task)

    def close_ingress_admission(self) -> None:
        """Reject callbacks scheduled after shutdown, including late on_ready."""
        self._ingress_admission_open = False

    async def drain_ingress(self) -> None:
        """Await all independently spawned Gateway tasks before runtime closes."""
        self.close_ingress_admission()
        current = asyncio.current_task()
        tasks = [task for task in self._ingress_tasks if task is not current and not task.done()]
        await _cancel_and_join(tasks)

    async def quiesce(self) -> None:
        """Close physical create admission after Gateway ingress was drained."""
        if self._ingress_admission_open or any(not t.done() for t in self._ingress_tasks):
            raise RuntimeError('drain Gateway ingress before Discord SDK I/O')
        self._io_admission_open = False
        # Read/edit/delete operations have no CreateAttempt future, so drain
        # their owning tasks as well as Create owners before closing the SDK.
        current = asyncio.current_task()
        tasks = [task for task in self._io_tasks if task is not current and not task.done()]
        await _cancel_and_join(tasks)
        # Create cancellation records an unknown receipt, never proof of no
        # physical Discord POST; nothing can safely replay such an attempt.
        pending = [entry.future for entry in self._creates.values() if not entry.future.done()]
        if pending:
            await asyncio.gather(*(asyncio.shield(f) for f in pending))
