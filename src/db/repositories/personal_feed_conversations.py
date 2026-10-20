"""Durable personal-feed conversation admission, ownership, and Discord delivery state."""

from __future__ import annotations

from .platform_surfaces import PlatformSurfaceRepository, discord_copy_query

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from typing import Callable, ContextManager

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import (
    InboundConversationObject,
    InboundDiscordDelivery,
    PersonalFeedThreadMapping,
    PublishedActivityObject,
    utcnow,
)


@dataclass(frozen=True, slots=True)
class DueDiscordDelivery:
    """Carry one claimed Discord render attempt outside the transaction."""

    delivery_id: int
    object_id: str
    actor_name: str | None
    content_markdown: str
    parent_object_id: str | None
    root_object_id: str
    root_actor_url: str
    root_channel_id: int
    root_message_id: int
    destination_thread_id: int | None
    discord_message_id: int | None
    deleted: bool


@dataclass(frozen=True, slots=True)
class InboundAdmission:
    """Describe one atomic inbound mutation admission decision."""

    status: str
    object_id: int | None = None
    delivery_id: int | None = None


class PersonalFeedConversationRepository:
    """Own canonical remote comments, deterministic thread mapping, and retry state."""

    def __init__(
        self,
        session_factory: Callable[[], ContextManager[Session]],
        immediate_session_factory: Callable[[], ContextManager[Session]],
    ) -> None:
        """Bind normal readers and SQLite immediate-writer transactions."""
        self.session = session_factory
        self.immediate_session = immediate_session_factory

    def adopt_thread(
        self,
        *,
        root_object_id: str,
        channel_id: int,
        root_message_id: int,
        thread_id: int,
    ) -> PersonalFeedThreadMapping:
        """Adopt Discord's deterministic thread identity for exactly one mapped root."""
        if thread_id != root_message_id:
            raise ValueError(
                "Discord public thread id must equal its source message id"
            )
        with self.immediate_session() as session:
            root = session.scalar(
                select(PublishedActivityObject).where(
                    PublishedActivityObject.object_id == root_object_id,
                    PublishedActivityObject.publication_context == "personal_feed",
                    PublishedActivityObject.kind == "post",
                )
            )
            if root is None:
                raise LookupError("personal-feed root not found")
            existing = session.scalar(
                discord_copy_query(PersonalFeedThreadMapping).where(
                    PersonalFeedThreadMapping.root_published_object_id == root.id
                )
            )
            if existing is not None:
                if existing.thread_id != thread_id:
                    raise ValueError("root already owns a different Discord thread")
                return existing
            row = PersonalFeedThreadMapping(
                **PlatformSurfaceRepository.discord_copy_location(session, channel_id),
                external_thread_id=str(thread_id),
                external_starter_message_id=str(root_message_id),
                root_published_object_id=root.id,
                root_discord_channel_id=channel_id,
                root_discord_message_id=root_message_id,
                thread_id=thread_id,
            )
            session.add(row)
            session.flush()
            return row

    def get_thread_by_thread_id(
        self, thread_id: int
    ) -> PersonalFeedThreadMapping | None:
        """Resolve a Discord thread to its personal-feed root mapping."""
        with self.session() as session:
            return session.scalar(
                discord_copy_query(PersonalFeedThreadMapping).where(
                    PersonalFeedThreadMapping.thread_id == thread_id
                )
            )

    def get_root_for_thread(self, thread_id: int) -> PublishedActivityObject | None:
        """Load the canonical personal-feed root owned by one Discord thread."""
        with self.session() as session:
            return session.scalar(
                select(PublishedActivityObject)
                .join(
                    PersonalFeedThreadMapping,
                    PersonalFeedThreadMapping.root_published_object_id
                    == PublishedActivityObject.id,
                )
                .where(PersonalFeedThreadMapping.thread_id == thread_id)
            )

    def resolve_conversation_object(
        self, object_id: str
    ) -> tuple[str, str, str | None] | None:
        """Resolve a bridge-owned or inbound object to context, root, and owner actor."""
        with self.session() as session:
            published = session.scalar(
                select(PublishedActivityObject).where(
                    PublishedActivityObject.object_id == object_id
                )
            )
            if published is not None:
                return (
                    published.publication_context,
                    published.conversation_root_object_id or published.object_id,
                    published.actor_url,
                )
            inbound = session.scalar(
                select(InboundConversationObject).where(
                    InboundConversationObject.object_id == object_id
                )
            )
            if inbound is not None:
                return (
                    inbound.publication_context,
                    inbound.conversation_root_object_id,
                    inbound.actor_id,
                )
            return None

    def get_inbound_object(self, object_id: str) -> InboundConversationObject | None:
        """Load one canonical remote-authored object by ActivityPub identity."""
        with self.session() as session:
            return session.scalar(
                select(InboundConversationObject).where(
                    InboundConversationObject.object_id == object_id
                )
            )

    def admit_create(
        self,
        *,
        activity_id: str,
        object_id: str,
        root_object_id: str,
        parent_object_id: str | None,
        activity_actor_id: str,
        attributed_to_actor_id: str,
        actor_name: str | None,
        content_markdown: str,
        published_at: datetime | None,
        source_activity: dict | None,
        destination_channel_id: int,
        parent_known: bool,
    ) -> InboundAdmission:
        """Atomically verify Create ownership and persist canonical state before Discord."""
        if activity_actor_id != attributed_to_actor_id:
            return InboundAdmission("actor_mismatch")
        # Incoming AP timestamps must identify UTC instants, not local wall time.
        try:
            published_at = self._utc_incoming(published_at)
        except ValueError:
            return InboundAdmission("invalid_timestamp")
        fingerprint = self._fingerprint(content_markdown, published_at, False)
        source_json = self._bounded_source(source_activity)
        with self.immediate_session() as session:
            root = session.scalar(
                select(PublishedActivityObject).where(
                    PublishedActivityObject.object_id == root_object_id,
                    PublishedActivityObject.publication_context == "personal_feed",
                )
            )
            if root is None:
                return InboundAdmission("unknown_root")
            existing = session.scalar(
                select(InboundConversationObject).where(
                    InboundConversationObject.object_id == object_id
                )
            )
            if existing is not None:
                if existing.actor_id != activity_actor_id:
                    return InboundAdmission("actor_mismatch", existing.id)
                delivery = session.scalar(
                    select(InboundDiscordDelivery).where(
                        InboundDiscordDelivery.inbound_object_id == existing.id,
                        InboundDiscordDelivery.root_published_object_id == root.id,
                    )
                )
                return InboundAdmission(
                    "duplicate", existing.id, delivery.id if delivery else None
                )
            row = InboundConversationObject(
                object_id=object_id,
                publication_context="personal_feed",
                activity_id=activity_id,
                conversation_root_object_id=root_object_id,
                parent_object_id=parent_object_id,
                actor_id=activity_actor_id,
                actor_name=actor_name,
                content_markdown=content_markdown,
                source_activity_json=source_json,
                published_at=published_at,
                revision_fingerprint=fingerprint,
            )
            session.add(row)
            session.flush()
            delivery = InboundDiscordDelivery(
                **PlatformSurfaceRepository.discord_copy_location(
                    session, destination_channel_id
                ),
                inbound_object_id=row.id,
                root_published_object_id=root.id,
                destination_channel_id=destination_channel_id,
                state="ready" if parent_known else "pending_parent",
                attempt_count=0,
                next_attempt_at=utcnow() if parent_known else None,
                last_applied_revision=fingerprint,
            )
            session.add(delivery)
            session.flush()
            return InboundAdmission("created", row.id, delivery.id)

    def retain_community_create(
        self,
        *,
        activity_id: str,
        object_id: str,
        root_object_id: str,
        parent_object_id: str | None,
        actor_id: str,
        actor_name: str | None,
        content_markdown: str,
        published_at: datetime | None,
        source_activity: dict | None,
    ) -> InboundAdmission:
        """Persist a canonical community comment before its existing Discord fanout."""
        try:
            published_at = self._utc_incoming(published_at)
        except ValueError:
            return InboundAdmission("invalid_timestamp")
        fingerprint = self._fingerprint(content_markdown, published_at, False)
        with self.immediate_session() as session:
            existing = session.scalar(
                select(InboundConversationObject).where(
                    InboundConversationObject.object_id == object_id
                )
            )
            if existing is not None:
                if existing.actor_id != actor_id:
                    return InboundAdmission("actor_mismatch", existing.id)
                return InboundAdmission("duplicate", existing.id)
            row = InboundConversationObject(
                object_id=object_id,
                publication_context="community",
                activity_id=activity_id,
                conversation_root_object_id=root_object_id,
                parent_object_id=parent_object_id,
                actor_id=actor_id,
                actor_name=actor_name,
                content_markdown=content_markdown,
                source_activity_json=self._bounded_source(source_activity),
                published_at=published_at,
                revision_fingerprint=fingerprint,
            )
            session.add(row)
            session.flush()
            return InboundAdmission("created", row.id)

    def admit_update(
        self,
        *,
        object_id: str,
        activity_actor_id: str,
        attributed_to_actor_id: str,
        content_markdown: str,
        updated_at: datetime | None,
        source_activity: dict | None,
    ) -> InboundAdmission:
        """Atomically enforce object ownership and ordering before accepting a remote Update."""
        if activity_actor_id != attributed_to_actor_id:
            return InboundAdmission("actor_mismatch")
        try:
            updated_at = self._utc_incoming(updated_at)
        except ValueError:
            return InboundAdmission("invalid_timestamp")
        fingerprint = self._fingerprint(content_markdown, updated_at, False)
        with self.immediate_session() as session:
            row = session.scalar(
                select(InboundConversationObject).where(
                    InboundConversationObject.object_id == object_id
                )
            )
            if row is None:
                return InboundAdmission("unknown_object")
            if row.actor_id != activity_actor_id:
                return InboundAdmission("actor_mismatch", row.id)
            if row.deleted_at is not None:
                return InboundAdmission("deleted", row.id)
            if row.revision_fingerprint == fingerprint:
                return InboundAdmission("duplicate", row.id)
            if updated_at is None or (
                row.updated_at_remote is not None
                and updated_at <= self._utc_stored(row.updated_at_remote)
            ):
                return InboundAdmission("stale", row.id)
            row.content_markdown = content_markdown
            row.updated_at_remote = updated_at
            row.revision_fingerprint = fingerprint
            row.source_activity_json = self._bounded_source(source_activity)
            delivery = session.scalar(
                select(InboundDiscordDelivery).where(
                    InboundDiscordDelivery.inbound_object_id == row.id
                )
            )
            if delivery is not None:
                delivery.last_applied_revision = fingerprint
                if delivery.discord_message_id is not None:
                    delivery.state = "ready"
                    delivery.attempt_count = 0
                    delivery.next_attempt_at = utcnow()
                elif delivery.state == "failed":
                    delivery.state = "ready"
                    delivery.attempt_count = 0
                    delivery.next_attempt_at = utcnow()
            return InboundAdmission(
                "accepted", row.id, delivery.id if delivery else None
            )

    def admit_delete(
        self, *, object_id: str, activity_actor_id: str
    ) -> InboundAdmission:
        """Atomically enforce stored actor ownership and make Delete terminal."""
        with self.immediate_session() as session:
            row = session.scalar(
                select(InboundConversationObject).where(
                    InboundConversationObject.object_id == object_id
                )
            )
            if row is None:
                return InboundAdmission("unknown_object")
            if row.actor_id != activity_actor_id:
                return InboundAdmission("actor_mismatch", row.id)
            if row.deleted_at is not None:
                return InboundAdmission("duplicate", row.id)
            row.deleted_at = utcnow()
            delivery = session.scalar(
                select(InboundDiscordDelivery).where(
                    InboundDiscordDelivery.inbound_object_id == row.id
                )
            )
            if delivery is not None:
                if delivery.discord_message_id is None:
                    delivery.state = "suppressed"
                    delivery.next_attempt_at = None
                else:
                    delivery.state = "ready"
                    delivery.attempt_count = 0
                    delivery.next_attempt_at = utcnow()
            return InboundAdmission(
                "accepted", row.id, delivery.id if delivery else None
            )

    def wake_children(self, parent_object_id: str) -> int:
        """Make waiting children due without consuming Discord attempt budget."""
        with self.immediate_session() as session:
            children = list(
                session.scalars(
                    select(InboundDiscordDelivery)
                    .join(
                        InboundConversationObject,
                        InboundConversationObject.id
                        == InboundDiscordDelivery.inbound_object_id,
                    )
                    .where(
                        InboundConversationObject.parent_object_id == parent_object_id,
                        InboundDiscordDelivery.state == "pending_parent",
                    )
                )
            )
            for child in children:
                child.state = "ready"
                child.next_attempt_at = utcnow()
            return len(children)

    def claim_due(self, *, limit: int = 20) -> list[DueDiscordDelivery]:
        """Claim due ready work without counting a Discord attempt yet."""
        now = utcnow()
        claims: list[DueDiscordDelivery] = []
        with self.immediate_session() as session:
            rows = list(
                session.execute(
                    select(
                        InboundDiscordDelivery,
                        InboundConversationObject,
                        PublishedActivityObject,
                    )
                    .join(
                        InboundConversationObject,
                        InboundConversationObject.id
                        == InboundDiscordDelivery.inbound_object_id,
                    )
                    .join(
                        PublishedActivityObject,
                        PublishedActivityObject.id
                        == InboundDiscordDelivery.root_published_object_id,
                    )
                    .where(
                        InboundDiscordDelivery.state == "ready",
                        (InboundDiscordDelivery.next_attempt_at.is_(None))
                        | (InboundDiscordDelivery.next_attempt_at <= now),
                    )
                    .order_by(InboundDiscordDelivery.id)
                    .limit(limit)
                )
            )
            for delivery, obj, root in rows:
                delivery.state = "dispatching"
                claims.append(
                    DueDiscordDelivery(
                        delivery_id=delivery.id,
                        object_id=obj.object_id,
                        actor_name=obj.actor_name,
                        content_markdown=obj.content_markdown,
                        parent_object_id=obj.parent_object_id,
                        root_object_id=root.object_id,
                        root_actor_url=root.actor_url,
                        root_channel_id=int(root.discord_channel_id),
                        root_message_id=int(root.discord_message_id),
                        destination_thread_id=delivery.destination_thread_id,
                        discord_message_id=delivery.discord_message_id,
                        deleted=obj.deleted_at is not None,
                    )
                )
        return claims

    def defer_pending_parent(self, delivery_id: int) -> None:
        """Return a claimed row to dependency waiting without spending attempts."""
        with self.immediate_session() as session:
            row = session.get(InboundDiscordDelivery, delivery_id)
            if row is not None and row.state == "dispatching":
                row.state = "pending_parent"
                row.next_attempt_at = None

    def suppress_delivery(self, delivery_id: int, reason: str) -> None:
        """Record an inactive destination without deleting canonical history."""
        with self.immediate_session() as session:
            row = session.get(InboundDiscordDelivery, delivery_id)
            if row is not None:
                row.state = "suppressed"
                row.next_attempt_at = None
                row.last_error = reason[:512]

    def set_delivery_thread(self, delivery_id: int, thread_id: int) -> None:
        """Persist the deterministic destination thread before message send."""
        with self.immediate_session() as session:
            row = session.get(InboundDiscordDelivery, delivery_id)
            if row is not None:
                row.destination_thread_id = thread_id
                row.external_thread_id = str(thread_id)

    def recover_dispatching(self) -> int:
        """Make interrupted side-effect claims retryable after process restart."""
        with self.immediate_session() as session:
            rows = list(
                session.scalars(
                    select(InboundDiscordDelivery).where(
                        InboundDiscordDelivery.state == "dispatching"
                    )
                )
            )
            for row in rows:
                row.state = "ready"
                row.next_attempt_at = utcnow()
            return len(rows)

    def record_discord_attempt(
        self,
        delivery_id: int,
        *,
        success: bool,
        discord_message_id: int | None = None,
        error: str | None = None,
    ) -> str:
        """Count only an actual Discord API call and enforce the three-attempt ceiling."""
        with self.immediate_session() as session:
            row = session.get(InboundDiscordDelivery, delivery_id)
            if row is None:
                raise LookupError("delivery not found")
            if row.state in {"suppressed", "failed", "delivered"}:
                return row.state
            row.attempt_count += 1
            if success:
                row.state = "delivered"
                if discord_message_id is not None:
                    row.discord_message_id = discord_message_id
                    row.external_message_id = str(discord_message_id)
                row.next_attempt_at = None
                row.last_error = None
            elif row.attempt_count >= 3:
                row.state = "failed"
                row.next_attempt_at = None
                row.last_error = (error or "discord delivery failed")[:512]
            else:
                row.state = "ready"
                delay = 10 if row.attempt_count == 1 else 30
                row.next_attempt_at = datetime.now(timezone.utc) + timedelta(
                    seconds=delay
                )
                row.last_error = (error or "discord delivery failed")[:512]
            return row.state

    @staticmethod
    def _utc_incoming(timestamp: datetime | None) -> datetime | None:
        """Normalize a supplied ActivityPub instant to UTC; never guess a missing offset."""
        if timestamp is None:
            return None
        if (not isinstance(timestamp, datetime) or timestamp.tzinfo is None
                or timestamp.utcoffset() is None):
            raise ValueError("incoming ActivityPub timestamp must be timezone-aware")
        return timestamp.astimezone(timezone.utc)

    @staticmethod
    def _utc_stored(timestamp: datetime) -> datetime:
        """Interpret SQLite's timezone-stripped stored timestamp as UTC."""
        # This database historically stores UTC, but SQLite's DateTime type
        # does not retain tzinfo after a write/read roundtrip.
        return (timestamp.replace(tzinfo=timezone.utc) if timestamp.tzinfo is None
                else timestamp.astimezone(timezone.utc))

    @staticmethod
    def _fingerprint(content: str, timestamp: datetime | None, deleted: bool) -> str:
        """Hash the UTC instant so equivalent offsets yield one revision."""
        instant = PersonalFeedConversationRepository._utc_incoming(timestamp)
        raw = f"{content}\0{instant.isoformat() if instant else ''}\0{int(deleted)}"
        return sha256(raw.encode()).hexdigest()

    @staticmethod
    def _bounded_source(source: dict | None) -> str | None:
        """Retain a bounded normalized diagnostic payload rather than arbitrary input."""
        if source is None:
            return None
        encoded = json.dumps(source, sort_keys=True, separators=(",", ":"))
        return encoded[:16384]
