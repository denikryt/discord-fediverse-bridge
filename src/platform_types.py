"""SDK-independent platform references, content, receipts and inbound event contracts.

These are snapshots, not database entities or authenticated credentials. Physical
IDs belong to exactly one connector and never stand in for ActivityPub IDs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal, TypeAlias
from urllib.parse import urlsplit, urlunsplit


PlatformName: TypeAlias = Literal['discord', 'fluxer']
ChannelKind: TypeAlias = Literal['forum', 'text', 'announcement', 'unknown']
AuthorKind: TypeAlias = Literal['human', 'bot', 'webhook', 'unknown']
DeliveryMode: TypeAlias = Literal['bot', 'webhook']
PlatformAction: TypeAlias = Literal[
    'view_channel', 'read_history', 'create_forum_post', 'start_message_thread',
    'send_message', 'edit_message', 'delete_message',
]
PermissionResult: TypeAlias = Literal['allowed', 'denied', 'unknown', 'unsupported']
PlatformErrorReason: TypeAlias = Literal[
    'not_found', 'forbidden', 'unsupported', 'invalid_request',
    'rate_limited', 'unavailable', 'unknown_delivery',
]


def _identity(value: str, name: str) -> None:
    """Reject invalid opaque IDs without interpreting them as Discord integers."""
    if (not isinstance(value, str) or not value or not value.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in value)):
        raise ValueError(f'{name} must be a nonempty opaque string')


def _optional_identity(value: str | None, name: str) -> None:
    """Validate a known ID while preserving unknown metadata as None."""
    if value is not None:
        _identity(value, name)


def _one_of(value: object, possibilities: tuple[str, ...], name: str) -> None:
    """Check runtime values even when a caller does not run a type checker."""
    if value not in possibilities:
        raise ValueError(f'invalid {name}: {value!r}')


@dataclass(frozen=True, slots=True)
class ConnectorRef:
    """Non-secret snapshot of one persisted immutable platform connection."""

    key: str
    platform: PlatformName
    instance_origin: str | None = None

    def __post_init__(self) -> None:
        """Enforce Stage 01 connector metadata before registry registration."""
        _identity(self.key, 'connector key')
        _one_of(self.platform, ('discord', 'fluxer'), 'platform')
        if self.platform == 'discord':
            if self.instance_origin is not None:
                raise ValueError('Discord connectors have no instance origin')
        else:
            if not isinstance(self.instance_origin, str):
                raise ValueError('Fluxer connectors require an instance origin')
            # Persisted Stage 01 origins contain only HTTP(S) scheme/authority.
            # Keep DTO imports independent from SQLAlchemy/repository ownership.
            parsed = urlsplit(self.instance_origin)
            if (parsed.scheme.lower() not in ('http', 'https') or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.path or parsed.query or parsed.fragment):
                raise ValueError('Fluxer connector origin requires a clean HTTP(S) authority')
            host = parsed.hostname.lower()
            host = f'[{host}]' if ':' in host else host
            if parsed.port is not None:
                host += f':{parsed.port}'
            canonical = urlunsplit((parsed.scheme.lower(), host, '', '', ''))
            if self.instance_origin != canonical:
                raise ValueError('Fluxer connector origin must be canonical')


@dataclass(frozen=True, slots=True)
class ChannelRef:
    """Physical channel identity; refinable kind/guild metadata is not identity."""

    connector_key: str
    external_channel_id: str
    kind: ChannelKind = field(default='unknown', compare=False)
    external_guild_id: str | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        """Validate physical identity but allow an unresolved channel kind."""
        _identity(self.connector_key, 'connector key')
        _identity(self.external_channel_id, 'channel ID')
        _one_of(self.kind, ('forum', 'text', 'announcement', 'unknown'), 'channel kind')
        _optional_identity(self.external_guild_id, 'guild ID')


@dataclass(frozen=True, slots=True)
class ThreadRef:
    """Physical thread with a known parent channel, never a starter-message ID."""

    channel: ChannelRef
    external_thread_id: str

    def __post_init__(self) -> None:
        """Ensure the contained channel is a real scoped reference."""
        if not isinstance(self.channel, ChannelRef):
            raise ValueError('thread requires a ChannelRef parent')
        _identity(self.external_thread_id, 'thread ID')

    @property
    def connector_key(self) -> str:
        """Return the connector of the immutable parent channel."""
        return self.channel.connector_key


@dataclass(frozen=True, slots=True)
class MessageRef:
    """Message identity using its immediate container, not the parent forum."""

    connector_key: str
    external_container_id: str
    external_message_id: str

    def __post_init__(self) -> None:
        """Allow raw delete/update IDs without a network or database lookup."""
        _identity(self.connector_key, 'connector key')
        _identity(self.external_container_id, 'message container ID')
        _identity(self.external_message_id, 'message ID')


@dataclass(frozen=True, slots=True)
class AuthorRef:
    """Untrusted inbound platform provenance; never a bridge/AP user identity."""

    connector_key: str
    external_actor_id: str | None
    kind: AuthorKind
    display_name: str
    is_bridge_owned: bool = False

    def __post_init__(self) -> None:
        """Prevent absent authors from being claimed as identified humans."""
        _identity(self.connector_key, 'connector key')
        _optional_identity(self.external_actor_id, 'actor ID')
        _one_of(self.kind, ('human', 'bot', 'webhook', 'unknown'), 'author kind')
        if not isinstance(self.display_name, str):
            raise ValueError('display name must be text')
        if self.kind == 'human' and self.external_actor_id is None:
            raise ValueError('human author requires a verified platform actor ID')
        if self.is_bridge_owned and self.external_actor_id is None:
            raise ValueError('owned bot/webhook provenance requires verified actor ID')
        if not isinstance(self.is_bridge_owned, bool) or (
            self.is_bridge_owned and self.kind not in ('bot', 'webhook')
        ):
            raise ValueError('only a verified bot or webhook can be bridge-owned')


def _http_url(url: str, name: str) -> None:
    """Require a parseable HTTP(S) link; fetching and SSRF policy stay in adapters."""
    if not isinstance(url, str):
        raise ValueError(f'{name} must be an HTTP(S) URL')
    parsed = urlsplit(url)
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname
        or parsed.username is not None or parsed.password is not None):
        raise ValueError(f'{name} must be HTTP(S) without embedded credentials')


@dataclass(frozen=True, slots=True)
class AttachmentRef:
    """Attachment metadata for adapter-owned download/upload and size checks."""

    url: str
    filename: str
    mime_type: str | None = None
    size: int | None = None

    def __post_init__(self) -> None:
        """Reject non-HTTP locations without fetching untrusted URLs in core."""
        _http_url(self.url, 'attachment URL')
        _identity(self.filename, 'attachment filename')
        if self.mime_type is not None:
            _identity(self.mime_type, 'MIME type')
        if self.size is not None and (type(self.size) is not int or self.size < 0):
            raise ValueError('attachment size cannot be negative')


@dataclass(frozen=True, slots=True)
class ContentDocument:
    """Neutral message content, preserving an empty body separately from missing edits."""

    body: str
    format: Literal['plain', 'portable_markdown'] = 'plain'
    title: str | None = None
    source_url: str | None = None
    attachments: tuple[AttachmentRef, ...] = ()

    def __post_init__(self) -> None:
        """Reject SDK/native payloads rather than converting them inside core."""
        if not isinstance(self.body, str):
            raise ValueError('content body must be text')
        _one_of(self.format, ('plain', 'portable_markdown'), 'content format')
        if self.title is not None and not isinstance(self.title, str):
            raise ValueError('title must be text')
        if self.source_url is not None:
            _http_url(self.source_url, 'source URL')
        if not isinstance(self.attachments, tuple) or not all(isinstance(item, AttachmentRef) for item in self.attachments):
            raise ValueError('attachments must be immutable attachment descriptors')


@dataclass(frozen=True, slots=True)
class DeliveryPresentation:
    """Requested visual delivery mode; webhook display is not AP attribution."""

    mode: DeliveryMode = 'bot'
    display_name: str | None = None
    avatar_url: str | None = None

    def __post_init__(self) -> None:
        """Refuse invalid modes before a transport decides actual capability."""
        _one_of(self.mode, ('bot', 'webhook'), 'delivery mode')
        if self.display_name is not None and not isinstance(self.display_name, str):
            raise ValueError('display name must be text')
        if self.avatar_url is not None:
            _http_url(self.avatar_url, 'avatar URL')


@dataclass(frozen=True, slots=True)
class CreatedThread:
    """Successful forum creation receipt with independently observed starter ID."""

    thread: ThreadRef
    starter: MessageRef
    delivery_mode: DeliveryMode

    def __post_init__(self) -> None:
        """Reject a starter addressed to a different immediate container."""
        if not isinstance(self.thread, ThreadRef) or not isinstance(self.starter, MessageRef):
            raise ValueError('created thread requires thread and starter refs')
        if (self.thread.connector_key, self.thread.external_thread_id) != (
            self.starter.connector_key, self.starter.external_container_id
        ):
            raise ValueError('starter must belong to the created thread container')
        _one_of(self.delivery_mode, ('bot', 'webhook'), 'delivery mode')


@dataclass(frozen=True, slots=True)
class CreatedMessage:
    """Successful message creation receipt, scoped to its actual container."""

    message: MessageRef
    delivery_mode: DeliveryMode

    def __post_init__(self) -> None:
        """Require a physical message reference and explicit actual send mode."""
        if not isinstance(self.message, MessageRef):
            raise ValueError('created message requires a MessageRef')
        _one_of(self.delivery_mode, ('bot', 'webhook'), 'delivery mode')


@dataclass(frozen=True, slots=True)
class CreateAttempt:
    """Core-owned correlation key for one desired physical copy, not a retry permit."""

    key: str
    connector_key: str
    operation: Literal['forum_thread', 'reply_thread', 'message']
    destination: ChannelRef | ThreadRef | MessageRef

    def __post_init__(self) -> None:
        """Validate operation/target shape and connector before any network write."""
        _identity(self.key, 'attempt key')
        _identity(self.connector_key, 'connector key')
        required = {'forum_thread': ChannelRef, 'reply_thread': MessageRef, 'message': (ChannelRef, ThreadRef)}
        if self.operation not in required or not isinstance(self.destination, required[self.operation]):
            raise ValueError('create attempt operation/destination mismatch')
        if self.destination.connector_key != self.connector_key:
            raise ValueError('create attempt targets another connector')


@dataclass(frozen=True, slots=True)
class PlatformCapabilities:
    """Technical support flags; never authorization or a promise of ACL permission."""

    forum_posts: bool = False
    message_threads: bool = False
    thread_replies: bool = False
    attachments: bool = False
    webhook_send: bool = False
    webhook_edit_delete: bool = False
    message_edit_delete: bool = False

    def __post_init__(self) -> None:
        """Keep capability values strictly boolean rather than truthy strings."""
        if any(type(getattr(self, name)) is not bool for name in self.__dataclass_fields__):
            raise ValueError('capability flags must be booleans')


class PlatformOperationError(Exception):
    """Transport-neutral definite/unknown delivery classification for core callers."""

    def __init__(self, reason: PlatformErrorReason, message: str = '') -> None:
        """Expose one reason; never expose SDK exceptions or a second state field."""
        _one_of(reason, ('not_found', 'forbidden', 'unsupported', 'invalid_request',
                         'rate_limited', 'unavailable', 'unknown_delivery'), 'error reason')
        self.reason = reason
        super().__init__(message or reason)


def _known_channel_metadata_agrees(left: ChannelRef, right: ChannelRef) -> bool:
    """Compare only known metadata for the same physical channel."""
    # ChannelRef equality deliberately ignores refinable metadata; placement
    # validation must detect contradictions without rejecting unknown values.
    return (left.kind == 'unknown' or right.kind == 'unknown' or left.kind == right.kind) and (
        left.external_guild_id is None or right.external_guild_id is None
        or left.external_guild_id == right.external_guild_id
    )


def validate_create_attempt(
    attempt: CreateAttempt,
    operation: Literal['forum_thread', 'reply_thread', 'message'],
    destination: ChannelRef | ThreadRef | MessageRef,
) -> None:
    """Admission check for adapters before each create's first network I/O."""
    if not isinstance(attempt, CreateAttempt) or attempt.operation != operation or (
        type(attempt.destination) is not type(destination) or attempt.destination != destination
    ) or attempt.connector_key != destination.connector_key:
        # ChannelRef equality ignores only refinable metadata, by design.
        raise PlatformOperationError('invalid_request', 'create attempt target/operation mismatch')


def validate_message_reply(
    destination: ThreadRef | ChannelRef,
    reply_to: MessageRef | None,
) -> None:
    """Reject cross-connector/container replies; never silently make them flat."""
    if not isinstance(destination, (ThreadRef, ChannelRef)):
        raise PlatformOperationError('invalid_request', 'invalid message destination')
    if reply_to is None:
        return
    container = (destination.external_thread_id if isinstance(destination, ThreadRef)
                 else destination.external_channel_id)
    if not isinstance(reply_to, MessageRef) or (
        reply_to.connector_key, reply_to.external_container_id
    ) != (destination.connector_key, container):
        raise PlatformOperationError('invalid_request', 'reply parent outside destination container')


def validate_created_receipt(
    attempt: CreateAttempt,
    receipt: CreatedThread | ThreadRef | CreatedMessage,
    *, presentation: DeliveryPresentation | None = None,
) -> None:
    """Reject a mismatched verified create result before persisting a surface mapping.

    This structural check does not prove attempt-specific delivery: only an
    adapter with platform evidence can attest a receipt in reconcile_create.
    """
    expected = {'forum_thread': CreatedThread, 'reply_thread': ThreadRef, 'message': CreatedMessage}
    if not isinstance(attempt, CreateAttempt) or type(receipt) is not expected[attempt.operation]:
        raise PlatformOperationError('invalid_request', 'unexpected create receipt type')
    if presentation is not None:
        if (not isinstance(presentation, DeliveryPresentation)
            or not isinstance(receipt, (CreatedThread, CreatedMessage))
            or receipt.delivery_mode != presentation.mode):
            # Do not report a webhook request as a successful bot delivery.
            raise PlatformOperationError('invalid_request', 'receipt delivery mode contradicts request')
    if isinstance(receipt, CreatedThread):
        requested = attempt.destination
        observed = receipt.thread.channel
        if observed != requested:
            raise PlatformOperationError('invalid_request', 'created forum thread has wrong parent')
        # Physical equality deliberately ignores mutable metadata. A receipt
        # must still reject contradictory *known* kind/guild observations.
        if not _known_channel_metadata_agrees(requested, observed):
            raise PlatformOperationError('invalid_request', 'created forum thread has conflicting parent metadata')
    elif isinstance(receipt, CreatedMessage):
        destination = attempt.destination
        container = (destination.external_thread_id if isinstance(destination, ThreadRef)
                     else destination.external_channel_id)
        if (receipt.message.connector_key, receipt.message.external_container_id) != (
            attempt.connector_key, container
        ):
            raise PlatformOperationError('invalid_request', 'created message has wrong destination')
    else:
        # The adapter must additionally verify root→thread uniqueness and kind;
        # other platforms must not inherit Discord's root/thread ID equivalence.
        if receipt.connector_key != attempt.connector_key or (
            receipt.channel.external_channel_id != attempt.destination.external_container_id
        ):
            raise PlatformOperationError('invalid_request', 'reply thread not anchored to root channel')


@dataclass(frozen=True, slots=True)
class ChannelView:
    """Actual adapter-resolved channel metadata; not a domain ownership claim."""

    channel: ChannelRef
    name: str | None = None

    def __post_init__(self) -> None:
        """A fetched channel view must identify an actual physical channel."""
        if not isinstance(self.channel, ChannelRef):
            raise ValueError('channel view requires ChannelRef')


@dataclass(frozen=True, slots=True)
class ThreadView:
    """Actual thread and observed starter, which may be unknown after a fetch."""

    thread: ThreadRef
    name: str | None = None
    starter: MessageRef | None = None
    kind: Literal['forum', 'message', 'unknown'] = 'unknown'

    def __post_init__(self) -> None:
        """Never substitute the thread ID for an unknown starter message ID."""
        if not isinstance(self.thread, ThreadRef):
            raise ValueError('thread view requires ThreadRef')
        _one_of(self.kind, ('forum', 'message', 'unknown'), 'thread kind')
        if self.starter is not None and not isinstance(self.starter, MessageRef):
            raise ValueError('thread starter must be MessageRef')
        if self.starter is not None and (self.starter.connector_key, self.starter.external_container_id) != (
            self.thread.connector_key, self.thread.external_thread_id
        ):
            raise ValueError('thread starter does not belong to the thread')


@dataclass(frozen=True, slots=True)
class MessageView:
    """Adapter-resolved physical message body and optional author information."""

    message: MessageRef
    author: AuthorRef | None = None
    content: ContentDocument | None = None
    edited_at: datetime | None = None

    def __post_init__(self) -> None:
        """Reject a resolved author from another connector."""
        if not isinstance(self.message, MessageRef):
            raise ValueError('message view requires MessageRef')
        if self.author is not None and (
            not isinstance(self.author, AuthorRef) or self.author.connector_key != self.message.connector_key
        ):
            raise ValueError('message author belongs to another connector')
        if self.content is not None and not isinstance(self.content, ContentDocument):
            raise ValueError('message view content must be neutral content')
        _aware_datetime(self.edited_at, 'message view edited_at')


def _aware_datetime(value: datetime | None, name: str, *, optional: bool = True) -> None:
    """Reject invalid or naive timestamps before consumers compare event times."""
    if value is None and optional:
        return
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f'{name} must be a timezone-aware datetime')


def _guild_context(external_guild_id: str | None, *channels: ChannelRef | None) -> None:
    """Check the event guild against every independently resolved context."""
    _optional_identity(external_guild_id, 'guild ID')
    for channel in channels:
        if (channel is not None and channel.external_guild_id is not None
            and external_guild_id is not None and channel.external_guild_id != external_guild_id):
            raise ValueError('event guild contradicts known channel guild')


def _thread_event(connector_key: str, thread: ThreadRef, owner: AuthorRef | None = None) -> None:
    """Ensure enriched thread/owner metadata matches event physical identity."""
    _identity(connector_key, 'event connector key')
    if not isinstance(thread, ThreadRef) or connector_key != thread.connector_key:
        raise ValueError('thread event connector mismatch')
    if owner is not None and (
        not isinstance(owner, AuthorRef) or owner.connector_key != connector_key
    ):
        raise ValueError('thread owner connector mismatch')


def _message_event(
    connector_key: str, message: MessageRef,
    parent_channel: ChannelRef | None, thread_context: ThreadRef | None,
) -> None:
    """Accept partial raw events but reject contradictory optional thread context."""
    _identity(connector_key, 'event connector key')
    if not isinstance(message, MessageRef) or connector_key != message.connector_key:
        raise ValueError('message event connector mismatch')
    if parent_channel is not None and (
        not isinstance(parent_channel, ChannelRef) or parent_channel.connector_key != connector_key
    ):
        raise ValueError('message parent connector mismatch')
    if thread_context is not None:
        if not isinstance(thread_context, ThreadRef) or thread_context.connector_key != connector_key or (
            thread_context.external_thread_id != message.external_container_id
        ):
            raise ValueError('message immediate container is not its thread context')
        if parent_channel is not None and (parent_channel != thread_context.channel
            or not _known_channel_metadata_agrees(parent_channel, thread_context.channel)):
            raise ValueError('conflicting resolved parent channel')


@dataclass(frozen=True, slots=True)
class ThreadCreated:
    """Gateway thread-created/rejoined notice; not proof of a new post."""

    connector_key: str
    thread: ThreadRef
    name: str
    owner: AuthorRef | None = None
    starter: MessageRef | None = None
    newly_created: bool | None = None
    external_guild_id: str | None = None
    received_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        """Check event context and preserve unknown starter/newly-created fields."""
        _aware_datetime(self.received_at, 'received_at', optional=False)
        _thread_event(self.connector_key, self.thread, self.owner)
        if self.starter is not None and (
            self.starter.connector_key, self.starter.external_container_id
        ) != (self.connector_key, self.thread.external_thread_id):
            raise ValueError('starter not in created thread')
        if self.newly_created is not None and type(self.newly_created) is not bool:
            raise ValueError('newly_created must be bool or None')
        _guild_context(self.external_guild_id, self.thread.channel)


@dataclass(frozen=True, slots=True)
class ThreadUpdated:
    """Partial physical thread metadata change, not a canonical post edit."""

    connector_key: str
    thread: ThreadRef
    name: str | None = None
    external_guild_id: str | None = None
    received_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        """Reject connector mismatch even if no metadata fields are present."""
        _aware_datetime(self.received_at, 'received_at', optional=False)
        _thread_event(self.connector_key, self.thread)
        _guild_context(self.external_guild_id, self.thread.channel)


@dataclass(frozen=True, slots=True)
class ThreadDeleted:
    """Physical thread removal notice; core determines source versus copy role."""

    connector_key: str
    thread: ThreadRef
    external_guild_id: str | None = None
    received_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        """Reject delete events that target a different connector."""
        _aware_datetime(self.received_at, 'received_at', optional=False)
        _thread_event(self.connector_key, self.thread)
        _guild_context(self.external_guild_id, self.thread.channel)


@dataclass(frozen=True, slots=True)
class MessageCreated:
    """Observed new message, optionally identified as a forum starter."""

    connector_key: str
    message: MessageRef
    author: AuthorRef
    content: ContentDocument
    reply_to: MessageRef | None = None
    is_starter: bool | None = None
    parent_channel: ChannelRef | None = None
    thread_context: ThreadRef | None = None
    external_guild_id: str | None = None
    received_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # Publication time comes from the source message, never Gateway receipt time.
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        """Preserve provenance and reject contradictory resolved placement data."""
        _aware_datetime(self.received_at, 'received_at', optional=False)
        _aware_datetime(self.created_at, 'message created_at')
        _message_event(self.connector_key, self.message, self.parent_channel, self.thread_context)
        if not isinstance(self.author, AuthorRef) or self.author.connector_key != self.connector_key:
            raise ValueError('message author connector mismatch')
        if not isinstance(self.content, ContentDocument):
            raise ValueError('message create requires content')
        if self.reply_to is not None and (
            not isinstance(self.reply_to, MessageRef) or self.reply_to.connector_key != self.connector_key
        ):
            raise ValueError('reply parent connector mismatch')
        if self.is_starter is not None and type(self.is_starter) is not bool:
            raise ValueError('is_starter must be bool or None')
        _guild_context(self.external_guild_id, self.parent_channel,
                       self.thread_context.channel if self.thread_context else None)


@dataclass(frozen=True, slots=True)
class MessageUpdated:
    """Potentially partial message edit; missing content is not empty content."""

    connector_key: str
    message: MessageRef
    content: ContentDocument | None = None
    edited_at: datetime | None = None
    attachments: tuple[AttachmentRef, ...] | None = None
    parent_channel: ChannelRef | None = None
    thread_context: ThreadRef | None = None
    external_guild_id: str | None = None
    received_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # Missing raw author is not an assertion about the original author.
    author: AuthorRef | None = None

    def __post_init__(self) -> None:
        """Allow ID-only raw edits and preserve absent attachment/body fields."""
        _aware_datetime(self.received_at, 'received_at', optional=False)
        if self.author is not None and (
            not isinstance(self.author, AuthorRef) or self.author.connector_key != self.connector_key
        ):
            raise ValueError('edited message author connector mismatch')
        _message_event(self.connector_key, self.message, self.parent_channel, self.thread_context)
        if self.content is not None and not isinstance(self.content, ContentDocument):
            raise ValueError('edited content must be neutral content or unknown')
        _aware_datetime(self.edited_at, 'message update edited_at')
        if self.attachments is not None and (
            not isinstance(self.attachments, tuple)
            or not all(isinstance(item, AttachmentRef) for item in self.attachments)
        ):
            raise ValueError('edited attachments must be immutable descriptors')
        _guild_context(self.external_guild_id, self.parent_channel,
                       self.thread_context.channel if self.thread_context else None)


@dataclass(frozen=True, slots=True)
class MessageDeleted:
    """Raw message deletion from immediate container, without required lookup."""

    connector_key: str
    message: MessageRef
    actor: AuthorRef | None = None
    parent_channel: ChannelRef | None = None
    thread_context: ThreadRef | None = None
    external_guild_id: str | None = None
    received_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        """Validate only claims present in the raw or independently resolved event."""
        _aware_datetime(self.received_at, 'received_at', optional=False)
        _message_event(self.connector_key, self.message, self.parent_channel, self.thread_context)
        if self.actor is not None and (
            not isinstance(self.actor, AuthorRef) or self.actor.connector_key != self.connector_key
        ):
            raise ValueError('deletion actor connector mismatch')
        _guild_context(self.external_guild_id, self.parent_channel,
                       self.thread_context.channel if self.thread_context else None)


PlatformInboundEvent: TypeAlias = (
    ThreadCreated | ThreadUpdated | ThreadDeleted
    | MessageCreated | MessageUpdated | MessageDeleted
)
