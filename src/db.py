from __future__ import annotations

from contextlib import contextmanager

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from .models import Base, CommentLink, PostLink, SyncState


class Database:
    def __init__(self, url: str) -> None:
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        self.engine = create_engine(url, future=True, connect_args=connect_args)
        self.session_factory = sessionmaker(self.engine, expire_on_commit=False, class_=Session)

    def create_all(self) -> None:
        Base.metadata.create_all(self.engine)

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

    def create_post_link(
        self,
        *,
        lemmy_post_id: int,
        discord_forum_thread_id: int,
        discord_starter_message_id: int | None,
        direction: str,
    ) -> PostLink:
        with self.session() as session:
            link = PostLink(
                lemmy_post_id=lemmy_post_id,
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

    def create_comment_link(
        self,
        *,
        lemmy_comment_id: int,
        lemmy_post_id: int,
        discord_forum_thread_id: int,
        discord_message_id: int,
        direction: str,
    ) -> CommentLink:
        with self.session() as session:
            link = CommentLink(
                lemmy_comment_id=lemmy_comment_id,
                lemmy_post_id=lemmy_post_id,
                discord_forum_thread_id=discord_forum_thread_id,
                discord_message_id=discord_message_id,
                direction=direction,
            )
            session.add(link)
            session.flush()
            return link

    def get_sync_state(self, key: str) -> str | None:
        with self.session() as session:
            state = session.scalar(select(SyncState).where(SyncState.key == key))
            return state.value if state else None

    def set_sync_state(self, key: str, value: str) -> None:
        with self.session() as session:
            state = session.scalar(select(SyncState).where(SyncState.key == key))
            if state is None:
                session.add(SyncState(key=key, value=value))
            else:
                state.value = value
