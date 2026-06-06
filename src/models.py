"""SQLAlchemy models for bridge routing, identity, and federation state."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    """Return a timezone-aware UTC timestamp for persisted records."""
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    """Base class for all ORM models used by the bridge."""
    pass


class PostLink(Base):
    """Map one Lemmy post copy to one Discord forum thread for runtime routing."""

    # PostLink records one Discord delivery target for a Lemmy post. A remote
    # community may fan out into several subscribed Discord forum channels, so
    # one Lemmy post can now own several PostLink rows at once.
    __tablename__ = "post_links"
    __table_args__ = (
        UniqueConstraint("lemmy_post_id", "discord_forum_channel_id"),
        UniqueConstraint("discord_forum_thread_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lemmy_post_id: Mapped[int] = mapped_column(Integer, nullable=False)
    lemmy_post_ap_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    discord_forum_channel_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    discord_forum_thread_id: Mapped[int] = mapped_column(Integer, nullable=False)
    discord_starter_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    direction: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class CommentLink(Base):
    """Map one Lemmy comment copy to one Discord message for runtime routing."""

    # CommentLink preserves message-level deduplication and parent context for
    # comment sync in both directions. One remote comment may fan out into
    # several Discord threads, so uniqueness is per target thread/message.
    __tablename__ = "comment_links"
    __table_args__ = (
        UniqueConstraint("lemmy_comment_id", "discord_forum_thread_id"),
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
    outcome: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detail: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class DiscordGuildSnapshot(Base):
    """Cache one Discord guild name for public dashboard placement labels."""

    # The dashboard is public and must not resolve names through Discord APIs on
    # request. This row stores the last-known public-readable guild label only.
    __tablename__ = "discord_guild_snapshots"
    __table_args__ = (UniqueConstraint("discord_guild_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    discord_guild_id: Mapped[int] = mapped_column(Integer, nullable=False)
    guild_name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class DiscordChannelSnapshot(Base):
    """Cache one Discord channel name for public dashboard placement labels."""

    # Channel snapshots are generic enough to record a type, but dashboard
    # rendering currently consumes forum-backed rows only. No foreign keys are
    # used so this additive table follows the surrounding schema style.
    __tablename__ = "discord_channel_snapshots"
    __table_args__ = (UniqueConstraint("discord_channel_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    discord_channel_id: Mapped[int] = mapped_column(Integer, nullable=False)
    discord_guild_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    channel_name: Mapped[str] = mapped_column(String(255), nullable=False)
    channel_type: Mapped[str] = mapped_column(String(64), nullable=False)
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
    discord_guild_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    lemmy_community_actor_id: Mapped[str] = mapped_column(String(512), nullable=False)
    lemmy_community_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    lemmy_community_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    community_handle: Mapped[str | None] = mapped_column(String(255), nullable=True)
    community_inbox_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    follow_activity_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    initiated_by_discord_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
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


class RegistrationSession(Base):
    """Persist one browser-side registration flow across Discord redirects."""

    # RegistrationSession keeps only the state needed to safely resume the
    # OAuth-based web flow after Discord redirects the browser back here.
    __tablename__ = "registration_sessions"
    __table_args__ = (UniqueConstraint("session_token"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_token: Mapped[str] = mapped_column(String(255), nullable=False)
    oauth_state: Mapped[str | None] = mapped_column(String(255), nullable=True)
    discord_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    discord_username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    discord_avatar_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    activitypub_username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="started")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
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


class PublishedActivityObject(Base):
    """Persist one gateway-published ActivityPub object for later resolution."""

    # PublishedActivityObject is the durable source of truth for local AP
    # object URLs. Reply-chain resolution and canonical object routes rely on
    # this table instead of transient in-memory caches.
    __tablename__ = "published_activity_objects"
    __table_args__ = (
        UniqueConstraint("activity_id"),
        UniqueConstraint("object_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor_username: Mapped[str] = mapped_column(String(255), nullable=False)
    actor_url: Mapped[str] = mapped_column(String(512), nullable=False)
    community_actor_url: Mapped[str] = mapped_column(String(512), nullable=False)
    activity_id: Mapped[str] = mapped_column(String(512), nullable=False)
    object_id: Mapped[str] = mapped_column(String(512), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    body_markdown: Mapped[str] = mapped_column(String, nullable=False)
    in_reply_to_object_id: Mapped[str | None] = mapped_column(
        String(512), nullable=True
    )
    discord_channel_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    discord_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class CommunityThreadGroup(Base):
    """Map one canonical source thread event to all its Discord delivery targets."""

    # CommunityThreadGroup is the Phase 2+ replacement for PostLink. It records
    # the canonical source event (AP IDs, source channel/thread) and lets each
    # subscribed channel own a separate CommunityThreadGroupDelivery row.
    # source_* fields are nullable to accommodate inbound AP events, which have
    # no source Discord channel — threads are created in all subscribed channels.
    __tablename__ = "community_thread_groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    community_actor_id: Mapped[str] = mapped_column(String(512), nullable=False)
    source_channel_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_thread_id: Mapped[int | None] = mapped_column(Integer, nullable=True, unique=True)
    source_starter_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ap_activity_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    ap_object_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class CommunityThreadGroupDelivery(Base):
    """Record one per-channel Discord delivery for a CommunityThreadGroup."""

    # Each subscribed channel gets its own delivery row so fanout and retry
    # logic can track which channels received the thread and which still need it.
    # UNIQUE(thread_group_id, discord_channel_id) ensures one delivery per channel per group.
    __tablename__ = "community_thread_group_deliveries"
    __table_args__ = (
        UniqueConstraint("thread_group_id", "discord_channel_id"),
        UniqueConstraint("discord_thread_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    thread_group_id: Mapped[int] = mapped_column(Integer, nullable=False)
    discord_channel_id: Mapped[int] = mapped_column(Integer, nullable=False)
    discord_thread_id: Mapped[int] = mapped_column(Integer, nullable=False)
    discord_starter_message_id: Mapped[int] = mapped_column(Integer, nullable=False)
    # "source" for the originating channel; "mirror" for fanout delivery targets.
    role: Mapped[str] = mapped_column(String(32), nullable=False)


class CommunityMessageGroup(Base):
    """Map one canonical source message event to all its Discord delivery targets."""

    # CommunityMessageGroup is the Phase 2+ replacement for CommentLink. It
    # records the canonical source message (AP IDs, source context) and lets
    # each delivery channel own a CommunityMessageGroupDelivery row.
    # source_* fields are nullable to accommodate inbound AP events, which have
    # no source Discord message — the message is created in all subscribed threads.
    __tablename__ = "community_message_groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    community_actor_id: Mapped[str] = mapped_column(String(512), nullable=False)
    thread_group_id: Mapped[int] = mapped_column(Integer, nullable=False)
    source_channel_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_thread_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ap_activity_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    ap_object_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # FK to another CommunityMessageGroup if this message is a reply.
    parent_message_group_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class CommunityMessageGroupDelivery(Base):
    """Record one per-channel Discord delivery for a CommunityMessageGroup."""

    # Each delivery channel gets its own row so fanout and retry logic can track
    # which threads received the message.
    # UNIQUE(message_group_id, discord_channel_id) ensures one delivery per channel per group.
    __tablename__ = "community_message_group_deliveries"
    __table_args__ = (
        UniqueConstraint("message_group_id", "discord_channel_id"),
        UniqueConstraint("discord_message_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    message_group_id: Mapped[int] = mapped_column(Integer, nullable=False)
    discord_channel_id: Mapped[int] = mapped_column(Integer, nullable=False)
    discord_thread_id: Mapped[int] = mapped_column(Integer, nullable=False)
    discord_message_id: Mapped[int] = mapped_column(Integer, nullable=False)
    # "source" for the originating channel; "mirror" for fanout delivery targets.
    role: Mapped[str] = mapped_column(String(32), nullable=False)


class BridgeActorFollow(Base):
    """Track one AP-level Follow from the bridge actor to a remote community.

    This table owns the network-level fact of whether our bridge actor is
    following a community. It is independent of Discord channel subscriptions:
    multiple channels can subscribe to the same community while only one
    BridgeActorFollow row exists. The row is deleted only when the last
    channel subscription for that community is removed and an Undo(Follow)
    has been dispatched.
    """

    # status values: pending | accepted | failed
    __tablename__ = "bridge_actor_follows"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Canonical AP actor URL for the remote community — UNIQUE enforces the
    # one-follow-per-community invariant at the DB level.
    community_actor_id: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    follow_activity_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    community_inbox_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


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


class LocalCommunity(Base):
    """Persist one Discord forum exposed as a local federated community.

    LocalCommunity is the Python-owned source of truth for the community actor
    metadata that the gateway exposes publicly. The row binds one Discord forum
    channel to one stable actor URL and one stable keypair. Creator ownership is
    stored as a Discord user id for local bridge management commands only; it is
    not an ActivityPub identity.
    """

    __tablename__ = "local_communities"
    __table_args__ = (
        UniqueConstraint("discord_forum_channel_id"),
        UniqueConstraint("slug"),
        UniqueConstraint("actor_url"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    discord_guild_id: Mapped[int] = mapped_column(Integer, nullable=False)
    discord_forum_channel_id: Mapped[int] = mapped_column(Integer, nullable=False)
    slug: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    summary: Mapped[str | None] = mapped_column(String, nullable=True)
    created_by_discord_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    actor_url: Mapped[str] = mapped_column(String(512), nullable=False)
    inbox_url: Mapped[str] = mapped_column(String(512), nullable=False)
    outbox_url: Mapped[str] = mapped_column(String(512), nullable=False)
    followers_url: Mapped[str] = mapped_column(String(512), nullable=False)
    public_key_pem: Mapped[str] = mapped_column(String, nullable=False)
    private_key_pem: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)




class CommunityActorBan(Base):
    """Record one local-only remote actor ban scoped to one local community.

    This is bridge moderation state, not a federated ActivityPub Block. V1 uses
    it only to acknowledge and skip future inbound events before side effects.
    """

    __tablename__ = "community_actor_bans"
    __table_args__ = (
        UniqueConstraint("local_community_id", "actor_handle", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    local_community_id: Mapped[int] = mapped_column(Integer, nullable=False)
    actor_handle: Mapped[str] = mapped_column(String(255), nullable=False)
    actor_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    created_by_discord_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class ManagementAuditEvent(Base):
    """Backend audit trail for management operations.

    Rows in this table record successful management mutations and selected
    authorization denials. They are not inbound ActivityPub receipt rows and
    they are not a general application log.
    """

    __tablename__ = "management_audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    result: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    reason_code: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    actor_discord_user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    local_community_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    target_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    target_id: Mapped[str | None] = mapped_column(String(512), nullable=True, index=True)
    before_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    after_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)


class RemoteSubscriber(Base):
    """Persist one remote ActivityPub subscriber for a local community.

    Remote-subscriber rows are owned by Python because moderation and retry
    policy live there, while the gateway only owns the protocol-side Accept
    delivery.
    """

    __tablename__ = "remote_subscribers"
    __table_args__ = (
        UniqueConstraint("local_community_id", "remote_actor_id"),
        UniqueConstraint("follow_activity_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    local_community_id: Mapped[int] = mapped_column(Integer, nullable=False)
    remote_actor_id: Mapped[str] = mapped_column(String(512), nullable=False)
    remote_inbox_url: Mapped[str] = mapped_column(String(512), nullable=False)
    follow_activity_id: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="accepted")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class LocalSubscriber(Base):
    """Persist one same-instance Discord forum subscribed to a local community.

    Local-subscriber rows are control-plane state in Stage 1. Later stages may
    use them for participant sync, but this model itself does not imply any
    runtime routing behavior.
    """

    __tablename__ = "local_subscribers"
    __table_args__ = (
        UniqueConstraint("local_community_id", "discord_channel_id"),
        UniqueConstraint("discord_channel_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    local_community_id: Mapped[int] = mapped_column(Integer, nullable=False)
    discord_guild_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    discord_channel_id: Mapped[int] = mapped_column(Integer, nullable=False)
    initiated_by_discord_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

class LocalCommunityRelaySourceActivity(Base):
    """Store one immutable inbound ActivityPub source activity for relay.

    The source row is the durable payload reference used by retries and
    duplicate recovery. Delivery rows point here instead of duplicating large
    JSON blobs for every target follower.
    """

    __tablename__ = "local_community_relay_source_activities"
    __table_args__ = (
        UniqueConstraint("local_community_id", "operation", "source_object_ap_id", "source_activity_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    local_community_id: Mapped[int] = mapped_column(Integer, nullable=False)
    object_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    operation: Mapped[str] = mapped_column(String(32), nullable=False)
    source_object_ap_id: Mapped[str] = mapped_column(String(512), nullable=False)
    source_activity_id: Mapped[str] = mapped_column(String(512), nullable=False)
    source_announce_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    origin_remote_actor_id: Mapped[str] = mapped_column(String(512), nullable=False)
    source_activity_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class LocalCommunityRelayDelivery(Base):
    """Track one relay attempt target for a local-community source activity.

    Rows are operation-aware so create, update, and delete delivery outcomes do
    not overwrite each other. This state lets later update/delete fanout target
    only followers that actually received the original create.
    """

    __tablename__ = "local_community_relay_deliveries"
    __table_args__ = (
        UniqueConstraint("source_activity_row_id", "target_remote_actor_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_activity_row_id: Mapped[int] = mapped_column(Integer, nullable=False)
    local_community_id: Mapped[int] = mapped_column(Integer, nullable=False)
    object_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    operation: Mapped[str] = mapped_column(String(32), nullable=False)
    source_object_ap_id: Mapped[str] = mapped_column(String(512), nullable=False)
    source_activity_id: Mapped[str] = mapped_column(String(512), nullable=False)
    origin_remote_actor_id: Mapped[str] = mapped_column(String(512), nullable=False)
    target_remote_actor_id: Mapped[str] = mapped_column(String(512), nullable=False)
    target_inbox_url: Mapped[str] = mapped_column(String(512), nullable=False)
    delivery_profile: Mapped[str] = mapped_column(String(64), nullable=False, default="threadiverse_group")
    relay_activity_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class LocalCommunityThread(Base):
    """Map one canonical local-community thread in either federation direction.

    Threads are the canonical post-level identity rows for local-community
    mode. Stage 2 deliberately removes direct Discord ownership from this row
    so one canonical thread can later fan out onto several Discord surfaces.
    """

    __tablename__ = "local_community_threads"
    __table_args__ = (
        UniqueConstraint("ap_activity_id"),
        UniqueConstraint("ap_object_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    local_community_id: Mapped[int] = mapped_column(Integer, nullable=False)
    ap_activity_id: Mapped[str] = mapped_column(String(512), nullable=False)
    ap_object_id: Mapped[str] = mapped_column(String(512), nullable=False)
    direction: Mapped[str] = mapped_column(String(32), nullable=False)
    origin_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class LocalCommunityMessage(Base):
    """Map one canonical local-community message or remote comment.

    Message rows preserve the canonical AP identity and AP-parent linkage for
    one local-community comment. Discord-parent linkage moves to per-surface
    rows in Stage 2 because later stages will need different parent message ids
    on different Discord forum surfaces.
    """

    __tablename__ = "local_community_messages"
    __table_args__ = (
        UniqueConstraint("ap_activity_id"),
        UniqueConstraint("ap_object_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    local_community_thread_id: Mapped[int] = mapped_column(Integer, nullable=False)
    ap_activity_id: Mapped[str] = mapped_column(String(512), nullable=False)
    ap_object_id: Mapped[str] = mapped_column(String(512), nullable=False)
    parent_ap_object_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    direction: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class LocalCommunityThreadSurface(Base):
    """Map one canonical local-community thread onto one Discord forum surface.

    Stage 2 introduces explicit surface rows so host-forum behavior can keep
    working while later stages add more local Discord targets for the same
    canonical thread.
    """

    __tablename__ = "local_community_thread_surfaces"
    __table_args__ = (
        UniqueConstraint("local_community_thread_id", "discord_forum_channel_id"),
        UniqueConstraint("discord_thread_id"),
        UniqueConstraint("discord_starter_message_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    local_community_thread_id: Mapped[int] = mapped_column(Integer, nullable=False)
    discord_forum_channel_id: Mapped[int] = mapped_column(Integer, nullable=False)
    discord_thread_id: Mapped[int] = mapped_column(Integer, nullable=False)
    discord_starter_message_id: Mapped[int] = mapped_column(Integer, nullable=False)
    # Host surfaces keep this empty. Local-subscriber surfaces store the row
    # that selected the target forum, preserving historical target identity.
    local_subscriber_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class LocalCommunityMessageSurface(Base):
    """Map one canonical local-community message onto one Discord message surface.

    Message surfaces stay bound to a concrete thread surface so nested reply
    routing can remain local to one Discord thread even after later stages add
    several sibling forum surfaces for the same canonical community activity.
    """

    __tablename__ = "local_community_message_surfaces"
    __table_args__ = (
        UniqueConstraint("local_community_message_id", "local_community_thread_surface_id"),
        UniqueConstraint("discord_message_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    local_community_message_id: Mapped[int] = mapped_column(Integer, nullable=False)
    local_community_thread_surface_id: Mapped[int] = mapped_column(Integer, nullable=False)
    discord_forum_channel_id: Mapped[int] = mapped_column(Integer, nullable=False)
    discord_message_id: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_discord_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Host surfaces keep this empty. Local-subscriber surfaces store the row
    # that selected the target forum, preserving historical target identity.
    local_subscriber_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
