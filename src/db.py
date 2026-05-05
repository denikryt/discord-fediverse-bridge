from __future__ import annotations

from contextlib import contextmanager

from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import Session, sessionmaker

from .models import ActivityPubEventReceipt, Base, ChannelCommunitySubscription, CommentLink, PostLink


class Database:
    # Database is a small repository-style wrapper that keeps bridge code away
    # from session management and direct ORM details.
    def __init__(self, url: str) -> None:
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        self.engine = create_engine(url, future=True, connect_args=connect_args)
        self.session_factory = sessionmaker(self.engine, expire_on_commit=False, class_=Session)

    def create_all(self) -> None:
        Base.metadata.create_all(self.engine)
        self._ensure_legacy_schema_compatibility()

    @contextmanager
    def session(self) -> Session:
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
        with self.session() as session:
            return session.scalar(select(PostLink).where(PostLink.discord_forum_thread_id == discord_thread_id))

    def get_post_link_by_lemmy_post_id(self, lemmy_post_id: int) -> PostLink | None:
        with self.session() as session:
            return session.scalar(select(PostLink).where(PostLink.lemmy_post_id == lemmy_post_id))

    def get_post_link_by_lemmy_post_ap_id(self, lemmy_post_ap_id: str) -> PostLink | None:
        with self.session() as session:
            return session.scalar(select(PostLink).where(PostLink.lemmy_post_ap_id == lemmy_post_ap_id))

    def create_post_link(
        self,
        *,
        lemmy_post_id: int,
        lemmy_post_ap_id: str | None,
        discord_forum_thread_id: int,
        discord_starter_message_id: int | None,
        direction: str,
    ) -> PostLink:
        with self.session() as session:
            link = PostLink(
                lemmy_post_id=lemmy_post_id,
                lemmy_post_ap_id=lemmy_post_ap_id,
                discord_forum_thread_id=discord_forum_thread_id,
                discord_starter_message_id=discord_starter_message_id,
                direction=direction,
            )
            session.add(link)
            session.flush()
            return link

    def has_comment_link_for_discord_message(self, discord_message_id: int) -> bool:
        with self.session() as session:
            return session.scalar(select(CommentLink.id).where(CommentLink.discord_message_id == discord_message_id)) is not None

    def has_comment_link_for_lemmy_comment(self, lemmy_comment_id: int) -> bool:
        with self.session() as session:
            return session.scalar(select(CommentLink.id).where(CommentLink.lemmy_comment_id == lemmy_comment_id)) is not None

    def get_comment_link_by_lemmy_comment_ap_id(self, lemmy_comment_ap_id: str) -> CommentLink | None:
        with self.session() as session:
            return session.scalar(select(CommentLink).where(CommentLink.lemmy_comment_ap_id == lemmy_comment_ap_id))

    def get_comment_link_by_discord_message_id(self, discord_message_id: int) -> CommentLink | None:
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
        with self.session() as session:
            return session.scalar(select(ActivityPubEventReceipt).where(ActivityPubEventReceipt.delivery_id == delivery_id))

    def create_event_receipt(self, *, delivery_id: str, event_type: str, object_ap_id: str, status: str, detail: str | None = None) -> ActivityPubEventReceipt:
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
        with self.session() as session:
            receipt = session.scalar(select(ActivityPubEventReceipt).where(ActivityPubEventReceipt.delivery_id == delivery_id))
            if receipt is None:
                raise RuntimeError(f"Missing receipt for delivery {delivery_id}")
            receipt.status = status
            receipt.detail = detail

    def get_subscription_by_channel(self, discord_channel_id: int) -> ChannelCommunitySubscription | None:
        # Each channel maps to at most one community, so this is a point lookup.
        with self.session() as session:
            return session.scalar(select(ChannelCommunitySubscription).where(ChannelCommunitySubscription.discord_channel_id == discord_channel_id))

    def get_subscriptions_by_community(self, lemmy_community_actor_id: str) -> list[ChannelCommunitySubscription]:
        # Used by inbound event handlers to find all Discord channels that should
        # receive posts/comments from a given Lemmy community.
        with self.session() as session:
            return list(session.scalars(select(ChannelCommunitySubscription).where(ChannelCommunitySubscription.lemmy_community_actor_id == lemmy_community_actor_id)))

    def get_all_subscriptions(self) -> list[ChannelCommunitySubscription]:
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
    ) -> ChannelCommunitySubscription:
        # Callers are responsible for checking uniqueness before calling this;
        # the DB UNIQUE constraint on discord_channel_id is the final safety net
        # and will raise IntegrityError if a duplicate is attempted.
        with self.session() as session:
            sub = ChannelCommunitySubscription(
                discord_channel_id=discord_channel_id,
                lemmy_community_actor_id=lemmy_community_actor_id,
                lemmy_community_name=lemmy_community_name,
                lemmy_community_id=lemmy_community_id,
            )
            session.add(sub)
            session.flush()
            return sub

    def delete_subscription(self, discord_channel_id: int) -> bool:
        # Returns False when no subscription exists so callers can give a
        # meaningful response without a separate existence check.
        with self.session() as session:
            sub = session.scalar(select(ChannelCommunitySubscription).where(ChannelCommunitySubscription.discord_channel_id == discord_channel_id))
            if sub is None:
                return False
            session.delete(sub)
            return True

    def _ensure_legacy_schema_compatibility(self) -> None:
        # Existing local SQLite files may predate ActivityPub AP-ID columns, so
        # the bridge upgrades the minimal schema in place on startup.
        inspector = inspect(self.engine)
        post_columns = {column["name"] for column in inspector.get_columns("post_links")}
        comment_columns = {column["name"] for column in inspector.get_columns("comment_links")}

        statements: list[str] = []
        if "lemmy_post_ap_id" not in post_columns:
            statements.append("ALTER TABLE post_links ADD COLUMN lemmy_post_ap_id VARCHAR(512)")
        if "lemmy_comment_ap_id" not in comment_columns:
            statements.append("ALTER TABLE comment_links ADD COLUMN lemmy_comment_ap_id VARCHAR(512)")
        if "lemmy_parent_comment_ap_id" not in comment_columns:
            statements.append("ALTER TABLE comment_links ADD COLUMN lemmy_parent_comment_ap_id VARCHAR(512)")

        index_statements = [
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_post_links_lemmy_post_ap_id ON post_links (lemmy_post_ap_id)",
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_comment_links_lemmy_comment_ap_id ON comment_links (lemmy_comment_ap_id)",
        ]

        with self.engine.begin() as connection:
            for statement in statements:
                connection.execute(text(statement))
            for statement in index_statements:
                connection.execute(text(statement))
