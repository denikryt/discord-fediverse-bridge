"""Repository helpers for bridge routing, identity, and federation state."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from . import migrations, schema
from .repositories import (
    LegacyLemmyMappingRepository,
    DiscordFanoutGroupRepository,
    RemoteActorRepository,
    MessageMappingRepository,
    ActivityPubObjectRepository,
    UserRepository,
    RegistrationSessionRepository,
    EventReceiptRepository,
    RemoteSubscriptionRepository,
    BridgeActorFollowRepository,
    LocalCommunityContentRepository,
    LocalCommunityRelayRepository,
    LocalCommunityRepository,
    LocalCommunitySurfaceRepository,
    LocalSubscriberRepository,
    RemoteSubscriberRepository,
)
from ..models import (
    ActivityPubEventReceipt,
    BridgeActorFollow,
    ChannelCommunitySubscription,
    CommentLink,
    CommunityMessageGroup,
    CommunityMessageGroupDelivery,
    CommunityThreadGroup,
    CommunityThreadGroupDelivery,
    LocalCommunity,
    LocalSubscriber,
    LocalCommunityMessage,
    LocalCommunityMessageSurface,
    LocalCommunityRelayDelivery,
    LocalCommunityRelaySourceActivity,
    LocalCommunityThread,
    LocalCommunityThreadSurface,
    MessageMapping,
    PostLink,
    PublishedActivityObject,
    RegistrationSession,
    RemoteActor,
    RemoteSubscriber,
    User,
    utcnow,
)


class Database:
    """Wrap ORM access behind intent-specific repository methods."""

    # Database is a small repository-style wrapper that keeps bridge code away
    # from session management and direct ORM details.
    # ---------------------------------------------------------------------------
    # Engine, session, and schema lifecycle helpers
    #
    # Database owns engine construction, session factory construction, schema
    # bootstrap, migration checks, and transactional session cleanup. Later
    # repositories must share this ownership instead of creating independent
    # engines or session factories.
    # ---------------------------------------------------------------------------

    def __init__(self, url: str) -> None:
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        self.engine = create_engine(url, future=True, connect_args=connect_args)
        self.session_factory = sessionmaker(self.engine, expire_on_commit=False, class_=Session)
        self.local_communities = LocalCommunityRepository(self.session)
        self.remote_subscribers = RemoteSubscriberRepository(self.session)
        self.local_subscribers = LocalSubscriberRepository(self.session)
        self.local_community_content = LocalCommunityContentRepository(self.session)
        self.local_community_surfaces = LocalCommunitySurfaceRepository(self.session)
        self.local_community_relay = LocalCommunityRelayRepository(self.session)
        self.remote_subscriptions = RemoteSubscriptionRepository(self.session)
        self.bridge_actor_follows = BridgeActorFollowRepository(self.session)
        self.event_receipts = EventReceiptRepository(self.session)
        self.users = UserRepository(self.session)
        self.registration_sessions = RegistrationSessionRepository(self.session)
        self.message_mappings = MessageMappingRepository(self.session)
        self.activitypub_objects = ActivityPubObjectRepository(self.session)
        self.remote_actors = RemoteActorRepository(self.session)
        self.legacy_lemmy_mappings = LegacyLemmyMappingRepository(self.session)
        self.discord_fanout_groups = DiscordFanoutGroupRepository(self.session)

    def create_all(self) -> None:
        """Create the full clean-schema set required by the current codebase."""
        schema.create_all(self.engine)

    def migrate(self) -> None:
        """Apply additive schema migrations that create_all cannot handle."""
        migrations.migrate(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        """Yield one transactional session with uniform cleanup semantics."""
        # Every public DB operation uses the same commit/rollback discipline so
        # callers do not need to care about transaction cleanup.
        session = self.session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # ---------------------------------------------------------------------------
    # Legacy direct Lemmy post/comment mapping helpers
    #
    # These helpers own the older PostLink and CommentLink rows used by direct
    # remote-community publish/fanout paths. They preserve existing Discord
    # thread/message dedup keys until Stage 7 moves them into a repository.
    # ---------------------------------------------------------------------------

    def get_post_link_by_thread_id(self, discord_thread_id: int) -> PostLink | None:
        """Temporarily forward to the Stage 7 legacy_lemmy_mappings repository."""
        return self.legacy_lemmy_mappings.get_post_link_by_thread_id(discord_thread_id)

    def get_post_link_by_lemmy_post_id(self, lemmy_post_id: int) -> PostLink | None:
        """Temporarily forward to the Stage 7 legacy_lemmy_mappings repository."""
        return self.legacy_lemmy_mappings.get_post_link_by_lemmy_post_id(lemmy_post_id)

    def get_post_link_by_lemmy_post_ap_id(self, lemmy_post_ap_id: str) -> PostLink | None:
        """Temporarily forward to the Stage 7 legacy_lemmy_mappings repository."""
        return self.legacy_lemmy_mappings.get_post_link_by_lemmy_post_ap_id(lemmy_post_ap_id)

    def get_post_links_by_lemmy_post_ap_id(self, lemmy_post_ap_id: str) -> list[PostLink]:
        """Temporarily forward to the Stage 7 legacy_lemmy_mappings repository."""
        return self.legacy_lemmy_mappings.get_post_links_by_lemmy_post_ap_id(lemmy_post_ap_id)

    def get_post_link_by_lemmy_post_ap_id_and_channel_id(self, lemmy_post_ap_id: str, discord_forum_channel_id: int) -> PostLink | None:
        """Temporarily forward to the Stage 7 legacy_lemmy_mappings repository."""
        return self.legacy_lemmy_mappings.get_post_link_by_lemmy_post_ap_id_and_channel_id(lemmy_post_ap_id, discord_forum_channel_id)

    def create_post_link(self, *, lemmy_post_id: int, lemmy_post_ap_id: str | None, discord_forum_channel_id: int | None=None, discord_forum_thread_id: int, discord_starter_message_id: int | None, direction: str) -> PostLink:
        """Temporarily forward to the Stage 7 legacy_lemmy_mappings repository."""
        return self.legacy_lemmy_mappings.create_post_link(lemmy_post_id=lemmy_post_id, lemmy_post_ap_id=lemmy_post_ap_id, discord_forum_channel_id=discord_forum_channel_id, discord_forum_thread_id=discord_forum_thread_id, discord_starter_message_id=discord_starter_message_id, direction=direction)

    def has_comment_link_for_discord_message(self, discord_message_id: int) -> bool:
        """Temporarily forward to the Stage 7 legacy_lemmy_mappings repository."""
        return self.legacy_lemmy_mappings.has_comment_link_for_discord_message(discord_message_id)

    def has_comment_link_for_lemmy_comment(self, lemmy_comment_id: int) -> bool:
        """Temporarily forward to the Stage 7 legacy_lemmy_mappings repository."""
        return self.legacy_lemmy_mappings.has_comment_link_for_lemmy_comment(lemmy_comment_id)

    def get_comment_link_by_lemmy_comment_ap_id(self, lemmy_comment_ap_id: str) -> CommentLink | None:
        """Temporarily forward to the Stage 7 legacy_lemmy_mappings repository."""
        return self.legacy_lemmy_mappings.get_comment_link_by_lemmy_comment_ap_id(lemmy_comment_ap_id)

    def get_comment_links_by_lemmy_comment_ap_id(self, lemmy_comment_ap_id: str) -> list[CommentLink]:
        """Temporarily forward to the Stage 7 legacy_lemmy_mappings repository."""
        return self.legacy_lemmy_mappings.get_comment_links_by_lemmy_comment_ap_id(lemmy_comment_ap_id)

    def get_comment_link_by_discord_message_id(self, discord_message_id: int) -> CommentLink | None:
        """Temporarily forward to the Stage 7 legacy_lemmy_mappings repository."""
        return self.legacy_lemmy_mappings.get_comment_link_by_discord_message_id(discord_message_id)

    def create_comment_link(self, *, lemmy_comment_id: int, lemmy_comment_ap_id: str | None, lemmy_parent_comment_ap_id: str | None, lemmy_post_id: int, discord_forum_thread_id: int, discord_message_id: int, direction: str) -> CommentLink:
        """Temporarily forward to the Stage 7 legacy_lemmy_mappings repository."""
        return self.legacy_lemmy_mappings.create_comment_link(lemmy_comment_id=lemmy_comment_id, lemmy_comment_ap_id=lemmy_comment_ap_id, lemmy_parent_comment_ap_id=lemmy_parent_comment_ap_id, lemmy_post_id=lemmy_post_id, discord_forum_thread_id=discord_forum_thread_id, discord_message_id=discord_message_id, direction=direction)

    # ---------------------------------------------------------------------------
    # Inbound ActivityPub event receipt helpers
    #
    # These helpers persist delivery receipts so inbound ActivityPub processing
    # can remain idempotent across retries. Stage 5 moves them to the event
    # receipt repository without changing delivery-id decisions.
    # ---------------------------------------------------------------------------

    def get_event_receipt(self, delivery_id: str) -> ActivityPubEventReceipt | None:
        """Temporarily forward to the Stage 5 event_receipts repository."""
        return self.event_receipts.get_event_receipt(delivery_id)

    def create_event_receipt(self, *, delivery_id: str, event_type: str, object_ap_id: str, status: str, detail: str | None=None) -> ActivityPubEventReceipt:
        """Temporarily forward to the Stage 5 event_receipts repository."""
        return self.event_receipts.create_event_receipt(delivery_id=delivery_id, event_type=event_type, object_ap_id=object_ap_id, status=status, detail=detail)

    def update_event_receipt(self, *, delivery_id: str, status: str, detail: str | None=None) -> None:
        """Temporarily forward to the Stage 5 event_receipts repository."""
        return self.event_receipts.update_event_receipt(delivery_id=delivery_id, status=status, detail=detail)

    # ---------------------------------------------------------------------------
    # Remote community subscription helpers
    #
    # These helpers own ChannelCommunitySubscription lifecycle state for
    # /subscribe-channel, /unsubscribe-channel, inbound fanout selection, and
    # stale inbound filtering. Stage 4 extracts them with bridge-follow state.
    # ---------------------------------------------------------------------------

    def get_subscription_by_channel(self, discord_channel_id: int) -> ChannelCommunitySubscription | None:
        """Temporarily forward to the Stage 4 remote_subscriptions repository."""
        return self.remote_subscriptions.get_subscription_by_channel(discord_channel_id)

    def get_subscriptions_by_community(self, lemmy_community_actor_id: str) -> list[ChannelCommunitySubscription]:
        """Temporarily forward to the Stage 4 remote_subscriptions repository."""
        return self.remote_subscriptions.get_subscriptions_by_community(lemmy_community_actor_id)

    def get_all_subscriptions(self) -> list[ChannelCommunitySubscription]:
        """Temporarily forward to the Stage 4 remote_subscriptions repository."""
        return self.remote_subscriptions.get_all_subscriptions()

    def get_subscriptions_by_guild(self, discord_guild_id: int) -> list[ChannelCommunitySubscription]:
        """Temporarily forward to the Stage 4 remote_subscriptions repository."""
        return self.remote_subscriptions.get_subscriptions_by_guild(discord_guild_id)

    def create_subscription(self, *, discord_channel_id: int, discord_guild_id: int | None=None, lemmy_community_actor_id: str, lemmy_community_name: str | None, lemmy_community_id: int | None, community_handle: str | None=None, community_inbox_url: str | None=None, follow_activity_id: str | None=None, initiated_by_discord_user_id: str | None=None, status: str='pending') -> ChannelCommunitySubscription:
        """Temporarily forward to the Stage 4 remote_subscriptions repository."""
        return self.remote_subscriptions.create_subscription(discord_channel_id=discord_channel_id, discord_guild_id=discord_guild_id, lemmy_community_actor_id=lemmy_community_actor_id, lemmy_community_name=lemmy_community_name, lemmy_community_id=lemmy_community_id, community_handle=community_handle, community_inbox_url=community_inbox_url, follow_activity_id=follow_activity_id, initiated_by_discord_user_id=initiated_by_discord_user_id, status=status)

    def update_subscription_follow_state(self, *, discord_channel_id: int, community_handle: str | None=None, community_inbox_url: str | None, follow_activity_id: str | None, status: str) -> None:
        """Temporarily forward to the Stage 4 remote_subscriptions repository."""
        return self.remote_subscriptions.update_subscription_follow_state(discord_channel_id=discord_channel_id, community_handle=community_handle, community_inbox_url=community_inbox_url, follow_activity_id=follow_activity_id, status=status)

    def delete_subscription(self, discord_channel_id: int) -> bool:
        """Temporarily forward to the Stage 4 remote_subscriptions repository."""
        return self.remote_subscriptions.delete_subscription(discord_channel_id)

    # ---------------------------------------------------------------------------
    # User identity and registration-session helpers
    #
    # These helpers create local ActivityPub users and persist browser/OAuth
    # registration state. Stage 5 separates user rows from registration-session
    # rows while preserving the current HTTP registration flow.
    # ---------------------------------------------------------------------------

    def create_user(self, *, discord_user_id: str, activitypub_username: str, actor_url: str, inbox_url: str, outbox_url: str, followers_url: str, public_key_pem: str, private_key_pem: str) -> User:
        """Temporarily forward to the Stage 5 users repository."""
        return self.users.create_user(discord_user_id=discord_user_id, activitypub_username=activitypub_username, actor_url=actor_url, inbox_url=inbox_url, outbox_url=outbox_url, followers_url=followers_url, public_key_pem=public_key_pem, private_key_pem=private_key_pem)

    def create_registration_session(self, *, session_token: str, expires_at: datetime) -> RegistrationSession:
        """Temporarily forward to the Stage 5 registration_sessions repository."""
        return self.registration_sessions.create_registration_session(session_token=session_token, expires_at=expires_at)

    def get_registration_session_by_token(self, session_token: str | None) -> RegistrationSession | None:
        """Temporarily forward to the Stage 5 registration_sessions repository."""
        return self.registration_sessions.get_registration_session_by_token(session_token)

    def update_registration_session_oauth_state(self, *, session_token: str, oauth_state: str, expires_at: datetime) -> RegistrationSession:
        """Temporarily forward to the Stage 5 registration_sessions repository."""
        return self.registration_sessions.update_registration_session_oauth_state(session_token=session_token, oauth_state=oauth_state, expires_at=expires_at)

    def update_registration_session_discord_identity(self, *, session_token: str, discord_user_id: str, discord_username: str, discord_avatar_url: str | None, expires_at: datetime) -> RegistrationSession:
        """Temporarily forward to the Stage 5 registration_sessions repository."""
        return self.registration_sessions.update_registration_session_discord_identity(session_token=session_token, discord_user_id=discord_user_id, discord_username=discord_username, discord_avatar_url=discord_avatar_url, expires_at=expires_at)

    def mark_registration_session_completed(self, *, session_token: str, activitypub_username: str, expires_at: datetime) -> RegistrationSession:
        """Temporarily forward to the Stage 5 registration_sessions repository."""
        return self.registration_sessions.mark_registration_session_completed(session_token=session_token, activitypub_username=activitypub_username, expires_at=expires_at)

    def get_user_by_discord_user_id(self, discord_user_id: str) -> User | None:
        """Temporarily forward to the Stage 5 users repository."""
        return self.users.get_user_by_discord_user_id(discord_user_id)

    def get_user_by_activitypub_username(self, activitypub_username: str) -> User | None:
        """Temporarily forward to the Stage 5 users repository."""
        return self.users.get_user_by_activitypub_username(activitypub_username)

    def get_user_by_actor_url(self, actor_url: str) -> User | None:
        """Temporarily forward to the Stage 5 users repository."""
        return self.users.get_user_by_actor_url(actor_url)

    # ---------------------------------------------------------------------------
    # Local community identity helpers
    #
    # These helpers own Discord-backed local community actor rows and lookup
    # paths used by create-community, actor rendering, dashboard navigation, and
    # local runtime routing. Stage 3 moves them to LocalCommunityRepository.
    # ---------------------------------------------------------------------------

    def create_local_community(self, *, discord_guild_id: int, discord_forum_channel_id: int, slug: str, display_name: str, summary: str, actor_url: str, inbox_url: str, outbox_url: str, followers_url: str, public_key_pem: str, private_key_pem: str, status: str='active') -> LocalCommunity:
        """Temporarily forward to the Stage 3 local_communities repository."""
        return self.local_communities.create_local_community(discord_guild_id=discord_guild_id, discord_forum_channel_id=discord_forum_channel_id, slug=slug, display_name=display_name, summary=summary, actor_url=actor_url, inbox_url=inbox_url, outbox_url=outbox_url, followers_url=followers_url, public_key_pem=public_key_pem, private_key_pem=private_key_pem, status=status)

    def get_local_community_by_forum_channel_id(self, discord_forum_channel_id: int) -> LocalCommunity | None:
        """Temporarily forward to the Stage 3 local_communities repository."""
        return self.local_communities.get_local_community_by_forum_channel_id(discord_forum_channel_id)

    def get_local_community_by_actor_url(self, actor_url: str) -> LocalCommunity | None:
        """Temporarily forward to the Stage 3 local_communities repository."""
        return self.local_communities.get_local_community_by_actor_url(actor_url)

    def get_local_community_by_slug(self, slug: str) -> LocalCommunity | None:
        """Temporarily forward to the Stage 3 local_communities repository."""
        return self.local_communities.get_local_community_by_slug(slug)

    def get_local_community_by_id(self, local_community_id: int) -> LocalCommunity | None:
        """Temporarily forward to the Stage 3 local_communities repository."""
        return self.local_communities.get_local_community_by_id(local_community_id)

    def list_local_communities(self) -> list[LocalCommunity]:
        """Temporarily forward to the Stage 3 local_communities repository."""
        return self.local_communities.list_local_communities()

    # ---------------------------------------------------------------------------
    # Remote subscriber helpers
    #
    # These helpers own ActivityPub actors that follow bridge-hosted local
    # communities. Accepted remote subscribers remain the fanout source of truth
    # until Stage 3 moves the methods to RemoteSubscriberRepository.
    # ---------------------------------------------------------------------------

    def create_remote_subscriber(self, *, local_community_id: int, remote_actor_id: str, remote_inbox_url: str, follow_activity_id: str, status: str='accepted') -> RemoteSubscriber:
        """Temporarily forward to the Stage 3 remote_subscribers repository."""
        return self.remote_subscribers.create_remote_subscriber(local_community_id=local_community_id, remote_actor_id=remote_actor_id, remote_inbox_url=remote_inbox_url, follow_activity_id=follow_activity_id, status=status)

    def get_remote_subscriber(self, *, local_community_id: int, remote_actor_id: str) -> RemoteSubscriber | None:
        """Temporarily forward to the Stage 3 remote_subscribers repository."""
        return self.remote_subscribers.get_remote_subscriber(local_community_id=local_community_id, remote_actor_id=remote_actor_id)

    def get_remote_subscriber_by_follow_activity_id(self, follow_activity_id: str) -> RemoteSubscriber | None:
        """Temporarily forward to the Stage 3 remote_subscribers repository."""
        return self.remote_subscribers.get_remote_subscriber_by_follow_activity_id(follow_activity_id)

    def update_remote_subscriber_acceptance(self, *, local_community_id: int, remote_actor_id: str, remote_inbox_url: str, follow_activity_id: str, status: str='accepted') -> RemoteSubscriber | None:
        """Temporarily forward to the Stage 3 remote_subscribers repository."""
        return self.remote_subscribers.update_remote_subscriber_acceptance(local_community_id=local_community_id, remote_actor_id=remote_actor_id, remote_inbox_url=remote_inbox_url, follow_activity_id=follow_activity_id, status=status)

    def delete_remote_subscriber(self, *, local_community_id: int, remote_actor_id: str) -> bool:
        """Temporarily forward to the Stage 3 remote_subscribers repository."""
        return self.remote_subscribers.delete_remote_subscriber(local_community_id=local_community_id, remote_actor_id=remote_actor_id)

    def list_remote_subscribers(self, local_community_id: int, *, status: str | None='accepted') -> list[RemoteSubscriber]:
        """Temporarily forward to the Stage 3 remote_subscribers repository."""
        return self.remote_subscribers.list_remote_subscribers(local_community_id, status=status)

    def list_remote_subscribers_for_all(self, *, status: str | None='accepted') -> list[RemoteSubscriber]:
        """Temporarily forward to the Stage 3 remote_subscribers repository."""
        return self.remote_subscribers.list_remote_subscribers_for_all(status=status)

    # ---------------------------------------------------------------------------
    # Local subscriber helpers
    #
    # These helpers own same-instance Discord forum subscriptions to local
    # communities. They support participant routing, dashboard counts, and
    # source-authority checks for local-subscriber edits/deletes.
    # ---------------------------------------------------------------------------

    def create_local_subscriber(self, *, local_community_id: int, discord_guild_id: int | None, discord_channel_id: int, initiated_by_discord_user_id: str | None, status: str='active') -> LocalSubscriber:
        """Temporarily forward to the Stage 3 local_subscribers repository."""
        return self.local_subscribers.create_local_subscriber(local_community_id=local_community_id, discord_guild_id=discord_guild_id, discord_channel_id=discord_channel_id, initiated_by_discord_user_id=initiated_by_discord_user_id, status=status)

    def get_local_subscriber(self, *, local_community_id: int, discord_channel_id: int) -> LocalSubscriber | None:
        """Temporarily forward to the Stage 3 local_subscribers repository."""
        return self.local_subscribers.get_local_subscriber(local_community_id=local_community_id, discord_channel_id=discord_channel_id)

    def get_local_subscriber_by_channel(self, discord_channel_id: int) -> LocalSubscriber | None:
        """Temporarily forward to the Stage 3 local_subscribers repository."""
        return self.local_subscribers.get_local_subscriber_by_channel(discord_channel_id)

    def list_local_subscribers(self, local_community_id: int) -> list[LocalSubscriber]:
        """Temporarily forward to the Stage 3 local_subscribers repository."""
        return self.local_subscribers.list_local_subscribers(local_community_id)

    def list_local_subscribers_by_guild(self, discord_guild_id: int) -> list[LocalSubscriber]:
        """Temporarily forward to the Stage 3 local_subscribers repository."""
        return self.local_subscribers.list_local_subscribers_by_guild(discord_guild_id)

    def delete_local_subscriber(self, discord_channel_id: int) -> bool:
        """Temporarily forward to the Stage 3 local_subscribers repository."""
        return self.local_subscribers.delete_local_subscriber(discord_channel_id)

    def count_local_subscribers(self, local_community_id: int) -> int:
        """Temporarily forward to the Stage 3 local_subscribers repository."""
        return self.local_subscribers.count_local_subscribers(local_community_id)

    # ---------------------------------------------------------------------------
    # Local-community canonical content helpers
    #
    # These helpers own canonical local-community thread/message rows and their
    # ActivityPub IDs. Discord-specific delivery surfaces are marked separately
    # below so Stage 3 can preserve the canonical-vs-surface boundary.
    # ---------------------------------------------------------------------------

    def create_local_community_thread(self, *, local_community_id: int, discord_thread_id: int, discord_starter_message_id: int, ap_activity_id: str, ap_object_id: str, direction: str, origin_kind: str) -> LocalCommunityThread:
        """Temporarily forward to the Stage 3 local_community_content repository."""
        return self.local_community_content.create_local_community_thread(local_community_id=local_community_id, discord_thread_id=discord_thread_id, discord_starter_message_id=discord_starter_message_id, ap_activity_id=ap_activity_id, ap_object_id=ap_object_id, direction=direction, origin_kind=origin_kind)

    def create_local_community_thread_canonical(self, *, local_community_id: int, ap_activity_id: str, ap_object_id: str, direction: str, origin_kind: str) -> LocalCommunityThread:
        """Temporarily forward to the Stage 3 local_community_content repository."""
        return self.local_community_content.create_local_community_thread_canonical(local_community_id=local_community_id, ap_activity_id=ap_activity_id, ap_object_id=ap_object_id, direction=direction, origin_kind=origin_kind)

    # ---------------------------------------------------------------------------
    # Local-community thread surface helpers
    #
    # These helpers map one canonical local-community thread to concrete Discord
    # thread/starter-message surfaces. Surface rows carry host/subscriber role
    # and local_subscriber_id ownership; canonical AP IDs stay on content rows.
    # ---------------------------------------------------------------------------

    def create_local_community_thread_surface(self, *, local_community_thread_id: int, discord_forum_channel_id: int, discord_thread_id: int, discord_starter_message_id: int, role: str, local_subscriber_id: int | None=None) -> LocalCommunityThreadSurface:
        """Temporarily forward to the Stage 3 local_community_surfaces repository."""
        return self.local_community_surfaces.create_local_community_thread_surface(local_community_thread_id=local_community_thread_id, discord_forum_channel_id=discord_forum_channel_id, discord_thread_id=discord_thread_id, discord_starter_message_id=discord_starter_message_id, role=role, local_subscriber_id=local_subscriber_id)

    def get_local_community_thread_surface(self, *, local_community_thread_id: int, discord_forum_channel_id: int) -> LocalCommunityThreadSurface | None:
        """Temporarily forward to the Stage 3 local_community_surfaces repository."""
        return self.local_community_surfaces.get_local_community_thread_surface(local_community_thread_id=local_community_thread_id, discord_forum_channel_id=discord_forum_channel_id)

    def get_local_community_thread_surface_by_discord_thread_id(self, discord_thread_id: int) -> LocalCommunityThreadSurface | None:
        """Temporarily forward to the Stage 3 local_community_surfaces repository."""
        return self.local_community_surfaces.get_local_community_thread_surface_by_discord_thread_id(discord_thread_id)

    def get_local_community_thread_surface_by_starter_message_id(self, discord_starter_message_id: int) -> LocalCommunityThreadSurface | None:
        """Temporarily forward to the Stage 3 local_community_surfaces repository."""
        return self.local_community_surfaces.get_local_community_thread_surface_by_starter_message_id(discord_starter_message_id)

    def list_local_community_thread_surfaces(self, local_community_thread_id: int) -> list[LocalCommunityThreadSurface]:
        """Temporarily forward to the Stage 3 local_community_surfaces repository."""
        return self.local_community_surfaces.list_local_community_thread_surfaces(local_community_thread_id)

    def get_host_local_community_thread_surface(self, local_community_thread_id: int) -> LocalCommunityThreadSurface | None:
        """Temporarily forward to the Stage 3 local_community_surfaces repository."""
        return self.local_community_surfaces.get_host_local_community_thread_surface(local_community_thread_id)

    def get_local_community_thread_by_ap_object_id(self, ap_object_id: str) -> LocalCommunityThread | None:
        """Temporarily forward to the Stage 3 local_community_content repository."""
        return self.local_community_content.get_local_community_thread_by_ap_object_id(ap_object_id)

    # ---------------------------------------------------------------------------
    # Local-community canonical message helpers
    #
    # These helpers own canonical local-community comment rows and AP object IDs.
    # Message surface helpers below keep Discord message placement separate.
    # ---------------------------------------------------------------------------

    def create_local_community_message(self, *, local_community_thread_id: int, discord_message_id: int, ap_activity_id: str, ap_object_id: str, parent_ap_object_id: str | None, parent_discord_message_id: int | None, direction: str) -> LocalCommunityMessage:
        """Temporarily forward to the Stage 3 local_community_content repository."""
        return self.local_community_content.create_local_community_message(local_community_thread_id=local_community_thread_id, discord_message_id=discord_message_id, ap_activity_id=ap_activity_id, ap_object_id=ap_object_id, parent_ap_object_id=parent_ap_object_id, parent_discord_message_id=parent_discord_message_id, direction=direction)

    def create_local_community_message_canonical(self, *, local_community_thread_id: int, ap_activity_id: str, ap_object_id: str, parent_ap_object_id: str | None, direction: str) -> LocalCommunityMessage:
        """Temporarily forward to the Stage 3 local_community_content repository."""
        return self.local_community_content.create_local_community_message_canonical(local_community_thread_id=local_community_thread_id, ap_activity_id=ap_activity_id, ap_object_id=ap_object_id, parent_ap_object_id=parent_ap_object_id, direction=direction)

    # ---------------------------------------------------------------------------
    # Local-community message surface helpers
    #
    # These helpers map canonical local-community comments to concrete Discord
    # message surfaces for host and local-subscriber forums.
    # ---------------------------------------------------------------------------

    def create_local_community_message_surface(self, *, local_community_message_id: int, local_community_thread_surface_id: int, discord_forum_channel_id: int, discord_message_id: int, parent_discord_message_id: int | None, role: str, local_subscriber_id: int | None=None) -> LocalCommunityMessageSurface:
        """Temporarily forward to the Stage 3 local_community_surfaces repository."""
        return self.local_community_surfaces.create_local_community_message_surface(local_community_message_id=local_community_message_id, local_community_thread_surface_id=local_community_thread_surface_id, discord_forum_channel_id=discord_forum_channel_id, discord_message_id=discord_message_id, parent_discord_message_id=parent_discord_message_id, role=role, local_subscriber_id=local_subscriber_id)

    def get_local_community_message_surface(self, *, local_community_message_id: int, local_community_thread_surface_id: int) -> LocalCommunityMessageSurface | None:
        """Temporarily forward to the Stage 3 local_community_surfaces repository."""
        return self.local_community_surfaces.get_local_community_message_surface(local_community_message_id=local_community_message_id, local_community_thread_surface_id=local_community_thread_surface_id)

    def get_local_community_message_surface_by_discord_message_id(self, discord_message_id: int) -> LocalCommunityMessageSurface | None:
        """Temporarily forward to the Stage 3 local_community_surfaces repository."""
        return self.local_community_surfaces.get_local_community_message_surface_by_discord_message_id(discord_message_id)

    def list_local_community_message_surfaces(self, local_community_message_id: int) -> list[LocalCommunityMessageSurface]:
        """Temporarily forward to the Stage 3 local_community_surfaces repository."""
        return self.local_community_surfaces.list_local_community_message_surfaces(local_community_message_id)

    def get_host_local_community_message_surface(self, local_community_message_id: int) -> LocalCommunityMessageSurface | None:
        """Temporarily forward to the Stage 3 local_community_surfaces repository."""
        return self.local_community_surfaces.get_host_local_community_message_surface(local_community_message_id)

    def get_local_community_thread_surface_by_id(self, local_community_thread_surface_id: int) -> LocalCommunityThreadSurface | None:
        """Temporarily forward to the Stage 3 local_community_surfaces repository."""
        return self.local_community_surfaces.get_local_community_thread_surface_by_id(local_community_thread_surface_id)

    def get_local_community_thread_for_surface(self, local_community_thread_surface_id: int) -> LocalCommunityThread | None:
        """Temporarily forward to the Stage 3 local_community_surfaces repository."""
        return self.local_community_surfaces.get_local_community_thread_for_surface(local_community_thread_surface_id)

    def get_local_community_message_for_surface(self, local_community_message_surface_id: int) -> LocalCommunityMessage | None:
        """Temporarily forward to the Stage 3 local_community_surfaces repository."""
        return self.local_community_surfaces.get_local_community_message_for_surface(local_community_message_surface_id)

    def get_local_community_message_by_ap_object_id(self, ap_object_id: str) -> LocalCommunityMessage | None:
        """Temporarily forward to the Stage 3 local_community_content repository."""
        return self.local_community_content.get_local_community_message_by_ap_object_id(ap_object_id)

    def list_local_community_messages_for_thread(self, local_community_thread_id: int) -> list[LocalCommunityMessage]:
        """Temporarily forward to the Stage 3 local_community_content repository."""
        return self.local_community_content.list_local_community_messages_for_thread(local_community_thread_id)

    def get_local_community_thread_by_id(self, local_community_thread_id: int) -> LocalCommunityThread | None:
        """Temporarily forward to the Stage 3 local_community_content repository."""
        return self.local_community_content.get_local_community_thread_by_id(local_community_thread_id)


    # ---------------------------------------------------------------------------
    # Local-community relay source and delivery helpers
    #
    # These helpers persist source activity rows and per-follower delivery
    # attempts for local-community federation fanout. Stage 3 extracts them
    # without changing accepted-subscriber targeting or retry semantics.
    # ---------------------------------------------------------------------------

    def get_or_create_local_community_relay_source_activity(self, *, local_community_id: int, object_kind: str, operation: str, source_object_ap_id: str, source_activity_id: str, source_announce_id: str | None, origin_remote_actor_id: str, source_activity_json: dict) -> LocalCommunityRelaySourceActivity:
        """Temporarily forward to the Stage 3 local_community_relay repository."""
        return self.local_community_relay.get_or_create_local_community_relay_source_activity(local_community_id=local_community_id, object_kind=object_kind, operation=operation, source_object_ap_id=source_object_ap_id, source_activity_id=source_activity_id, source_announce_id=source_announce_id, origin_remote_actor_id=origin_remote_actor_id, source_activity_json=source_activity_json)

    def list_local_community_relay_deliveries_for_source(self, source_activity_row_id: int) -> list[LocalCommunityRelayDelivery]:
        """Temporarily forward to the Stage 3 local_community_relay repository."""
        return self.local_community_relay.list_local_community_relay_deliveries_for_source(source_activity_row_id)

    def create_missing_local_community_relay_deliveries(self, *, source_activity: LocalCommunityRelaySourceActivity, targets: list[dict[str, str]]) -> list[LocalCommunityRelayDelivery]:
        """Temporarily forward to the Stage 3 local_community_relay repository."""
        return self.local_community_relay.create_missing_local_community_relay_deliveries(source_activity=source_activity, targets=targets)

    def list_delivered_local_community_create_relay_targets(self, *, local_community_id: int, source_object_ap_id: str) -> list[LocalCommunityRelayDelivery]:
        """Temporarily forward to the Stage 3 local_community_relay repository."""
        return self.local_community_relay.list_delivered_local_community_create_relay_targets(local_community_id=local_community_id, source_object_ap_id=source_object_ap_id)

    def mark_local_community_relay_delivery_result(self, *, delivery_id: int, status: str, relay_activity_id: str | None=None, error: str | None=None) -> LocalCommunityRelayDelivery | None:
        """Temporarily forward to the Stage 3 local_community_relay repository."""
        return self.local_community_relay.mark_local_community_relay_delivery_result(delivery_id=delivery_id, status=status, relay_activity_id=relay_activity_id, error=error)

    def get_local_community_relay_source_activity(self, *, local_community_id: int, operation: str, source_object_ap_id: str, source_activity_id: str) -> LocalCommunityRelaySourceActivity | None:
        """Temporarily forward to the Stage 3 local_community_relay repository."""
        return self.local_community_relay.get_local_community_relay_source_activity(local_community_id=local_community_id, operation=operation, source_object_ap_id=source_object_ap_id, source_activity_id=source_activity_id)

    # ---------------------------------------------------------------------------
    # User listing helper
    #
    # This dashboard/export helper remains with user persistence in the Stage 0
    # inventory. Message mapping and published object helpers start below.
    # ---------------------------------------------------------------------------

    def list_users(self) -> list[User]:
        """Temporarily forward to the Stage 5 users repository."""
        return self.users.list_users()

    # ---------------------------------------------------------------------------
    # Generic source-to-ActivityPub message mapping helpers
    #
    # These helpers own source/activity/object/Discord ID mappings used for
    # deduplication and edit/delete lookup. Stage 6 extracts them without
    # changing generated ActivityPub IDs or fallback compatibility.
    # ---------------------------------------------------------------------------

    def create_message_mapping(self, *, source_platform: str, source_id: str, activity_id: str, object_id: str, actor_url: str, community_actor_url: str, discord_channel_id: int | None, discord_message_id: int | None) -> MessageMapping:
        """Temporarily forward to the Stage 6 message_mappings repository."""
        return self.message_mappings.create_message_mapping(source_platform=source_platform, source_id=source_id, activity_id=activity_id, object_id=object_id, actor_url=actor_url, community_actor_url=community_actor_url, discord_channel_id=discord_channel_id, discord_message_id=discord_message_id)

    def get_message_mapping_by_activity_id(self, activity_id: str) -> MessageMapping | None:
        """Temporarily forward to the Stage 6 message_mappings repository."""
        return self.message_mappings.get_message_mapping_by_activity_id(activity_id)

    def get_message_mapping_by_object_id(self, object_id: str) -> MessageMapping | None:
        """Temporarily forward to the Stage 6 message_mappings repository."""
        return self.message_mappings.get_message_mapping_by_object_id(object_id)

    def get_message_mapping_by_discord_message_id(self, discord_message_id: int) -> MessageMapping | None:
        """Temporarily forward to the Stage 6 message_mappings repository."""
        return self.message_mappings.get_message_mapping_by_discord_message_id(discord_message_id)

    # ---------------------------------------------------------------------------
    # Published ActivityPub object helpers
    #
    # These helpers persist gateway-published ActivityPub objects for later
    # resolution and serving. Stage 6 extracts them without changing stored JSON
    # lookup semantics or object URL compatibility.
    # ---------------------------------------------------------------------------

    def create_published_activity_object(self, *, actor_username: str, actor_url: str, community_actor_url: str, activity_id: str, object_id: str, kind: str, title: str | None, body_markdown: str, in_reply_to_object_id: str | None, discord_channel_id: int | None, discord_message_id: int | None, published_at: datetime | None=None) -> PublishedActivityObject:
        """Temporarily forward to the Stage 6 activitypub_objects repository."""
        return self.activitypub_objects.create_published_activity_object(actor_username=actor_username, actor_url=actor_url, community_actor_url=community_actor_url, activity_id=activity_id, object_id=object_id, kind=kind, title=title, body_markdown=body_markdown, in_reply_to_object_id=in_reply_to_object_id, discord_channel_id=discord_channel_id, discord_message_id=discord_message_id, published_at=published_at)

    def get_published_activity_object_by_object_id(self, object_id: str) -> PublishedActivityObject | None:
        """Temporarily forward to the Stage 6 activitypub_objects repository."""
        return self.activitypub_objects.get_published_activity_object_by_object_id(object_id)

    def get_published_activity_object_by_discord_message_id(self, discord_message_id: int) -> PublishedActivityObject | None:
        """Temporarily forward to the Stage 6 activitypub_objects repository."""
        return self.activitypub_objects.get_published_activity_object_by_discord_message_id(discord_message_id)

    # ---------------------------------------------------------------------------
    # Remote actor cache helpers
    #
    # These helpers cache remote ActivityPub actor addressing and public-key
    # metadata. Stage 6 extracts them without changing actor URL, inbox, shared
    # inbox, or key lookup semantics.
    # ---------------------------------------------------------------------------

    def upsert_remote_actor(self, *, actor_url: str, preferred_username: str | None, inbox_url: str, shared_inbox_url: str | None, public_key_pem: str) -> RemoteActor:
        """Temporarily forward to the Stage 6 remote_actors repository."""
        return self.remote_actors.upsert_remote_actor(actor_url=actor_url, preferred_username=preferred_username, inbox_url=inbox_url, shared_inbox_url=shared_inbox_url, public_key_pem=public_key_pem)

    def get_remote_actor_by_actor_url(self, actor_url: str) -> RemoteActor | None:
        """Temporarily forward to the Stage 6 remote_actors repository."""
        return self.remote_actors.get_remote_actor_by_actor_url(actor_url)

    # ---------------------------------------------------------------------------
    # Shared Discord thread fanout group helpers
    #
    # These helpers own logical thread groups and per-channel thread deliveries
    # for remote ActivityPub fanout. Stage 7 extracts them with message fanout
    # group helpers while preserving dedup and reply/edit/delete lookup paths.
    # ---------------------------------------------------------------------------

    def create_thread_group(self, *, community_actor_id: str, source_channel_id: int | None, source_thread_id: int | None, source_starter_message_id: int | None, ap_activity_id: str | None=None, ap_object_id: str | None=None) -> CommunityThreadGroup:
        """Temporarily forward to the Stage 7 discord_fanout_groups repository."""
        return self.discord_fanout_groups.create_thread_group(community_actor_id=community_actor_id, source_channel_id=source_channel_id, source_thread_id=source_thread_id, source_starter_message_id=source_starter_message_id, ap_activity_id=ap_activity_id, ap_object_id=ap_object_id)

    def get_thread_group_by_source_thread(self, source_thread_id: int) -> CommunityThreadGroup | None:
        """Temporarily forward to the Stage 7 discord_fanout_groups repository."""
        return self.discord_fanout_groups.get_thread_group_by_source_thread(source_thread_id)

    def get_thread_group_by_ap_object(self, ap_object_id: str) -> CommunityThreadGroup | None:
        """Temporarily forward to the Stage 7 discord_fanout_groups repository."""
        return self.discord_fanout_groups.get_thread_group_by_ap_object(ap_object_id)

    def get_thread_group_by_id(self, thread_group_id: int) -> CommunityThreadGroup | None:
        """Temporarily forward to the Stage 7 discord_fanout_groups repository."""
        return self.discord_fanout_groups.get_thread_group_by_id(thread_group_id)

    def get_thread_group_by_any_thread(self, discord_thread_id: int) -> CommunityThreadGroup | None:
        """Temporarily forward to the Stage 7 discord_fanout_groups repository."""
        return self.discord_fanout_groups.get_thread_group_by_any_thread(discord_thread_id)

    def add_thread_delivery(self, *, thread_group_id: int, discord_channel_id: int, discord_thread_id: int, discord_starter_message_id: int, role: str) -> CommunityThreadGroupDelivery:
        """Temporarily forward to the Stage 7 discord_fanout_groups repository."""
        return self.discord_fanout_groups.add_thread_delivery(thread_group_id=thread_group_id, discord_channel_id=discord_channel_id, discord_thread_id=discord_thread_id, discord_starter_message_id=discord_starter_message_id, role=role)

    def get_thread_deliveries(self, thread_group_id: int) -> list[CommunityThreadGroupDelivery]:
        """Temporarily forward to the Stage 7 discord_fanout_groups repository."""
        return self.discord_fanout_groups.get_thread_deliveries(thread_group_id)

    def get_thread_delivery_by_thread(self, discord_thread_id: int) -> CommunityThreadGroupDelivery | None:
        """Temporarily forward to the Stage 7 discord_fanout_groups repository."""
        return self.discord_fanout_groups.get_thread_delivery_by_thread(discord_thread_id)

    # ---------------------------------------------------------------------------
    # Shared Discord message fanout group helpers
    #
    # These helpers own logical message groups and per-channel Discord message
    # deliveries for remote ActivityPub fanout. They stay in-place until the
    # Stage 7 DiscordFanoutGroupRepository extraction.
    # ---------------------------------------------------------------------------

    def create_message_group(self, *, community_actor_id: str, thread_group_id: int, source_channel_id: int | None, source_thread_id: int | None, source_message_id: int | None, ap_activity_id: str | None=None, ap_object_id: str | None=None, parent_message_group_id: int | None=None) -> CommunityMessageGroup:
        """Temporarily forward to the Stage 7 discord_fanout_groups repository."""
        return self.discord_fanout_groups.create_message_group(community_actor_id=community_actor_id, thread_group_id=thread_group_id, source_channel_id=source_channel_id, source_thread_id=source_thread_id, source_message_id=source_message_id, ap_activity_id=ap_activity_id, ap_object_id=ap_object_id, parent_message_group_id=parent_message_group_id)

    def get_message_group_by_id(self, message_group_id: int) -> CommunityMessageGroup | None:
        """Temporarily forward to the Stage 7 discord_fanout_groups repository."""
        return self.discord_fanout_groups.get_message_group_by_id(message_group_id)

    def get_message_group_by_source_message(self, source_message_id: int) -> CommunityMessageGroup | None:
        """Temporarily forward to the Stage 7 discord_fanout_groups repository."""
        return self.discord_fanout_groups.get_message_group_by_source_message(source_message_id)

    def get_message_group_by_ap_object(self, ap_object_id: str) -> CommunityMessageGroup | None:
        """Temporarily forward to the Stage 7 discord_fanout_groups repository."""
        return self.discord_fanout_groups.get_message_group_by_ap_object(ap_object_id)

    def get_message_group_by_delivered_message(self, discord_message_id: int) -> CommunityMessageGroup | None:
        """Temporarily forward to the Stage 7 discord_fanout_groups repository."""
        return self.discord_fanout_groups.get_message_group_by_delivered_message(discord_message_id)

    def add_message_delivery(self, *, message_group_id: int, discord_channel_id: int, discord_thread_id: int, discord_message_id: int, role: str) -> CommunityMessageGroupDelivery:
        """Temporarily forward to the Stage 7 discord_fanout_groups repository."""
        return self.discord_fanout_groups.add_message_delivery(message_group_id=message_group_id, discord_channel_id=discord_channel_id, discord_thread_id=discord_thread_id, discord_message_id=discord_message_id, role=role)

    def get_message_delivery_by_message(self, discord_message_id: int) -> CommunityMessageGroupDelivery | None:
        """Temporarily forward to the Stage 7 discord_fanout_groups repository."""
        return self.discord_fanout_groups.get_message_delivery_by_message(discord_message_id)

    def get_message_deliveries(self, message_group_id: int) -> list[CommunityMessageGroupDelivery]:
        """Temporarily forward to the Stage 7 discord_fanout_groups repository."""
        return self.discord_fanout_groups.get_message_deliveries(message_group_id)

    def get_message_delivery_in_thread(self, message_group_id: int, discord_thread_id: int) -> CommunityMessageGroupDelivery | None:
        """Temporarily forward to the Stage 7 discord_fanout_groups repository."""
        return self.discord_fanout_groups.get_message_delivery_in_thread(message_group_id, discord_thread_id)

    # ---------------------------------------------------------------------------
    # Bridge actor follow helpers
    #
    # These helpers own the shared bridge actor Follow lifecycle for remote
    # communities. Stage 4 extracts them with remote subscriptions without
    # reintroducing legacy direct-follow acceptance behavior.
    # ---------------------------------------------------------------------------

    def list_bridge_actor_follows(self) -> list[BridgeActorFollow]:
        """Temporarily forward to the Stage 4 bridge_actor_follows repository."""
        return self.bridge_actor_follows.list_bridge_actor_follows()

    def get_bridge_actor_follow(self, community_actor_id: str) -> BridgeActorFollow | None:
        """Temporarily forward to the Stage 4 bridge_actor_follows repository."""
        return self.bridge_actor_follows.get_bridge_actor_follow(community_actor_id)

    def get_bridge_actor_follow_by_follow_activity_id(self, follow_activity_id: str) -> BridgeActorFollow | None:
        """Temporarily forward to the Stage 4 bridge_actor_follows repository."""
        return self.bridge_actor_follows.get_bridge_actor_follow_by_follow_activity_id(follow_activity_id)

    def create_bridge_actor_follow(self, *, community_actor_id: str, follow_activity_id: str | None, community_inbox_url: str | None, status: str='pending') -> BridgeActorFollow:
        """Temporarily forward to the Stage 4 bridge_actor_follows repository."""
        return self.bridge_actor_follows.create_bridge_actor_follow(community_actor_id=community_actor_id, follow_activity_id=follow_activity_id, community_inbox_url=community_inbox_url, status=status)

    def mark_bridge_actor_follow_accepted(self, community_actor_id: str) -> BridgeActorFollow:
        """Temporarily forward to the Stage 4 bridge_actor_follows repository."""
        return self.bridge_actor_follows.mark_bridge_actor_follow_accepted(community_actor_id)

    def delete_bridge_actor_follow(self, community_actor_id: str) -> bool:
        """Temporarily forward to the Stage 4 bridge_actor_follows repository."""
        return self.bridge_actor_follows.delete_bridge_actor_follow(community_actor_id)

    def count_subscriptions_for_community(self, community_actor_id: str) -> int:
        """Temporarily forward to the Stage 4 remote_subscriptions repository."""
        return self.remote_subscriptions.count_subscriptions_for_community(community_actor_id)

    def get_pending_channel_subscriptions_for_community(self, community_actor_id: str) -> list[ChannelCommunitySubscription]:
        """Temporarily forward to the Stage 4 remote_subscriptions repository."""
        return self.remote_subscriptions.get_pending_channel_subscriptions_for_community(community_actor_id)
