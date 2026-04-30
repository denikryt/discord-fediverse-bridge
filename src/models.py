from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class PostLink(Base):
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
    __tablename__ = "activitypub_event_receipts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    delivery_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    object_ap_id: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    detail: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
