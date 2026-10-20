"""Pure Discord Gateway event normalization into SDK-independent platform events.

The adapter owns transport authentication and calls this mapper with genuinely
observed SDK/payload fields. Missing cache data remains unknown, never inferred.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from .platform_types import (
    AuthorRef, ChannelKind, ChannelRef, ContentDocument, MessageCreated,
    MessageDeleted, MessageRef, MessageUpdated, ThreadCreated, ThreadDeleted,
    ThreadRef, ThreadUpdated,
)


class DiscordGatewayNormalizer:
    """Convert verified Discord SDK/raw callbacks without domain side effects."""

    def __init__(
        self, connector_key: str, *, owned_bot_id: str | None,
        owned_webhook_ids: frozenset[str] = frozenset(),
    ) -> None:
        """Fix physical connector identity and authenticated local author IDs."""
        if not connector_key:
            raise ValueError("connector key required")
        self.connector_key = connector_key
        self.owned_bot_id = owned_bot_id
        self.owned_webhook_ids = owned_webhook_ids

    @staticmethod
    def _id(value: object, name: str) -> str:
        """Read a present external snowflake without fabricating cache state."""
        if value is None or not str(value).isdecimal():
            raise ValueError(f"Discord {name} must be a present numeric snowflake")
        return str(value)

    @staticmethod
    def _optional_id(value: object) -> str | None:
        """Preserve absence, rather than interpreting a missing ID as zero."""
        return None if value is None else DiscordGatewayNormalizer._id(value, "optional ID")

    def _channel(
        self, channel_id: object, kind: ChannelKind = "unknown",
        guild_id: object = None,
    ) -> ChannelRef:
        """Construct a verified or explicitly unknown physical channel reference."""
        return ChannelRef(
            connector_key=self.connector_key,
            external_channel_id=self._id(channel_id, "channel ID"),
            kind=kind,
            external_guild_id=self._optional_id(guild_id),
        )

    def _author(self, sdk_author: object | None, webhook_id: object = None) -> AuthorRef:
        """Classify only author provenance observable in the authenticated SDK."""
        if webhook_id is not None:
            webhook_identity = self._id(webhook_id, "webhook ID")
            return AuthorRef(
                self.connector_key, webhook_identity, "webhook",
                str(getattr(sdk_author, "display_name", "")),
                is_bridge_owned=webhook_identity in self.owned_webhook_ids,
            )
        raw_id = getattr(sdk_author, "id", None)
        identity = self._optional_id(raw_id)
        observed_bot = getattr(sdk_author, "bot", None)
        kind = ("bot" if observed_bot is True else "human"
                if identity is not None and observed_bot is False else "unknown")
        return AuthorRef(
            self.connector_key, identity, kind,
            str(getattr(sdk_author, "display_name", "")),
            is_bridge_owned=(kind == "bot" and identity is not None
                             and identity == self.owned_bot_id),
        )

    def message_created(
        self, message: object, *, channel_kind: ChannelKind,
        parent_kind: ChannelKind = "unknown",
        verified_parent: object | None = None,
    ) -> MessageCreated | None:
        """Map a full SDK message while preserving source timestamp and container."""
        message_type = getattr(message, "type", None)
        if getattr(message_type, "name", message_type) == "thread_starter_message":
            # Discord emits a synthetic starter reference that is not another
            # human post and must never be federated as a canonical message.
            return None
        container = getattr(message, "channel")
        guild_id = getattr(getattr(message, "guild", None), "id", None)
        channel_guild = getattr(getattr(container, "guild", None), "id", None)
        if guild_id is not None and channel_guild is not None and str(guild_id) != str(channel_guild):
            raise ValueError("Discord message guild contradicts observed channel")
        # An adapter may independently fetch a cold-cache thread parent;
        # the normalizer must not invent one from the numeric parent ID.
        parent = verified_parent if verified_parent is not None else getattr(container, "parent", None)
        if parent is None:
            # A thread may retain parent_id after its parent object falls out
            # of the SDK cache. Never misclassify it as a root text channel;
            # the adapter must resolve the parent before this conversion.
            if getattr(container, "parent_id", None) is not None:
                raise ValueError("Discord thread parent is not verified")
            channel = self._channel(getattr(container, "id", None), channel_kind, guild_id)
            thread_context = None
        else:
            parent_id = getattr(container, "parent_id", getattr(parent, "id", None))
            if str(parent_id) != str(getattr(parent, "id", None)):
                raise ValueError("Discord thread parent contradicts observed channel")
            channel = self._channel(parent_id, parent_kind, guild_id)
            thread_context = ThreadRef(channel, self._id(getattr(container, "id", None), "thread"))
        message_ref = MessageRef(
            self.connector_key, self._id(getattr(container, "id", None), "container"),
            self._id(getattr(message, "id", None), "message"),
        )
        reference = getattr(message, "reference", None)
        reply_id = getattr(reference, "message_id", None)
        reply_container = getattr(reference, "channel_id", None)
        reply = None if reply_id is None else MessageRef(
            self.connector_key,
            self._id(reply_container or message_ref.external_container_id, "reply container"),
            self._id(reply_id, "reply ID"),
        )
        return MessageCreated(
            connector_key=self.connector_key,
            message=message_ref,
            author=self._author(getattr(message, "author", None), getattr(message, "webhook_id", None)),
            content=ContentDocument(body=getattr(message, "content", "")),
            reply_to=reply,
            is_starter=None,
            parent_channel=channel,
            thread_context=thread_context,
            external_guild_id=self._optional_id(guild_id),
            created_at=getattr(message, "created_at", None),
        )

    @staticmethod
    def _timestamp(value: object) -> datetime | None:
        """Decode an optional ISO timestamp; an absent edit never becomes now."""
        if value is None:
            return None
        if isinstance(value, datetime):
            candidate = value
        elif isinstance(value, str):
            try:
                candidate = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("invalid Discord edited timestamp") from exc
        else:
            raise ValueError("Discord edited timestamp must be ISO datetime")
        if candidate.tzinfo is None or candidate.utcoffset() is None:
            raise ValueError("Discord edited timestamp must contain timezone")
        return candidate

    def raw_message_updated(self, raw: object) -> MessageUpdated:
        """Normalize an ID-only raw edit without a cache fetch or inferred content."""
        data = getattr(raw, "data", None) or {}
        if not isinstance(data, dict):
            raise ValueError("raw edit data must be an object")
        author = data.get("author")
        normalized_author = None
        if isinstance(author, dict):
            raw_id = author.get("id")
            sdk_like_author = SimpleNamespace(
                id=raw_id, bot=author.get("bot"),
                display_name=author.get("global_name") or author.get("username", ""),
            )
            normalized_author = self._author(sdk_like_author, data.get("webhook_id"))
        return MessageUpdated(
            connector_key=self.connector_key,
            message=MessageRef(self.connector_key, self._id(getattr(raw, "channel_id", None), "container"),
                               self._id(getattr(raw, "message_id", None), "message")),
            content=ContentDocument(data["content"]) if "content" in data else None,
            edited_at=self._timestamp(data.get("edited_timestamp")),
            external_guild_id=self._optional_id(getattr(raw, "guild_id", None)),
            author=normalized_author,
        )

    def raw_message_deleted(self, raw: object) -> MessageDeleted:
        """Normalize a cache-independent delete using immediate message container."""
        return MessageDeleted(
            connector_key=self.connector_key,
            message=MessageRef(self.connector_key,
                               self._id(getattr(raw, "channel_id", None), "container"),
                               self._id(getattr(raw, "message_id", None), "message")),
            external_guild_id=self._optional_id(getattr(raw, "guild_id", None)),
        )

    def thread_created(self, thread: object, *, parent_kind: ChannelKind) -> ThreadCreated | None:
        """Normalize verified forum threads or text-root discussion threads.

        A text/announcement parent is still only a physical observation; the
        existing Personal Feed root mapping decides whether adoption is valid.
        """
        if parent_kind not in ('forum', 'text', 'announcement'):
            return None
        parent = getattr(thread, "parent", None)
        if parent is None:
            return None
        parent_id = self._id(getattr(thread, "parent_id", None), "thread parent")
        if parent_id != self._id(getattr(parent, "id", None), "parent"):
            raise ValueError("Discord thread parent contradicts observed parent")
        guild_id = getattr(getattr(thread, "guild", None), "id", None)
        channel = self._channel(parent_id, parent_kind, guild_id)
        thread_ref = ThreadRef(channel, self._id(getattr(thread, "id", None), "thread"))
        starter = getattr(thread, "starter_message", None)
        starter_ref = None
        if starter is not None:
            if str(getattr(getattr(starter, "channel", None), "id", None)) != thread_ref.external_thread_id:
                raise ValueError("observed starter does not belong to thread")
            starter_ref = MessageRef(
                self.connector_key, thread_ref.external_thread_id,
                self._id(getattr(starter, "id", None), "starter"),
            )
        owner_id = getattr(thread, "owner_id", None)
        owner = None if owner_id is None else AuthorRef(
            self.connector_key, self._id(owner_id, "owner"), "unknown", "",
        )
        return ThreadCreated(
            connector_key=self.connector_key, thread=thread_ref,
            name=str(getattr(thread, "name", "")), owner=owner,
            starter=starter_ref, newly_created=None,
            external_guild_id=self._optional_id(guild_id),
        )

    def _raw_thread(self, payload: object) -> tuple[ThreadRef, str | None, str | None]:
        """Read only raw ID/guild/parent metadata without cache inference."""
        data = getattr(payload, "data", None) or {}
        if not isinstance(data, dict):
            raise ValueError("raw thread payload data must be an object")
        guild = getattr(payload, "guild_id", data.get("guild_id"))
        parent_id = getattr(payload, "parent_id", data.get("parent_id"))
        thread_id = getattr(payload, "id", data.get("id"))
        channel = self._channel(parent_id, "unknown", guild)
        return ThreadRef(channel, self._id(thread_id, "thread")), data.get("name"), self._optional_id(guild)

    def raw_thread_updated(self, payload: object) -> ThreadUpdated:
        """Map a raw cached-or-uncached thread update without AP mutation effects."""
        thread, name, guild = self._raw_thread(payload)
        return ThreadUpdated(self.connector_key, thread, name=name, external_guild_id=guild)

    def raw_thread_deleted(self, payload: object) -> ThreadDeleted:
        """Map a raw delete while keeping thread ownership unresolved in core."""
        thread, _, guild = self._raw_thread(payload)
        return ThreadDeleted(self.connector_key, thread, external_guild_id=guild)
