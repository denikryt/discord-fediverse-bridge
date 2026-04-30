from __future__ import annotations

from contextlib import contextmanager

from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import Session, sessionmaker

from .models import ActivityPubEventReceipt, Base, CommentLink, PostLink


class Database:
    def __init__(self, url: str) -> None:
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        self.engine = create_engine(url, future=True, connect_args=connect_args)
        self.session_factory = sessionmaker(self.engine, expire_on_commit=False, class_=Session)

    def create_all(self) -> None:
        Base.metadata.create_all(self.engine)
        self._ensure_legacy_schema_compatibility()

    @contextmanager
    def session(self) -> Session:
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

    def _ensure_legacy_schema_compatibility(self) -> None:
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
