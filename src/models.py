"""SQLAlchemy models for bridge routing, identity, and federation state."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    """Return a timezone-aware UTC timestamp for persisted records."""
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    """Base class for all ORM models used by the bridge."""
    pass


class PostLink(Base):
    """Map one Lemmy post to one Discord forum thread for runtime routing."""

    # PostLink records the one-to-one bridge mapping between a Lemmy post and
    # the Discord forum thread created for it.
    __tablename__ = "post_links"
    __table_args__ = (
        UniqueConstraint("lemmy_post_id"),
        UniqueConstraint("discord_forum_thread_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lemmy_post_id: Mapped[int] = mapped_column(Integer, nullable=False)
    lemmy_post_ap_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    discord_forum_thread_id: Mapped[int] = mapped_column(Integer, nullable=False)
    discord_starter_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    direction: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class CommentLink(Base):
    """Map one Lemmy comment to one Discord message for runtime routing."""

    # CommentLink preserves message-level deduplication and parent context for
    # comment sync in both directions.
    __tablename__ = "comment_links"
    __table_args__ = (
        UniqueConstraint("lemmy_comment_id"),
        UniqueConstraint("discord_message_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lemmy_comment_id: Mapped[int] = mapped_column(Integer, nullable=False)
    lemmy_comment_ap_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    lemmy_parent_comment_ap_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    lemmy_post_id: Mapped[int] = mapped_column(Integer, nullable=False)
    discord_forum_thread_id: Mapped[int] = mapped_column(Integer, nullable=False)
    discord_message_id: Mapped[int] = mapped_column(Integer, nullable=False)
    direction: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class ActivityPubEventReceipt(Base):
    """Persist inbound delivery state so ActivityPub handling stays idempotent."""

    # Receipts make inbound ActivityPub processing idempotent across retries,
    # duplicates, and partial failures.
    __tablename__ = "activitypub_event_receipts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    delivery_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    object_ap_id: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    detail: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class ChannelCommunitySubscription(Base):
    """Store the Discord-channel to Lemmy-community binding and follow state."""

    # ChannelCommunitySubscription binds one Discord forum channel to one Lemmy
    # community. The older Lemmy-named fields stay in place because the current
    # runtime still routes thread/comment sync through them, while the new
    # follow-state fields prepare the Stage 3+ federation flow.
    __tablename__ = "channel_community_subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    discord_channel_id: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    lemmy_community_actor_id: Mapped[str] = mapped_column(String(512), nullable=False)
    lemmy_community_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    lemmy_community_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    community_handle: Mapped[str | None] = mapped_column(String(255), nullable=True)
    community_inbox_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    follow_activity_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class User(Base):
    """Store the local ActivityPub identity owned by one Discord user."""

    # User is the shared source of truth for Discord-to-ActivityPub ownership,
    # including the actor URLs and keypair material needed by later publish
    # flows. Stage 2 stores the keys directly here to avoid split ownership.
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("discord_user_id"),
        UniqueConstraint("activitypub_username"),
        UniqueConstraint("actor_url"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    discord_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    activitypub_username: Mapped[str] = mapped_column(String(255), nullable=False)
    actor_url: Mapped[str] = mapped_column(String(512), nullable=False)
    inbox_url: Mapped[str] = mapped_column(String(512), nullable=False)
    outbox_url: Mapped[str] = mapped_column(String(512), nullable=False)
    followers_url: Mapped[str] = mapped_column(String(512), nullable=False)
    public_key_pem: Mapped[str] = mapped_column(String, nullable=False)
    private_key_pem: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class MessageMapping(Base):
    """Store generic dedup and source mapping for ActivityPub bridge traffic."""

    # MessageMapping is intentionally more general than PostLink/CommentLink.
    # It captures the source IDs and AP object/activity IDs needed to suppress
    # echo loops once user-authored ActivityPub publishes are added.
    __tablename__ = "message_mappings"
    __table_args__ = (
        UniqueConstraint("source_platform", "source_id"),
        UniqueConstraint("activity_id"),
        UniqueConstraint("object_id"),
        UniqueConstraint("discord_message_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_platform: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[str] = mapped_column(String(255), nullable=False)
    activity_id: Mapped[str] = mapped_column(String(512), nullable=False)
    object_id: Mapped[str] = mapped_column(String(512), nullable=False)
    actor_url: Mapped[str] = mapped_column(String(512), nullable=False)
    community_actor_url: Mapped[str] = mapped_column(String(512), nullable=False)
    discord_channel_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    discord_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class RemoteActor(Base):
    """Cache remote actor metadata needed for verification and delivery."""

    # RemoteActor keeps the last fetched addressing and public-key data for a
    # remote AP actor so later stages can verify signatures and choose the
    # correct inbox without refetching actor documents on every event.
    __tablename__ = "remote_actors"
    __table_args__ = (UniqueConstraint("actor_url"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor_url: Mapped[str] = mapped_column(String(512), nullable=False)
    preferred_username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    inbox_url: Mapped[str] = mapped_column(String(512), nullable=False)
    shared_inbox_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    public_key_pem: Mapped[str] = mapped_column(String, nullable=False)
    last_fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
