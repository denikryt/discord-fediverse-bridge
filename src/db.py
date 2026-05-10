"""Repository helpers for bridge routing, identity, and federation state."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from .models import (
    ActivityPubEventReceipt,
    Base,
    ChannelCommunitySubscription,
    CommentLink,
    CommunityMessageGroup,
    CommunityMessageGroupDelivery,
    CommunityThreadGroup,
    CommunityThreadGroupDelivery,
    MessageMapping,
    PostLink,
    PublishedActivityObject,
    RegistrationSession,
    RemoteActor,
    User,
    utcnow,
)


class Database:
    """Wrap ORM access behind intent-specific repository methods."""

    # Database is a small repository-style wrapper that keeps bridge code away
    # from session management and direct ORM details.
    def __init__(self, url: str) -> None:
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        self.engine = create_engine(url, future=True, connect_args=connect_args)
        self.session_factory = sessionmaker(self.engine, expire_on_commit=False, class_=Session)

    def create_all(self) -> None:
        """Create the full clean-schema set required by the current codebase."""
        Base.metadata.create_all(self.engine)

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

    def get_post_link_by_thread_id(self, discord_thread_id: int) -> PostLink | None:
        """Load the post-link row for one Discord thread, if it exists."""
        with self.session() as session:
            return session.scalar(select(PostLink).where(PostLink.discord_forum_thread_id == discord_thread_id))

    def get_post_link_by_lemmy_post_id(self, lemmy_post_id: int) -> PostLink | None:
        """Load the post-link row for one numeric Lemmy post ID."""
        with self.session() as session:
            return session.scalar(select(PostLink).where(PostLink.lemmy_post_id == lemmy_post_id))

    def get_post_link_by_lemmy_post_ap_id(self, lemmy_post_ap_id: str) -> PostLink | None:
        """Load one post-link row for an ActivityPub Lemmy post ID.

        This compatibility helper returns the first matching row. Newer
        multi-target inbound routing should prefer
        `get_post_links_by_lemmy_post_ap_id()`.
        """
        with self.session() as session:
            return session.scalar(select(PostLink).where(PostLink.lemmy_post_ap_id == lemmy_post_ap_id))

    def get_post_links_by_lemmy_post_ap_id(self, lemmy_post_ap_id: str) -> list[PostLink]:
        """Load every post-link row for one ActivityPub Lemmy post ID."""
        # Multi-channel inbound fanout needs every Discord thread that already
        # represents the same remote post, not just the first row.
        with self.session() as session:
            return list(
                session.scalars(
                    select(PostLink).where(PostLink.lemmy_post_ap_id == lemmy_post_ap_id)
                )
            )

    def get_post_link_by_lemmy_post_ap_id_and_channel_id(
        self,
        lemmy_post_ap_id: str,
        discord_forum_channel_id: int,
    ) -> PostLink | None:
        """Load the post-link row for one remote post delivered into one forum channel."""
        # Fanout retries must be able to see whether a specific subscribed
        # target channel already received the remote post.
        with self.session() as session:
            return session.scalar(
                select(PostLink).where(
                    PostLink.lemmy_post_ap_id == lemmy_post_ap_id,
                    PostLink.discord_forum_channel_id == discord_forum_channel_id,
                )
            )

    def create_post_link(
        self,
        *,
        lemmy_post_id: int,
        lemmy_post_ap_id: str | None,
        discord_forum_channel_id: int | None = None,
        discord_forum_thread_id: int,
        discord_starter_message_id: int | None,
        direction: str,
    ) -> PostLink:
        """Persist the thread-level mapping created by the existing bridge."""
        with self.session() as session:
            link = PostLink(
                lemmy_post_id=lemmy_post_id,
                lemmy_post_ap_id=lemmy_post_ap_id,
                discord_forum_channel_id=discord_forum_channel_id,
                discord_forum_thread_id=discord_forum_thread_id,
                discord_starter_message_id=discord_starter_message_id,
                direction=direction,
            )
            session.add(link)
            session.flush()
            return link

    def has_comment_link_for_discord_message(self, discord_message_id: int) -> bool:
        """Return whether one Discord message is already mapped as a comment."""
        with self.session() as session:
            return session.scalar(select(CommentLink.id).where(CommentLink.discord_message_id == discord_message_id)) is not None

    def has_comment_link_for_lemmy_comment(self, lemmy_comment_id: int) -> bool:
        """Return whether one Lemmy comment is already mapped into Discord."""
        with self.session() as session:
            return session.scalar(select(CommentLink.id).where(CommentLink.lemmy_comment_id == lemmy_comment_id)) is not None

    def get_comment_link_by_lemmy_comment_ap_id(self, lemmy_comment_ap_id: str) -> CommentLink | None:
        """Load one comment-link row for one ActivityPub comment ID.

        This compatibility helper returns the first matching row. Newer
        multi-target inbound routing should prefer
        `get_comment_links_by_lemmy_comment_ap_id()`.
        """
        with self.session() as session:
            return session.scalar(select(CommentLink).where(CommentLink.lemmy_comment_ap_id == lemmy_comment_ap_id))

    def get_comment_links_by_lemmy_comment_ap_id(self, lemmy_comment_ap_id: str) -> list[CommentLink]:
        """Load every comment-link row for one ActivityPub comment ID."""
        # Fanout routing and parent resolution both need to see all thread-local
        # copies of the same remote comment.
        with self.session() as session:
            return list(
                session.scalars(
                    select(CommentLink).where(CommentLink.lemmy_comment_ap_id == lemmy_comment_ap_id)
                )
            )

    def get_comment_link_by_discord_message_id(self, discord_message_id: int) -> CommentLink | None:
        """Load the comment-link row for one Discord message ID."""
        with self.session() as session:
            return session.scalar(select(CommentLink).where(CommentLink.discord_message_id == discord_message_id))

    def create_comment_link(
        self,
        *,
        lemmy_comment_id: int,
        lemmy_comment_ap_id: str | None,
        lemmy_parent_comment_ap_id: str | None,
        lemmy_post_id: int,
        discord_forum_thread_id: int,
        discord_message_id: int,
        direction: str,
    ) -> CommentLink:
        """Persist the message-level mapping created by the existing bridge."""
        with self.session() as session:
            link = CommentLink(
                lemmy_comment_id=lemmy_comment_id,
                lemmy_comment_ap_id=lemmy_comment_ap_id,
                lemmy_parent_comment_ap_id=lemmy_parent_comment_ap_id,
                lemmy_post_id=lemmy_post_id,
                discord_forum_thread_id=discord_forum_thread_id,
                discord_message_id=discord_message_id,
                direction=direction,
            )
            session.add(link)
            session.flush()
            return link

    def get_event_receipt(self, delivery_id: str) -> ActivityPubEventReceipt | None:
        """Load the receipt row for one inbound delivery ID."""
        with self.session() as session:
            return session.scalar(select(ActivityPubEventReceipt).where(ActivityPubEventReceipt.delivery_id == delivery_id))

    def create_event_receipt(self, *, delivery_id: str, event_type: str, object_ap_id: str, status: str, detail: str | None = None) -> ActivityPubEventReceipt:
        """Create the receipt row that gates idempotent event processing."""
        with self.session() as session:
            receipt = ActivityPubEventReceipt(
                delivery_id=delivery_id,
                event_type=event_type,
                object_ap_id=object_ap_id,
                status=status,
                detail=detail,
            )
            session.add(receipt)
            session.flush()
            return receipt

    def update_event_receipt(self, *, delivery_id: str, status: str, detail: str | None = None) -> None:
        """Update one inbound delivery receipt after processing progresses."""
        with self.session() as session:
            receipt = session.scalar(select(ActivityPubEventReceipt).where(ActivityPubEventReceipt.delivery_id == delivery_id))
            if receipt is None:
                raise RuntimeError(f"Missing receipt for delivery {delivery_id}")
            receipt.status = status
            receipt.detail = detail

    def get_subscription_by_channel(self, discord_channel_id: int) -> ChannelCommunitySubscription | None:
        """Load the single community subscription owned by one Discord channel."""
        # Each channel maps to at most one community, so this is a point lookup.
        with self.session() as session:
            return session.scalar(select(ChannelCommunitySubscription).where(ChannelCommunitySubscription.discord_channel_id == discord_channel_id))

    def get_subscriptions_by_community(self, lemmy_community_actor_id: str) -> list[ChannelCommunitySubscription]:
        """Load every accepted Discord subscription for one Lemmy community."""
        # Inbound routing only uses subscriptions that completed the Follow ->
        # Accept lifecycle. Pending/failed rows are visible to moderator flows
        # but must not fan out remote content into Discord.
        with self.session() as session:
            return list(
                session.scalars(
                    select(ChannelCommunitySubscription).where(
                        ChannelCommunitySubscription.lemmy_community_actor_id
                        == lemmy_community_actor_id,
                        ChannelCommunitySubscription.status == "accepted",
                    )
                )
            )

    def get_all_subscriptions(self) -> list[ChannelCommunitySubscription]:
        """Return all subscription rows in stable creation order."""
        # Ordered by creation time so the /list-subscriptions command shows a
        # stable, predictable list.
        with self.session() as session:
            return list(session.scalars(select(ChannelCommunitySubscription).order_by(ChannelCommunitySubscription.created_at)))

    def create_subscription(
        self,
        *,
        discord_channel_id: int,
        lemmy_community_actor_id: str,
        lemmy_community_name: str | None,
        lemmy_community_id: int | None,
        community_handle: str | None = None,
        community_inbox_url: str | None = None,
        follow_activity_id: str | None = None,
        initiated_by_discord_user_id: str | None = None,
        status: str = "pending",
    ) -> ChannelCommunitySubscription:
        """Create one channel-to-community subscription row with follow state."""
        # Callers are responsible for checking uniqueness before calling this;
        # the DB UNIQUE constraint on discord_channel_id is the final safety net
        # and will raise IntegrityError if a duplicate is attempted.
        with self.session() as session:
            sub = ChannelCommunitySubscription(
                discord_channel_id=discord_channel_id,
                lemmy_community_actor_id=lemmy_community_actor_id,
                lemmy_community_name=lemmy_community_name,
                lemmy_community_id=lemmy_community_id,
                community_handle=community_handle,
                community_inbox_url=community_inbox_url,
                follow_activity_id=follow_activity_id,
                initiated_by_discord_user_id=initiated_by_discord_user_id,
                status=status,
            )
            session.add(sub)
            session.flush()
            return sub

    def update_subscription_follow_state(
        self,
        *,
        discord_channel_id: int,
        community_handle: str | None = None,
        community_inbox_url: str | None,
        follow_activity_id: str | None,
        status: str,
    ) -> None:
        """Update the federation follow state for one existing subscription."""
        # Follow state lives on the subscription row because later stages need a
        # single source of truth for whether the bridge is pending/accepted.
        with self.session() as session:
            subscription = session.scalar(
                select(ChannelCommunitySubscription).where(
                    ChannelCommunitySubscription.discord_channel_id
                    == discord_channel_id
                )
            )
            if subscription is None:
                raise RuntimeError(
                    f"Missing subscription for Discord channel {discord_channel_id}"
                )
            subscription.community_handle = community_handle
            subscription.community_inbox_url = community_inbox_url
            subscription.follow_activity_id = follow_activity_id
            subscription.status = status

    def get_subscription_by_follow_activity_id(
        self, follow_activity_id: str
    ) -> ChannelCommunitySubscription | None:
        """Load the subscription row that owns one outbound Follow activity."""
        with self.session() as session:
            return session.scalar(
                select(ChannelCommunitySubscription).where(
                    ChannelCommunitySubscription.follow_activity_id
                    == follow_activity_id
                )
            )

    def mark_subscription_accepted_by_follow_activity_id(
        self, follow_activity_id: str
    ) -> ChannelCommunitySubscription:
        """Mark one subscription as accepted after Lemmy confirms the follow."""
        # Matching by follow activity ID makes the Accept handler idempotent and
        # avoids guessing based only on community actor or channel.
        with self.session() as session:
            subscription = session.scalar(
                select(ChannelCommunitySubscription).where(
                    ChannelCommunitySubscription.follow_activity_id
                    == follow_activity_id
                )
            )
            if subscription is None:
                raise RuntimeError(
                    f"Missing subscription for follow activity {follow_activity_id}"
                )
            subscription.status = "accepted"
            session.flush()
            return subscription

    def delete_subscription(self, discord_channel_id: int) -> bool:
        """Delete one channel subscription if it exists."""
        # Returns False when no subscription exists so callers can give a
        # meaningful response without a separate existence check.
        with self.session() as session:
            sub = session.scalar(select(ChannelCommunitySubscription).where(ChannelCommunitySubscription.discord_channel_id == discord_channel_id))
            if sub is None:
                return False
            session.delete(sub)
            return True

    def create_user(
        self,
        *,
        discord_user_id: str,
        activitypub_username: str,
        actor_url: str,
        inbox_url: str,
        outbox_url: str,
        followers_url: str,
        public_key_pem: str,
        private_key_pem: str,
    ) -> User:
        """Create the shared identity record for one registered Discord user."""
        with self.session() as session:
            user = User(
                discord_user_id=discord_user_id,
                activitypub_username=activitypub_username,
                actor_url=actor_url,
                inbox_url=inbox_url,
                outbox_url=outbox_url,
                followers_url=followers_url,
                public_key_pem=public_key_pem,
                private_key_pem=private_key_pem,
            )
            session.add(user)
            session.flush()
            return user

    def create_registration_session(
        self, *, session_token: str, expires_at: datetime
    ) -> RegistrationSession:
        """Create one server-side registration session row."""
        with self.session() as session:
            registration_session = RegistrationSession(
                session_token=session_token,
                expires_at=expires_at,
            )
            session.add(registration_session)
            session.flush()
            return registration_session

    def get_registration_session_by_token(
        self, session_token: str | None
    ) -> RegistrationSession | None:
        """Load one registration session by its opaque browser token."""
        if session_token is None:
            return None
        with self.session() as session:
            return session.scalar(
                select(RegistrationSession).where(
                    RegistrationSession.session_token == session_token
                )
            )

    def update_registration_session_oauth_state(
        self, *, session_token: str, oauth_state: str, expires_at: datetime
    ) -> RegistrationSession:
        """Persist the latest OAuth state for one browser registration flow."""
        # OAuth state must be stored server-side so the callback can reject
        # forged or replayed redirect attempts from outside Discord.
        with self.session() as session:
            registration_session = session.scalar(
                select(RegistrationSession).where(
                    RegistrationSession.session_token == session_token
                )
            )
            if registration_session is None:
                raise RuntimeError(
                    f"Missing registration session for token {session_token}"
                )
            registration_session.oauth_state = oauth_state
            registration_session.expires_at = expires_at
            registration_session.status = "oauth_started"
            session.flush()
            return registration_session

    def update_registration_session_discord_identity(
        self,
        *,
        session_token: str,
        discord_user_id: str,
        discord_username: str,
        discord_avatar_url: str | None,
        expires_at: datetime,
    ) -> RegistrationSession:
        """Attach the authenticated Discord identity to one session."""
        with self.session() as session:
            registration_session = session.scalar(
                select(RegistrationSession).where(
                    RegistrationSession.session_token == session_token
                )
            )
            if registration_session is None:
                raise RuntimeError(
                    f"Missing registration session for token {session_token}"
                )
            registration_session.discord_user_id = discord_user_id
            registration_session.discord_username = discord_username
            registration_session.discord_avatar_url = discord_avatar_url
            registration_session.expires_at = expires_at
            registration_session.status = "discord_authenticated"
            session.flush()
            return registration_session

    def mark_registration_session_completed(
        self, *, session_token: str, activitypub_username: str, expires_at: datetime
    ) -> RegistrationSession:
        """Mark one registration session complete after the user row is created."""
        with self.session() as session:
            registration_session = session.scalar(
                select(RegistrationSession).where(
                    RegistrationSession.session_token == session_token
                )
            )
            if registration_session is None:
                raise RuntimeError(
                    f"Missing registration session for token {session_token}"
                )
            registration_session.activitypub_username = activitypub_username
            registration_session.expires_at = expires_at
            registration_session.status = "completed"
            session.flush()
            return registration_session

    def get_user_by_discord_user_id(self, discord_user_id: str) -> User | None:
        """Load the registered user that owns one Discord account ID."""
        with self.session() as session:
            return session.scalar(
                select(User).where(User.discord_user_id == discord_user_id)
            )

    def get_user_by_activitypub_username(
        self, activitypub_username: str
    ) -> User | None:
        """Load the registered user that owns one local AP username."""
        with self.session() as session:
            return session.scalar(
                select(User).where(
                    User.activitypub_username == activitypub_username
                )
            )

    def get_user_by_actor_url(self, actor_url: str) -> User | None:
        """Load the registered user that owns one actor URL."""
        with self.session() as session:
            return session.scalar(select(User).where(User.actor_url == actor_url))

    def list_users(self) -> list[User]:
        """Return all registered users in stable creation order."""
        # User identity export needs a deterministic ordering so repeated dumps
        # can be diffed and restored without hidden row-order changes.
        with self.session() as session:
            return list(
                session.scalars(select(User).order_by(User.created_at, User.id))
            )

    def create_message_mapping(
        self,
        *,
        source_platform: str,
        source_id: str,
        activity_id: str,
        object_id: str,
        actor_url: str,
        community_actor_url: str,
        discord_channel_id: int | None,
        discord_message_id: int | None,
    ) -> MessageMapping:
        """Create the generic dedup record used by later AP publish flows."""
        with self.session() as session:
            mapping = MessageMapping(
                source_platform=source_platform,
                source_id=source_id,
                activity_id=activity_id,
                object_id=object_id,
                actor_url=actor_url,
                community_actor_url=community_actor_url,
                discord_channel_id=discord_channel_id,
                discord_message_id=discord_message_id,
            )
            session.add(mapping)
            session.flush()
            return mapping

    def get_message_mapping_by_activity_id(
        self, activity_id: str
    ) -> MessageMapping | None:
        """Load a generic mapping row by ActivityPub activity ID."""
        with self.session() as session:
            return session.scalar(
                select(MessageMapping).where(MessageMapping.activity_id == activity_id)
            )

    def get_message_mapping_by_object_id(
        self, object_id: str
    ) -> MessageMapping | None:
        """Load a generic mapping row by ActivityPub object ID."""
        with self.session() as session:
            return session.scalar(
                select(MessageMapping).where(MessageMapping.object_id == object_id)
            )

    def get_message_mapping_by_discord_message_id(
        self, discord_message_id: int
    ) -> MessageMapping | None:
        """Load a generic mapping row by Discord message ID."""
        with self.session() as session:
            return session.scalar(
                select(MessageMapping).where(
                    MessageMapping.discord_message_id == discord_message_id
                )
            )

    def create_published_activity_object(
        self,
        *,
        actor_username: str,
        actor_url: str,
        community_actor_url: str,
        activity_id: str,
        object_id: str,
        kind: str,
        title: str | None,
        body_markdown: str,
        in_reply_to_object_id: str | None,
        discord_channel_id: int | None,
        discord_message_id: int | None,
        published_at: datetime | None = None,
    ) -> PublishedActivityObject:
        """Persist one canonical AP object emitted by the gateway publish path."""
        with self.session() as session:
            published_object = PublishedActivityObject(
                actor_username=actor_username,
                actor_url=actor_url,
                community_actor_url=community_actor_url,
                activity_id=activity_id,
                object_id=object_id,
                kind=kind,
                title=title,
                body_markdown=body_markdown,
                in_reply_to_object_id=in_reply_to_object_id,
                discord_channel_id=discord_channel_id,
                discord_message_id=discord_message_id,
                published_at=published_at or utcnow(),
            )
            session.add(published_object)
            session.flush()
            return published_object

    def get_published_activity_object_by_object_id(
        self, object_id: str
    ) -> PublishedActivityObject | None:
        """Load one stored gateway-published object by its canonical AP URL."""
        with self.session() as session:
            return session.scalar(
                select(PublishedActivityObject).where(
                    PublishedActivityObject.object_id == object_id
                )
            )

    def upsert_remote_actor(
        self,
        *,
        actor_url: str,
        preferred_username: str | None,
        inbox_url: str,
        shared_inbox_url: str | None,
        public_key_pem: str,
    ) -> RemoteActor:
        """Insert or refresh one cached remote actor record in place."""
        # Remote actor fetches are repeatable, so this method updates the
        # mutable addressing and key fields instead of creating duplicates.
        with self.session() as session:
            actor = session.scalar(
                select(RemoteActor).where(RemoteActor.actor_url == actor_url)
            )
            if actor is None:
                actor = RemoteActor(
                    actor_url=actor_url,
                    preferred_username=preferred_username,
                    inbox_url=inbox_url,
                    shared_inbox_url=shared_inbox_url,
                    public_key_pem=public_key_pem,
                )
                session.add(actor)
                session.flush()
                return actor

            actor.preferred_username = preferred_username
            actor.inbox_url = inbox_url
            actor.shared_inbox_url = shared_inbox_url
            actor.public_key_pem = public_key_pem
            actor.last_fetched_at = utcnow()
            session.flush()
            return actor

    def get_remote_actor_by_actor_url(self, actor_url: str) -> RemoteActor | None:
        """Load the cached record for one remote ActivityPub actor."""
        with self.session() as session:
            return session.scalar(
                select(RemoteActor).where(RemoteActor.actor_url == actor_url)
            )

    # --- CommunityThreadGroup repository methods ---
    # These methods are the Phase 2+ replacement for PostLink queries. All
    # methods are fully implemented so Phase 2 can start writing rows without
    # further schema changes. They return empty/None until rows exist.

    def create_thread_group(
        self,
        *,
        community_actor_id: str,
        source_channel_id: int | None,
        source_thread_id: int | None,
        source_starter_message_id: int | None,
        ap_activity_id: str | None = None,
        ap_object_id: str | None = None,
    ) -> CommunityThreadGroup:
        """Create the canonical thread-group record for one source thread event.

        source_* fields are None for inbound AP events, which create Discord threads
        in all subscribed channels simultaneously without a single source channel.
        """
        with self.session() as session:
            group = CommunityThreadGroup(
                community_actor_id=community_actor_id,
                source_channel_id=source_channel_id,
                source_thread_id=source_thread_id,
                source_starter_message_id=source_starter_message_id,
                ap_activity_id=ap_activity_id,
                ap_object_id=ap_object_id,
            )
            session.add(group)
            session.flush()
            return group

    def get_thread_group_by_source_thread(
        self, source_thread_id: int
    ) -> CommunityThreadGroup | None:
        """Load the thread group that owns one source Discord thread."""
        with self.session() as session:
            return session.scalar(
                select(CommunityThreadGroup).where(
                    CommunityThreadGroup.source_thread_id == source_thread_id
                )
            )

    def get_thread_group_by_ap_object(
        self, ap_object_id: str
    ) -> CommunityThreadGroup | None:
        """Load the thread group that maps to one ActivityPub object ID."""
        # Inbound AP→Discord routing uses the AP object ID to check whether a
        # thread was already processed to avoid duplicate Discord threads.
        with self.session() as session:
            return session.scalar(
                select(CommunityThreadGroup).where(
                    CommunityThreadGroup.ap_object_id == ap_object_id
                )
            )

    def add_thread_delivery(
        self,
        *,
        thread_group_id: int,
        discord_channel_id: int,
        discord_thread_id: int,
        discord_starter_message_id: int,
        role: str,
    ) -> CommunityThreadGroupDelivery:
        """Record one per-channel thread delivery for a CommunityThreadGroup."""
        with self.session() as session:
            delivery = CommunityThreadGroupDelivery(
                thread_group_id=thread_group_id,
                discord_channel_id=discord_channel_id,
                discord_thread_id=discord_thread_id,
                discord_starter_message_id=discord_starter_message_id,
                role=role,
            )
            session.add(delivery)
            session.flush()
            return delivery

    def get_thread_deliveries(
        self, thread_group_id: int
    ) -> list[CommunityThreadGroupDelivery]:
        """Load all delivery rows for one thread group."""
        # Fanout routing needs to know which channels have already received
        # a thread so it can skip those and only deliver to remaining targets.
        with self.session() as session:
            return list(
                session.scalars(
                    select(CommunityThreadGroupDelivery).where(
                        CommunityThreadGroupDelivery.thread_group_id == thread_group_id
                    )
                )
            )

    def get_thread_delivery_by_thread(
        self, discord_thread_id: int
    ) -> CommunityThreadGroupDelivery | None:
        """Load the delivery row for one Discord thread ID."""
        with self.session() as session:
            return session.scalar(
                select(CommunityThreadGroupDelivery).where(
                    CommunityThreadGroupDelivery.discord_thread_id == discord_thread_id
                )
            )

    # --- CommunityMessageGroup repository methods ---
    # These methods are the Phase 2+ replacement for CommentLink queries.

    def create_message_group(
        self,
        *,
        community_actor_id: str,
        thread_group_id: int,
        source_channel_id: int | None,
        source_thread_id: int | None,
        source_message_id: int | None,
        ap_activity_id: str | None = None,
        ap_object_id: str | None = None,
        parent_message_group_id: int | None = None,
    ) -> CommunityMessageGroup:
        """Create the canonical message-group record for one source message event.

        source_* fields are None for inbound AP events, which deliver into all
        subscribed threads simultaneously without a single source message.
        """
        with self.session() as session:
            group = CommunityMessageGroup(
                community_actor_id=community_actor_id,
                thread_group_id=thread_group_id,
                source_channel_id=source_channel_id,
                source_thread_id=source_thread_id,
                source_message_id=source_message_id,
                ap_activity_id=ap_activity_id,
                ap_object_id=ap_object_id,
                parent_message_group_id=parent_message_group_id,
            )
            session.add(group)
            session.flush()
            return group

    def get_message_group_by_source_message(
        self, source_message_id: int
    ) -> CommunityMessageGroup | None:
        """Load the message group that owns one source Discord message."""
        with self.session() as session:
            return session.scalar(
                select(CommunityMessageGroup).where(
                    CommunityMessageGroup.source_message_id == source_message_id
                )
            )

    def get_message_group_by_ap_object(
        self, ap_object_id: str
    ) -> CommunityMessageGroup | None:
        """Load the message group that maps to one ActivityPub object ID."""
        # Echo suppression for inbound AP→Discord uses this lookup to detect
        # whether a comment was already processed from this bridge's own publish.
        with self.session() as session:
            return session.scalar(
                select(CommunityMessageGroup).where(
                    CommunityMessageGroup.ap_object_id == ap_object_id
                )
            )

    def get_message_group_by_delivered_message(
        self, discord_message_id: int
    ) -> CommunityMessageGroup | None:
        """Load the message group that has one Discord message as a delivery."""
        # Reply-chain resolution needs to find the parent message group by the
        # Discord message ID so the parent AP object ID can be resolved.
        with self.session() as session:
            delivery = session.scalar(
                select(CommunityMessageGroupDelivery).where(
                    CommunityMessageGroupDelivery.discord_message_id == discord_message_id
                )
            )
            if delivery is None:
                return None
            return session.scalar(
                select(CommunityMessageGroup).where(
                    CommunityMessageGroup.id == delivery.message_group_id
                )
            )

    def add_message_delivery(
        self,
        *,
        message_group_id: int,
        discord_channel_id: int,
        discord_thread_id: int,
        discord_message_id: int,
        role: str,
    ) -> CommunityMessageGroupDelivery:
        """Record one per-channel message delivery for a CommunityMessageGroup."""
        with self.session() as session:
            delivery = CommunityMessageGroupDelivery(
                message_group_id=message_group_id,
                discord_channel_id=discord_channel_id,
                discord_thread_id=discord_thread_id,
                discord_message_id=discord_message_id,
                role=role,
            )
            session.add(delivery)
            session.flush()
            return delivery

    def get_message_deliveries(
        self, message_group_id: int
    ) -> list[CommunityMessageGroupDelivery]:
        """Load all delivery rows for one message group."""
        with self.session() as session:
            return list(
                session.scalars(
                    select(CommunityMessageGroupDelivery).where(
                        CommunityMessageGroupDelivery.message_group_id == message_group_id
                    )
                )
            )

    def get_message_delivery_in_thread(
        self, message_group_id: int, discord_thread_id: int
    ) -> CommunityMessageGroupDelivery | None:
        """Load the delivery row for one message group in one specific thread."""
        # Used by fanout retry logic to check whether a specific thread already
        # received this message before attempting a duplicate delivery.
        with self.session() as session:
            return session.scalar(
                select(CommunityMessageGroupDelivery).where(
                    CommunityMessageGroupDelivery.message_group_id == message_group_id,
                    CommunityMessageGroupDelivery.discord_thread_id == discord_thread_id,
                )
            )
