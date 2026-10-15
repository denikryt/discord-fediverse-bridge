"""SQLAlchemy models for bridge routing, identity, and federation state."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Computed,
    ForeignKeyConstraint,
    Index,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    """Return a timezone-aware UTC timestamp for persisted records."""
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    """Base class for all ORM models used by the bridge."""

    pass


class PlatformConnector(Base):
    """Persist immutable connection identity without credentials or routing state."""

    __tablename__ = "platform_connectors"
    __table_args__ = (
        UniqueConstraint("connector_key"),
        CheckConstraint(
            "platform IN ('discord', 'fluxer')", name="ck_connector_platform"
        ),
        CheckConstraint(
            "(platform = 'discord' AND instance_origin IS NULL) OR (platform = 'fluxer' AND instance_origin IS NOT NULL AND length(trim(instance_origin)) > 0)",
            name="ck_connector_origin",
        ),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    connector_key: Mapped[str] = mapped_column(String(255), nullable=False)
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    instance_origin: Mapped[str | None] = mapped_column(String(512), nullable=True)


class ChannelSurface(Base):
    """Keep a durable physical channel and at most one typed current owner.

    Content references survive release. Only owner FKs change claim state;
    SQLite/PostgreSQL compute the SQL-readable role without synchronization.
    """

    __tablename__ = "platform_channel_surfaces"
    __table_args__ = (
        UniqueConstraint("connector_id", "external_channel_id"),
        UniqueConstraint("connector_id", "id"),
        UniqueConstraint("local_community_id"),
        UniqueConstraint("local_subscriber_id"),
        UniqueConstraint("remote_subscription_id"),
        UniqueConstraint("personal_feed_binding_id"),
        CheckConstraint(
            "kind IN ('forum','text','announcement','unknown')", name="ck_surface_kind"
        ),
        # Integer CASE is total-valued and portable; Boolean addition is not.
        CheckConstraint(
            "(CASE WHEN local_community_id IS NOT NULL THEN 1 ELSE 0 END) + (CASE WHEN local_subscriber_id IS NOT NULL THEN 1 ELSE 0 END) + (CASE WHEN remote_subscription_id IS NOT NULL THEN 1 ELSE 0 END) + (CASE WHEN personal_feed_binding_id IS NOT NULL THEN 1 ELSE 0 END) <= 1",
            name="ck_surface_one_owner",
        ),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    connector_id: Mapped[int] = mapped_column(
        ForeignKey("platform_connectors.id", ondelete="RESTRICT"), nullable=False
    )
    external_channel_id: Mapped[str] = mapped_column(Text, nullable=False)
    external_guild_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    local_community_id: Mapped[int | None] = mapped_column(
        ForeignKey("local_communities.id", ondelete="RESTRICT"), nullable=True
    )
    local_subscriber_id: Mapped[int | None] = mapped_column(
        ForeignKey("local_subscribers.id", ondelete="RESTRICT"), nullable=True
    )
    remote_subscription_id: Mapped[int | None] = mapped_column(
        ForeignKey("channel_community_subscriptions.id", ondelete="RESTRICT"),
        nullable=True,
    )
    personal_feed_binding_id: Mapped[int | None] = mapped_column(
        ForeignKey("personal_feed_bindings.id", ondelete="RESTRICT"), nullable=True
    )
    role: Mapped[str | None] = mapped_column(
        String(32),
        Computed(
            "CASE WHEN local_community_id IS NOT NULL THEN 'local_host' WHEN local_subscriber_id IS NOT NULL THEN 'local_subscriber' WHEN remote_subscription_id IS NOT NULL THEN 'remote_subscription' WHEN personal_feed_binding_id IS NOT NULL THEN 'personal_feed' ELSE NULL END",
            persisted=True,
        ),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class BridgeActorKey(Base):
    """Persist the single bridge service actor signing keypair."""

    __tablename__ = "bridge_actor_keys"
    __table_args__ = (
        UniqueConstraint("actor_url"),
        UniqueConstraint("key_id"),
        CheckConstraint("id = 1", name="ck_bridge_actor_keys_singleton"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor_url: Mapped[str] = mapped_column(String(512), nullable=False)
    key_id: Mapped[str] = mapped_column(String(768), nullable=False)
    key_format: Mapped[str] = mapped_column(String(16), nullable=False)
    algorithm: Mapped[str] = mapped_column(String(64), nullable=False)
    public_key_data: Mapped[str] = mapped_column(Text, nullable=False)
    private_key_data: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    def __repr__(self) -> str:
        """Represent metadata only so private key material cannot leak to logs."""
        return (
            f"BridgeActorKey(id={self.id!r}, actor_url={self.actor_url!r}, "
            f"key_format={self.key_format!r}, algorithm={self.algorithm!r})"
        )


class PostLink(Base):
    """Map one Lemmy post copy to one Discord forum thread for runtime routing."""

    # PostLink records one Discord delivery target for a Lemmy post. A remote
    # community may fan out into several subscribed Discord forum channels, so
    # one Lemmy post can now own several PostLink rows at once.
    __tablename__ = "post_links"
    __table_args__ = (
        ForeignKeyConstraint(
            ["connector_id", "channel_surface_id"],
            ["platform_channel_surfaces.connector_id", "platform_channel_surfaces.id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        UniqueConstraint("connector_id", "external_thread_id"),
        UniqueConstraint("connector_id", "external_starter_message_id"),
        UniqueConstraint("lemmy_post_id", "channel_surface_id"),
        UniqueConstraint("lemmy_post_id", "discord_forum_channel_id"),
    )

    connector_id: Mapped[int] = mapped_column(
        ForeignKey("platform_connectors.id", ondelete="RESTRICT"), nullable=False
    )
    channel_surface_id: Mapped[int] = mapped_column(Integer, nullable=False)
    external_thread_id: Mapped[str] = mapped_column(Text, nullable=False)
    external_starter_message_id: Mapped[str | None] = mapped_column(Text, nullable=True)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lemmy_post_id: Mapped[int] = mapped_column(Integer, nullable=False)
    lemmy_post_ap_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # MULTIPLATFORM_STAGE01_TEMP: Discord content ID columns remain only for pre-adapter callers; scoped IDs are authoritative.
    discord_forum_channel_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    discord_forum_thread_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    discord_starter_message_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    direction: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class CommentLink(Base):
    """Map one Lemmy comment copy to one Discord message for runtime routing."""

    # CommentLink preserves message-level deduplication and parent context for
    # comment sync in both directions. One remote comment may fan out into
    # several Discord threads, so uniqueness is per target thread/message.
    __tablename__ = "comment_links"
    __table_args__ = (
        ForeignKeyConstraint(
            ["connector_id", "channel_surface_id"],
            ["platform_channel_surfaces.connector_id", "platform_channel_surfaces.id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        UniqueConstraint("connector_id", "external_message_id"),
        UniqueConstraint("lemmy_comment_id", "discord_forum_thread_id"),
    )

    connector_id: Mapped[int] = mapped_column(
        ForeignKey("platform_connectors.id", ondelete="RESTRICT"), nullable=False
    )
    channel_surface_id: Mapped[int] = mapped_column(Integer, nullable=False)
    external_thread_id: Mapped[str] = mapped_column(Text, nullable=False)
    external_message_id: Mapped[str] = mapped_column(Text, nullable=False)
    external_parent_message_id: Mapped[str | None] = mapped_column(Text, nullable=True)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lemmy_comment_id: Mapped[int] = mapped_column(Integer, nullable=False)
    lemmy_comment_ap_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    lemmy_parent_comment_ap_id: Mapped[str | None] = mapped_column(
        String(512), nullable=True
    )
    lemmy_post_id: Mapped[int] = mapped_column(Integer, nullable=False)
    # MULTIPLATFORM_STAGE01_TEMP: Discord content ID columns remain only for pre-adapter callers; scoped IDs are authoritative.
    discord_forum_thread_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    discord_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    direction: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


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
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class DiscordGuildSnapshot(Base):
    """Cache one Discord guild name for public dashboard placement labels."""

    # The dashboard is public and must not resolve names through Discord APIs on
    # request. This row stores the last-known public-readable guild label only.
    __tablename__ = "discord_guild_snapshots"
    __table_args__ = (UniqueConstraint("discord_guild_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    discord_guild_id: Mapped[int] = mapped_column(Integer, nullable=False)
    guild_name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


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
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class ChannelCommunitySubscription(Base):
    """Store the Discord-channel to Lemmy-community binding and follow state."""

    # ChannelCommunitySubscription binds one Discord forum channel to one Lemmy
    # community. The older Lemmy-named fields stay in place because the current
    # runtime still routes thread/comment sync through them, while the new
    # follow-state fields prepare the Stage 3+ federation flow.
    __tablename__ = "channel_community_subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # MULTIPLATFORM_STAGE01_TEMP: Nullable Discord subscription location projection is for the legacy Discord runtime only.
    discord_channel_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True, unique=True
    )
    discord_guild_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    lemmy_community_actor_id: Mapped[str] = mapped_column(String(512), nullable=False)
    lemmy_community_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    lemmy_community_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    community_handle: Mapped[str | None] = mapped_column(String(255), nullable=True)
    community_inbox_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    follow_activity_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    initiated_by_discord_user_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class ProfileMediaAsset(Base):
    """Persist metadata for one immutable bridge-owned profile image."""

    __tablename__ = "profile_media_assets"
    __table_args__ = (
        UniqueConstraint("public_id"),
        UniqueConstraint("storage_key"),
        CheckConstraint(
            "media_kind IN ('avatar', 'header')", name="ck_profile_media_kind"
        ),
        CheckConstraint("byte_size > 0", name="ck_profile_media_byte_size"),
        CheckConstraint("width > 0 AND height > 0", name="ck_profile_media_dimensions"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(255), nullable=False)
    media_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    content_type: Mapped[str] = mapped_column(String(64), nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256_hex: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


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
    display_name: Mapped[str | None] = mapped_column(String(30), nullable=True)
    summary: Mapped[str | None] = mapped_column(String(500), nullable=True)
    avatar_media_id: Mapped[int | None] = mapped_column(
        ForeignKey("profile_media_assets.id", ondelete="RESTRICT"), nullable=True
    )
    header_media_id: Mapped[int | None] = mapped_column(
        ForeignKey("profile_media_assets.id", ondelete="RESTRICT"), nullable=True
    )
    profile_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class PersonalFeedBinding(Base):
    """Persist the single current Discord text-channel binding for one User Actor."""

    __tablename__ = "personal_feed_bindings"
    __table_args__ = (
        CheckConstraint(
            "CASE WHEN discord_guild_id IS NULL AND discord_channel_id IS NULL THEN 1 WHEN discord_guild_id IS NOT NULL AND discord_channel_id IS NOT NULL THEN 1 ELSE 0 END",
            name="ck_personal_feed_location_pair",
        ),
        UniqueConstraint("user_id"),
        UniqueConstraint("discord_guild_id"),
        UniqueConstraint("discord_channel_id"),
        CheckConstraint("discord_guild_id > 0", name="ck_personal_feed_guild_positive"),
        CheckConstraint(
            "discord_channel_id > 0", name="ck_personal_feed_channel_positive"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # MULTIPLATFORM_STAGE01_TEMP: Discord location projection remains for existing publication and personal-feed contracts.
    discord_guild_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    discord_channel_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class UserActorFollower(Base):
    """Persist one accepted remote follower relationship for a local User Actor."""

    __tablename__ = "user_actor_followers"
    __table_args__ = (
        UniqueConstraint("user_id", "remote_actor_id"),
        CheckConstraint("status = 'accepted'", name="ck_user_actor_follower_status"),
        CheckConstraint(
            "owner_notification_state IN ('pending', 'attempted')",
            name="ck_user_actor_follower_notification_state",
        ),
        Index("ix_user_actor_followers_collection", "user_id", "status", "id"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    remote_actor_id: Mapped[str] = mapped_column(String(512), nullable=False)
    remote_inbox_url: Mapped[str] = mapped_column(String(512), nullable=False)
    remote_shared_inbox_url: Mapped[str | None] = mapped_column(
        String(512), nullable=True
    )
    follow_activity_id: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="accepted")
    owner_notification_state: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending"
    )
    owner_notification_attempted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class UserActorFollowActivityId(Base):
    """Retain immutable global ownership of every observed User Actor Follow ID."""

    __tablename__ = "user_actor_follow_activity_ids"
    __table_args__ = (
        UniqueConstraint("follow_activity_id"),
        Index("ix_user_actor_follow_activity_owner", "user_id", "remote_actor_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    follow_activity_id: Mapped[str] = mapped_column(String(512), nullable=False)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    remote_actor_id: Mapped[str] = mapped_column(String(512), nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class UserProfileField(Base):
    """Persist one ordered profile metadata name/value pair for a local user."""

    __tablename__ = "user_profile_fields"
    __table_args__ = (
        UniqueConstraint("user_id", "position"),
        CheckConstraint(
            "position >= 0 AND position <= 3", name="ck_user_profile_field_position"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    value: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class UserActorOutboundSource(Base):
    """Persist one immutable User Actor activity and fixed follower audience."""

    __tablename__ = "user_actor_outbound_sources"
    __table_args__ = (
        UniqueConstraint("activity_id"),
        UniqueConstraint("ordering_key", "logical_version"),
        CheckConstraint("logical_version >= 1", name="ck_user_actor_source_version"),
        CheckConstraint(
            "length(snapshot_json) > 0", name="ck_user_actor_source_snapshot_nonempty"
        ),
        CheckConstraint(
            "source_kind IN ('profile_update', 'content_create', 'content_update', 'content_delete')",
            name="ck_user_actor_source_kind",
        ),
        CheckConstraint(
            "state IN ('render_pending', 'ready', 'render_failed')",
            name="ck_user_actor_source_state",
        ),
        CheckConstraint(
            "render_attempt_count >= 0", name="ck_user_actor_source_render_attempts"
        ),
        CheckConstraint(
            "(state = 'ready' AND activity_json IS NOT NULL) OR (state != 'ready' AND activity_json IS NULL)",
            name="ck_user_actor_source_activity_state",
        ),
        Index(
            "ix_user_actor_sources_retry",
            "state",
            "next_attempt_at",
            "user_id",
            "ordering_key",
            "logical_version",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    ordering_key: Mapped[str] = mapped_column(String(512), nullable=False)
    logical_version: Mapped[int] = mapped_column(Integer, nullable=False)
    activity_id: Mapped[str] = mapped_column(String(512), nullable=False)
    published_object_id: Mapped[int | None] = mapped_column(
        ForeignKey("published_activity_objects.id", ondelete="CASCADE"), nullable=True
    )
    snapshot_json: Mapped[str] = mapped_column(String, nullable=False)
    activity_json: Mapped[str | None] = mapped_column(String, nullable=True)
    state: Mapped[str] = mapped_column(
        String(32), nullable=False, default="render_pending"
    )
    render_attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    last_render_attempted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(String(512), nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    @property
    def profile_snapshot_json(self) -> str:
        """Expose the Stage 4 name for profile-only callers during migration."""
        return self.snapshot_json


class UserActorOutboundDelivery(Base):
    """Persist one endpoint-scoped transport outcome for an outbound source."""

    __tablename__ = "user_actor_outbound_deliveries"
    __table_args__ = (
        UniqueConstraint("source_id", "target_endpoint_url"),
        CheckConstraint(
            "status IN ('pending', 'sending', 'delivered', 'failed', 'ineligible', 'superseded')",
            name="ck_user_actor_delivery_status",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_user_actor_delivery_attempts"),
        CheckConstraint(
            "length(target_endpoint_url) > 0",
            name="ck_user_actor_delivery_endpoint_nonempty",
        ),
        Index(
            "ix_user_actor_deliveries_retry", "status", "next_attempt_at", "claimed_at"
        ),
        Index(
            "ix_user_actor_deliveries_endpoint",
            "target_endpoint_url",
            "status",
            "source_id",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[int] = mapped_column(
        ForeignKey("user_actor_outbound_sources.id", ondelete="CASCADE"), nullable=False
    )
    target_endpoint_url: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    claim_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_attempted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(String(512), nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class UserActorOutboundDeliveryFollower(Base):
    """Retain immutable follower-incarnation evidence for one endpoint delivery."""

    __tablename__ = "user_actor_outbound_delivery_followers"
    __table_args__ = (
        UniqueConstraint("delivery_id", "relationship_id_snapshot"),
        Index(
            "ix_user_actor_delivery_followers_relationship",
            "relationship_id_snapshot",
            "delivery_id",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    delivery_id: Mapped[int] = mapped_column(
        ForeignKey("user_actor_outbound_deliveries.id", ondelete="CASCADE"),
        nullable=False,
    )
    relationship_id_snapshot: Mapped[int] = mapped_column(Integer, nullable=False)
    remote_actor_id: Mapped[str] = mapped_column(String(512), nullable=False)
    snapshotted_endpoint_url: Mapped[str] = mapped_column(String(512), nullable=False)


class UserActorOutboundSourceMedia(Base):
    """Keep profile media alive while an immutable outbound source can execute."""

    __tablename__ = "user_actor_outbound_source_media"
    __table_args__ = (
        UniqueConstraint("source_id", "slot_kind"),
        CheckConstraint(
            "slot_kind IN ('avatar', 'header')",
            name="ck_user_actor_source_media_slot",
        ),
        Index("ix_user_actor_source_media_asset", "media_asset_id", "source_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[int] = mapped_column(
        ForeignKey("user_actor_outbound_sources.id", ondelete="CASCADE"), nullable=False
    )
    media_asset_id: Mapped[int] = mapped_column(
        ForeignKey("profile_media_assets.id", ondelete="RESTRICT"), nullable=False
    )
    slot_kind: Mapped[str] = mapped_column(String(16), nullable=False)


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
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class MessageMapping(Base):
    """Store generic dedup and source mapping for ActivityPub bridge traffic."""

    # MessageMapping is intentionally more general than PostLink/CommentLink.
    # It captures the source IDs and AP object/activity IDs needed to suppress
    # echo loops once user-authored ActivityPub publishes are added.
    __tablename__ = "message_mappings"
    __table_args__ = (
        ForeignKeyConstraint(
            ["source_connector_id", "source_channel_surface_id"],
            ["platform_channel_surfaces.connector_id", "platform_channel_surfaces.id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        CheckConstraint(
            "source_channel_surface_id IS NULL OR source_connector_id IS NOT NULL",
            name="ck_messagemapping_source_pair",
        ),
        UniqueConstraint("source_connector_id", "source_id"),
        UniqueConstraint("activity_id"),
        UniqueConstraint("object_id"),
        CheckConstraint(
            "(publication_context = 'community' AND community_actor_url IS NOT NULL) OR "
            "(publication_context = 'personal_feed' AND community_actor_url IS NULL "
            "AND discord_guild_id IS NOT NULL AND discord_channel_id IS NOT NULL "
            "AND discord_message_id IS NOT NULL)",
            name="ck_message_mapping_publication_context",
        ),
    )

    source_connector_id: Mapped[int | None] = mapped_column(
        ForeignKey("platform_connectors.id", ondelete="RESTRICT"), nullable=True
    )
    source_channel_surface_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # MULTIPLATFORM_STAGE01_TEMP: Discord location projection remains for existing publication and personal-feed contracts.
    source_platform: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[str] = mapped_column(String(255), nullable=False)
    activity_id: Mapped[str] = mapped_column(String(512), nullable=False)
    object_id: Mapped[str] = mapped_column(String(512), nullable=False)
    actor_url: Mapped[str] = mapped_column(String(512), nullable=False)
    publication_context: Mapped[str] = mapped_column(
        String(32), nullable=False, default="community"
    )
    community_actor_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    discord_guild_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    discord_channel_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    discord_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class PublishedActivityObject(Base):
    """Persist one gateway-published ActivityPub object for later resolution."""

    # PublishedActivityObject is the durable source of truth for local AP
    # object URLs. Reply-chain resolution and canonical object routes rely on
    # this table instead of transient in-memory caches.
    __tablename__ = "published_activity_objects"
    __table_args__ = (
        ForeignKeyConstraint(
            ["source_connector_id", "source_channel_surface_id"],
            ["platform_channel_surfaces.connector_id", "platform_channel_surfaces.id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        CheckConstraint(
            "source_channel_surface_id IS NULL OR source_connector_id IS NOT NULL",
            name="ck_publishedactivityobject_source_pair",
        ),
        UniqueConstraint("activity_id"),
        UniqueConstraint("object_id"),
        CheckConstraint(
            "(publication_context = 'community' AND community_actor_url IS NOT NULL) OR "
            "(publication_context = 'personal_feed' AND community_actor_url IS NULL "
            "AND discord_guild_id IS NOT NULL AND discord_channel_id IS NOT NULL "
            "AND discord_message_id IS NOT NULL)",
            name="ck_published_object_publication_context",
        ),
        Index(
            "ix_published_objects_user_outbox",
            "actor_username",
            "actor_url",
            "deleted_at",
            "published_at",
            "id",
        ),
    )

    source_connector_id: Mapped[int | None] = mapped_column(
        ForeignKey("platform_connectors.id", ondelete="RESTRICT"), nullable=True
    )
    source_channel_surface_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    source_external_message_id: Mapped[str | None] = mapped_column(Text, nullable=True)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor_username: Mapped[str] = mapped_column(String(255), nullable=False)
    actor_url: Mapped[str] = mapped_column(String(512), nullable=False)
    publication_context: Mapped[str] = mapped_column(
        String(32), nullable=False, default="community"
    )
    community_actor_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    activity_id: Mapped[str] = mapped_column(String(512), nullable=False)
    object_id: Mapped[str] = mapped_column(String(512), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    body_markdown: Mapped[str] = mapped_column(String, nullable=False)
    in_reply_to_object_id: Mapped[str | None] = mapped_column(
        String(512), nullable=True
    )
    conversation_root_object_id: Mapped[str | None] = mapped_column(
        String(512), nullable=True, index=True
    )
    # MULTIPLATFORM_STAGE01_TEMP: Discord location projection remains for existing publication and personal-feed contracts.
    discord_guild_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    discord_channel_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    discord_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    desired_kind: Mapped[str] = mapped_column(
        String(16), nullable=False, default="none"
    )
    desired_title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    desired_body_markdown: Mapped[str | None] = mapped_column(String, nullable=True)
    desired_edited_timestamp: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    desired_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    discord_reconciliation_needed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    discord_reconciliation_attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    discord_reconciliation_next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    discord_reconciliation_last_error: Mapped[str | None] = mapped_column(
        String(512), nullable=True
    )
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class PersonalFeedThreadMapping(Base):
    """Persist the single Discord thread identity owned by one personal-feed root."""

    __tablename__ = "personal_feed_thread_mappings"
    __table_args__ = (
        ForeignKeyConstraint(
            ["connector_id", "channel_surface_id"],
            ["platform_channel_surfaces.connector_id", "platform_channel_surfaces.id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        UniqueConstraint("connector_id", "external_thread_id"),
        UniqueConstraint("connector_id", "external_starter_message_id"),
        UniqueConstraint("root_published_object_id"),
        CheckConstraint(
            "thread_id = root_discord_message_id",
            name="ck_personal_feed_thread_identity",
        ),
    )

    connector_id: Mapped[int] = mapped_column(
        ForeignKey("platform_connectors.id", ondelete="RESTRICT"), nullable=False
    )
    channel_surface_id: Mapped[int] = mapped_column(Integer, nullable=False)
    external_thread_id: Mapped[str] = mapped_column(Text, nullable=False)
    external_starter_message_id: Mapped[str | None] = mapped_column(Text, nullable=True)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    root_published_object_id: Mapped[int] = mapped_column(
        ForeignKey("published_activity_objects.id", ondelete="CASCADE"), nullable=False
    )
    # MULTIPLATFORM_STAGE01_TEMP: Discord location projection remains for existing publication and personal-feed contracts.
    root_discord_channel_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    root_discord_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    thread_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class InboundConversationObject(Base):
    """Store one canonical remote-authored comment independently of Discord delivery."""

    __tablename__ = "inbound_conversation_objects"
    __table_args__ = (
        UniqueConstraint("object_id"),
        UniqueConstraint("activity_id"),
        Index(
            "ix_inbound_conversation_root_parent",
            "conversation_root_object_id",
            "parent_object_id",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    object_id: Mapped[str] = mapped_column(String(512), nullable=False)
    publication_context: Mapped[str] = mapped_column(
        String(32), nullable=False, default="personal_feed"
    )
    activity_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    conversation_root_object_id: Mapped[str] = mapped_column(
        String(512), nullable=False
    )
    parent_object_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    actor_id: Mapped[str] = mapped_column(String(512), nullable=False)
    actor_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    content_markdown: Mapped[str] = mapped_column(Text, nullable=False)
    source_activity_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at_remote: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revision_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class InboundDiscordDelivery(Base):
    """Track bounded Discord rendering for one retained inbound comment."""

    __tablename__ = "inbound_discord_deliveries"
    __table_args__ = (
        ForeignKeyConstraint(
            ["connector_id", "channel_surface_id"],
            ["platform_channel_surfaces.connector_id", "platform_channel_surfaces.id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        UniqueConstraint("connector_id", "external_message_id"),
        UniqueConstraint("inbound_object_id", "root_published_object_id"),
        CheckConstraint(
            "state IN ('pending_parent','pending_thread','ready','dispatching','delivered','suppressed','failed')",
            name="ck_inbound_discord_delivery_state",
        ),
        CheckConstraint(
            "attempt_count >= 0 AND attempt_count <= 3",
            name="ck_inbound_discord_attempt_count",
        ),
        Index("ix_inbound_discord_due", "state", "next_attempt_at", "id"),
    )

    connector_id: Mapped[int] = mapped_column(
        ForeignKey("platform_connectors.id", ondelete="RESTRICT"), nullable=False
    )
    channel_surface_id: Mapped[int] = mapped_column(Integer, nullable=False)
    external_thread_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    external_message_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    external_parent_message_id: Mapped[str | None] = mapped_column(Text, nullable=True)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    inbound_object_id: Mapped[int] = mapped_column(
        ForeignKey("inbound_conversation_objects.id", ondelete="CASCADE"),
        nullable=False,
    )
    root_published_object_id: Mapped[int] = mapped_column(
        ForeignKey("published_activity_objects.id", ondelete="CASCADE"), nullable=False
    )
    # MULTIPLATFORM_STAGE01_TEMP: Discord location projection remains for existing publication and personal-feed contracts.
    destination_channel_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    destination_thread_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    discord_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(String(512), nullable=True)
    last_applied_revision: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class UserActorContentMutation(Base):
    """Persist one replayable pre-gateway content mutation reservation."""

    __tablename__ = "user_actor_content_mutations"
    __table_args__ = (
        UniqueConstraint("operation_id"),
        UniqueConstraint("activity_id"),
        CheckConstraint(
            "kind IN ('content_update', 'content_delete')",
            name="ck_user_actor_content_mutation_kind",
        ),
        CheckConstraint(
            "state IN ('reserved', 'dispatching', 'ambiguous', 'canonical_ready', 'committed', 'aborted_pre_send')",
            name="ck_user_actor_content_mutation_state",
        ),
        Index(
            "ix_user_actor_content_mutations_recovery",
            "state",
            "next_attempt_at",
            "claimed_at",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    published_object_id: Mapped[int] = mapped_column(
        ForeignKey("published_activity_objects.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    expected_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    next_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    operation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    activity_id: Mapped[str] = mapped_column(String(512), nullable=False)
    request_json: Mapped[str] = mapped_column(String, nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="reserved")
    canonical_activity_json: Mapped[str | None] = mapped_column(String, nullable=True)
    claim_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    side_effect_classification: Mapped[str] = mapped_column(
        String(32), nullable=False, default="not_dispatched"
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class CommunityThreadGroup(Base):
    """Map one canonical source thread event to all its Discord delivery targets."""

    # CommunityThreadGroup is the Phase 2+ replacement for PostLink. It records
    # the canonical source event (AP IDs, source channel/thread) and lets each
    # subscribed channel own a separate CommunityThreadGroupDelivery row.
    # source_* fields are nullable to accommodate inbound AP events, which have
    # no source Discord channel — threads are created in all subscribed channels.
    __tablename__ = "community_thread_groups"
    __table_args__ = (
        ForeignKeyConstraint(
            ["source_connector_id", "source_channel_surface_id"],
            ["platform_channel_surfaces.connector_id", "platform_channel_surfaces.id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        CheckConstraint(
            "source_channel_surface_id IS NULL OR source_connector_id IS NOT NULL",
            name="ck_communitythreadgroup_source_pair",
        ),
        UniqueConstraint("source_connector_id", "source_external_thread_id"),
    )

    source_connector_id: Mapped[int | None] = mapped_column(
        ForeignKey("platform_connectors.id", ondelete="RESTRICT"), nullable=True
    )
    source_channel_surface_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    source_external_thread_id: Mapped[str | None] = mapped_column(Text, nullable=True)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    community_actor_id: Mapped[str] = mapped_column(String(512), nullable=False)
    # MULTIPLATFORM_STAGE01_TEMP: Discord content ID columns remain only for pre-adapter callers; scoped IDs are authoritative.
    source_channel_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_thread_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_starter_message_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    ap_activity_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    ap_object_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class CommunityThreadGroupDelivery(Base):
    """Record one per-channel Discord delivery for a CommunityThreadGroup."""

    # Each subscribed channel gets its own delivery row so fanout and retry
    # logic can track which channels received the thread and which still need it.
    # UNIQUE(thread_group_id, discord_channel_id) ensures one delivery per channel per group.
    __tablename__ = "community_thread_group_deliveries"
    __table_args__ = (
        ForeignKeyConstraint(
            ["connector_id", "channel_surface_id"],
            ["platform_channel_surfaces.connector_id", "platform_channel_surfaces.id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        UniqueConstraint("connector_id", "external_thread_id"),
        UniqueConstraint("connector_id", "external_starter_message_id"),
        UniqueConstraint("thread_group_id", "channel_surface_id"),
        UniqueConstraint("thread_group_id", "discord_channel_id"),
    )

    connector_id: Mapped[int] = mapped_column(
        ForeignKey("platform_connectors.id", ondelete="RESTRICT"), nullable=False
    )
    channel_surface_id: Mapped[int] = mapped_column(Integer, nullable=False)
    external_thread_id: Mapped[str] = mapped_column(Text, nullable=False)
    external_starter_message_id: Mapped[str] = mapped_column(Text, nullable=False)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    thread_group_id: Mapped[int] = mapped_column(Integer, nullable=False)
    # MULTIPLATFORM_STAGE01_TEMP: Discord content ID columns remain only for pre-adapter callers; scoped IDs are authoritative.
    discord_channel_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    discord_thread_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    discord_starter_message_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
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
    __table_args__ = (
        ForeignKeyConstraint(
            ["source_connector_id", "source_channel_surface_id"],
            ["platform_channel_surfaces.connector_id", "platform_channel_surfaces.id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        CheckConstraint(
            "source_channel_surface_id IS NULL OR source_connector_id IS NOT NULL",
            name="ck_communitymessagegroup_source_pair",
        ),
        UniqueConstraint("source_connector_id", "source_external_message_id"),
    )

    source_connector_id: Mapped[int | None] = mapped_column(
        ForeignKey("platform_connectors.id", ondelete="RESTRICT"), nullable=True
    )
    source_channel_surface_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    source_external_message_id: Mapped[str | None] = mapped_column(Text, nullable=True)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    community_actor_id: Mapped[str] = mapped_column(String(512), nullable=False)
    thread_group_id: Mapped[int] = mapped_column(Integer, nullable=False)
    # MULTIPLATFORM_STAGE01_TEMP: Discord content ID columns remain only for pre-adapter callers; scoped IDs are authoritative.
    source_channel_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_thread_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ap_activity_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    ap_object_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # FK to another CommunityMessageGroup if this message is a reply.
    parent_message_group_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class CommunityMessageGroupDelivery(Base):
    """Record one per-channel Discord delivery for a CommunityMessageGroup."""

    # Each delivery channel gets its own row so fanout and retry logic can track
    # which threads received the message.
    # UNIQUE(message_group_id, discord_channel_id) ensures one delivery per channel per group.
    __tablename__ = "community_message_group_deliveries"
    __table_args__ = (
        ForeignKeyConstraint(
            ["connector_id", "channel_surface_id"],
            ["platform_channel_surfaces.connector_id", "platform_channel_surfaces.id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        UniqueConstraint("connector_id", "external_message_id"),
        UniqueConstraint("message_group_id", "channel_surface_id"),
        UniqueConstraint("message_group_id", "discord_channel_id"),
    )

    connector_id: Mapped[int] = mapped_column(
        ForeignKey("platform_connectors.id", ondelete="RESTRICT"), nullable=False
    )
    channel_surface_id: Mapped[int] = mapped_column(Integer, nullable=False)
    external_thread_id: Mapped[str] = mapped_column(Text, nullable=False)
    external_message_id: Mapped[str] = mapped_column(Text, nullable=False)
    external_parent_message_id: Mapped[str | None] = mapped_column(Text, nullable=True)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    message_group_id: Mapped[int] = mapped_column(Integer, nullable=False)
    # MULTIPLATFORM_STAGE01_TEMP: Discord content ID columns remain only for pre-adapter callers; scoped IDs are authoritative.
    discord_channel_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    discord_thread_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    discord_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
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
    community_actor_id: Mapped[str] = mapped_column(
        String(512), nullable=False, unique=True
    )
    follow_activity_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    community_inbox_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


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
    last_fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class LocalCommunity(Base):
    """Persist one stable federated community independently of host placement.

    LocalCommunity is the Python-owned source of truth for the community actor
    metadata that the gateway exposes publicly. The row owns one stable actor URL and one stable keypair; its host
    placement is held by a typed ChannelSurface claim. Creator ownership is
    stored as a Discord user id for local bridge management commands only; it is
    not an ActivityPub identity.
    """

    __tablename__ = "local_communities"
    __table_args__ = (
        UniqueConstraint("slug"),
        UniqueConstraint("actor_url"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    slug: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    summary: Mapped[str | None] = mapped_column(String, nullable=True)
    created_by_discord_user_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    actor_url: Mapped[str] = mapped_column(String(512), nullable=False)
    inbox_url: Mapped[str] = mapped_column(String(512), nullable=False)
    outbox_url: Mapped[str] = mapped_column(String(512), nullable=False)
    followers_url: Mapped[str] = mapped_column(String(512), nullable=False)
    public_key_pem: Mapped[str] = mapped_column(String, nullable=False)
    private_key_pem: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class CommunityActorBan(Base):
    """Persist one bridge-local user ban with community or global scope.

    The row may identify a local registered Discord user as well as an
    ActivityPub actor. Global rows use ``local_community_id = NULL``; community
    rows require a concrete local community id. Status toggling preserves the
    existing create/reactivate/remove history model without emitting protocol
    moderation activities.
    """

    __tablename__ = "community_actor_bans"
    __table_args__ = (UniqueConstraint("scope", "scope_key", "actor_handle", "status"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scope: Mapped[str] = mapped_column(String(32), nullable=False, default="community")
    scope_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    local_community_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    actor_handle: Mapped[str] = mapped_column(String(255), nullable=False)
    actor_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    target_discord_user_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    created_by_discord_user_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    reason: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class BridgePolicyEntry(Base):
    """Persist one dynamic bridge policy subject with reversible status."""

    __tablename__ = "bridge_policy_entries"
    __table_args__ = (UniqueConstraint("policy_type", "normalized_subject"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    policy_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    normalized_subject: Mapped[str] = mapped_column(
        String(512), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="active", index=True
    )
    reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_by_discord_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_by_discord_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


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
    reason_code: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    actor_discord_user_id: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True
    )
    local_community_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True, index=True
    )
    target_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    target_id: Mapped[str | None] = mapped_column(
        String(512), nullable=True, index=True
    )
    before_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    after_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )


class GuildInvitePublication(Base):
    """Store the single public Discord invite currently published for a guild."""

    __tablename__ = "guild_invite_publications"
    __table_args__ = (UniqueConstraint("discord_guild_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    discord_guild_id: Mapped[int] = mapped_column(Integer, nullable=False)
    discord_channel_id: Mapped[int] = mapped_column(Integer, nullable=False)
    invite_code: Mapped[str] = mapped_column(String(128), nullable=False)
    invite_url: Mapped[str] = mapped_column(String(512), nullable=False)
    published_by_discord_user_id: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


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
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


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
    # MULTIPLATFORM_STAGE01_TEMP: Nullable Discord subscription location projection is for the legacy Discord runtime only.
    discord_channel_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    initiated_by_discord_user_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class LocalCommunityRelaySourceActivity(Base):
    """Store one immutable inbound ActivityPub source activity for relay.

    The source row is the durable payload reference used by retries and
    duplicate recovery. Delivery rows point here instead of duplicating large
    JSON blobs for every target follower.
    """

    __tablename__ = "local_community_relay_source_activities"
    __table_args__ = (
        UniqueConstraint(
            "local_community_id",
            "operation",
            "source_object_ap_id",
            "source_activity_id",
        ),
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
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


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
    delivery_profile: Mapped[str] = mapped_column(
        String(64), nullable=False, default="threadiverse_group"
    )
    relay_activity_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_attempted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


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
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


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
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class LocalCommunityThreadSurface(Base):
    """Map one canonical local-community thread onto one Discord forum surface.

    Stage 2 introduces explicit surface rows so host-forum behavior can keep
    working while later stages add more local Discord targets for the same
    canonical thread.
    """

    __tablename__ = "local_community_thread_surfaces"
    __table_args__ = (
        ForeignKeyConstraint(
            ["connector_id", "channel_surface_id"],
            ["platform_channel_surfaces.connector_id", "platform_channel_surfaces.id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        UniqueConstraint("connector_id", "external_thread_id"),
        UniqueConstraint("connector_id", "external_starter_message_id"),
        UniqueConstraint("local_community_thread_id", "channel_surface_id"),
        UniqueConstraint("id", "channel_surface_id"),
        UniqueConstraint("local_community_thread_id", "discord_forum_channel_id"),
    )

    connector_id: Mapped[int] = mapped_column(
        ForeignKey("platform_connectors.id", ondelete="RESTRICT"), nullable=False
    )
    channel_surface_id: Mapped[int] = mapped_column(Integer, nullable=False)
    external_thread_id: Mapped[str] = mapped_column(Text, nullable=False)
    external_starter_message_id: Mapped[str] = mapped_column(Text, nullable=False)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    local_community_thread_id: Mapped[int] = mapped_column(Integer, nullable=False)
    # MULTIPLATFORM_STAGE01_TEMP: Discord content ID columns remain only for pre-adapter callers; scoped IDs are authoritative.
    discord_forum_channel_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    discord_thread_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    discord_starter_message_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
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
        ForeignKeyConstraint(
            ["connector_id", "channel_surface_id"],
            ["platform_channel_surfaces.connector_id", "platform_channel_surfaces.id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        UniqueConstraint("connector_id", "external_message_id"),
        ForeignKeyConstraint(
            ["local_community_thread_surface_id", "channel_surface_id"],
            [
                "local_community_thread_surfaces.id",
                "local_community_thread_surfaces.channel_surface_id",
            ],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        UniqueConstraint(
            "local_community_message_id", "local_community_thread_surface_id"
        ),
    )

    connector_id: Mapped[int] = mapped_column(
        ForeignKey("platform_connectors.id", ondelete="RESTRICT"), nullable=False
    )
    channel_surface_id: Mapped[int] = mapped_column(Integer, nullable=False)
    external_message_id: Mapped[str] = mapped_column(Text, nullable=False)
    external_parent_message_id: Mapped[str | None] = mapped_column(Text, nullable=True)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    local_community_message_id: Mapped[int] = mapped_column(Integer, nullable=False)
    local_community_thread_surface_id: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    # MULTIPLATFORM_STAGE01_TEMP: Discord content ID columns remain only for pre-adapter callers; scoped IDs are authoritative.
    discord_forum_channel_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    discord_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    parent_discord_message_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    # Host surfaces keep this empty. Local-subscriber surfaces store the row
    # that selected the target forum, preserving historical target identity.
    local_subscriber_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
