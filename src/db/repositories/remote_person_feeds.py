"""Canonical remote public Notes and per-channel Discord placement lookups."""

from __future__ import annotations

from sqlalchemy import select

from ...models import (
    RemotePersonPost, RemotePersonPostDelivery, RemotePersonCommentPlacement,
)
from .base import BaseRepository


class RemotePersonFeedRepository(BaseRepository):
    """Resolve one ActivityPub root independently of its Discord copies."""

    def get_post(self, object_id: str) -> RemotePersonPost | None:
        """Retrieve one canonical Note by its globally unique ActivityPub ID."""
        with self.session() as session:
            return session.scalar(select(RemotePersonPost).where(
                RemotePersonPost.object_id == object_id
            ))

    def get_root_delivery_by_thread(self, discord_thread_id: int) -> RemotePersonPostDelivery | None:
        """Find a concrete Discord copy for thread-local reply routing."""
        with self.session() as session:
            return session.scalar(select(RemotePersonPostDelivery).where(
                RemotePersonPostDelivery.discord_thread_id == discord_thread_id
            ))

    def list_root_deliveries(self, post_id: int) -> list[RemotePersonPostDelivery]:
        """List all retained Discord copies, including former subscriptions."""
        with self.session() as session:
            return list(session.scalars(select(RemotePersonPostDelivery).where(
                RemotePersonPostDelivery.post_id == post_id
            ).order_by(RemotePersonPostDelivery.id)))

    def get_bot_placement_by_message(self, discord_message_id: int) -> RemotePersonCommentPlacement | None:
        """Resolve only bot-rendered comments, never original user messages."""
        with self.session() as session:
            return session.scalar(select(RemotePersonCommentPlacement).where(
                RemotePersonCommentPlacement.discord_message_id == discord_message_id
            ))
