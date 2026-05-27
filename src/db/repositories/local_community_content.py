from __future__ import annotations

from datetime import datetime

from sqlalchemy import select

from ...models import (
    LocalCommunity,
    LocalSubscriber,
    LocalCommunityMessage,
    LocalCommunityMessageSurface,
    LocalCommunityRelayDelivery,
    LocalCommunityRelaySourceActivity,
    LocalCommunityThread,
    LocalCommunityThreadSurface,
    RemoteSubscriber,
    utcnow,
)
from .base import BaseRepository


"""Canonical local-community thread and message persistence."""


class LocalCommunityContentRepository(BaseRepository):
    """Persist the local community content domain."""

    def create_local_community_thread(
            self,
            *,
            local_community_id: int,
            discord_thread_id: int,
            discord_starter_message_id: int,
            ap_activity_id: str,
            ap_object_id: str,
            direction: str,
            origin_kind: str,
        ) -> LocalCommunityThread:
            """Persist one canonical thread row plus its host Discord surface.

            Stage 2 keeps the public repository entry point small for callers while
            moving the actual Discord ownership into `LocalCommunityThreadSurface`.
            """
            with self.session() as session:
                thread = LocalCommunityThread(
                    local_community_id=local_community_id,
                    ap_activity_id=ap_activity_id,
                    ap_object_id=ap_object_id,
                    direction=direction,
                    origin_kind=origin_kind,
                )
                session.add(thread)
                session.flush()
                # Stage 2 only creates host surfaces. Later stages can add more
                # surfaces for the same canonical thread without rewriting callers.
                local_community = session.get(LocalCommunity, local_community_id)
                if local_community is None:
                    raise RuntimeError(
                        f"Missing LocalCommunity {local_community_id} while creating host thread surface"
                    )
                session.add(
                    LocalCommunityThreadSurface(
                        local_community_thread_id=thread.id,
                        discord_forum_channel_id=local_community.discord_forum_channel_id,
                        discord_thread_id=discord_thread_id,
                        discord_starter_message_id=discord_starter_message_id,
                        role="host",
                        local_subscriber_id=None,
                    )
                )
                session.flush()
                return thread

    def create_local_community_thread_canonical(
            self,
            *,
            local_community_id: int,
            ap_activity_id: str,
            ap_object_id: str,
            direction: str,
            origin_kind: str,
        ) -> LocalCommunityThread:
            """Persist one canonical local-community thread without a host surface.

            Stage 4 local-subscriber source events create the source surface first
            and then fan out to host/sibling targets.  This helper keeps that path
            from incorrectly storing the source Discord thread as the host surface.
            """
            with self.session() as session:
                thread = LocalCommunityThread(
                    local_community_id=local_community_id,
                    ap_activity_id=ap_activity_id,
                    ap_object_id=ap_object_id,
                    direction=direction,
                    origin_kind=origin_kind,
                )
                session.add(thread)
                session.flush()
                return thread

    def get_local_community_thread_by_ap_object_id(
            self, ap_object_id: str
        ) -> LocalCommunityThread | None:
            """Load the local-community thread row for one AP post object ID."""
            with self.session() as session:
                return session.scalar(
                    select(LocalCommunityThread).where(
                        LocalCommunityThread.ap_object_id == ap_object_id
                    )
                )

    def create_local_community_message(
            self,
            *,
            local_community_thread_id: int,
            discord_message_id: int,
            ap_activity_id: str,
            ap_object_id: str,
            parent_ap_object_id: str | None,
            parent_discord_message_id: int | None,
            direction: str,
        ) -> LocalCommunityMessage:
            """Persist one canonical message row plus its host Discord surface."""
            with self.session() as session:
                message = LocalCommunityMessage(
                    local_community_thread_id=local_community_thread_id,
                    ap_activity_id=ap_activity_id,
                    ap_object_id=ap_object_id,
                    parent_ap_object_id=parent_ap_object_id,
                    direction=direction,
                )
                session.add(message)
                session.flush()
                thread_surface = session.scalar(
                    select(LocalCommunityThreadSurface).where(
                        LocalCommunityThreadSurface.local_community_thread_id
                        == local_community_thread_id,
                        LocalCommunityThreadSurface.role == "host",
                    )
                )
                if thread_surface is None:
                    raise RuntimeError(
                        f"Missing host thread surface for local community thread {local_community_thread_id}"
                    )
                session.add(
                    LocalCommunityMessageSurface(
                        local_community_message_id=message.id,
                        local_community_thread_surface_id=thread_surface.id,
                        discord_forum_channel_id=thread_surface.discord_forum_channel_id,
                        discord_message_id=discord_message_id,
                        parent_discord_message_id=parent_discord_message_id,
                        role="host",
                        local_subscriber_id=None,
                    )
                )
                session.flush()
                return message

    def create_local_community_message_canonical(
            self,
            *,
            local_community_thread_id: int,
            ap_activity_id: str,
            ap_object_id: str,
            parent_ap_object_id: str | None,
            direction: str,
        ) -> LocalCommunityMessage:
            """Persist one canonical local-community comment without a host surface.

            Local-subscriber source comments must first record the source message
            surface, then copy into host and sibling surfaces.  Creating a host
            surface here would bind the source Discord message to the wrong forum.
            """
            with self.session() as session:
                message = LocalCommunityMessage(
                    local_community_thread_id=local_community_thread_id,
                    ap_activity_id=ap_activity_id,
                    ap_object_id=ap_object_id,
                    parent_ap_object_id=parent_ap_object_id,
                    direction=direction,
                )
                session.add(message)
                session.flush()
                return message

    def get_local_community_message_by_ap_object_id(
            self, ap_object_id: str
        ) -> LocalCommunityMessage | None:
            """Load the local-community message row for one AP comment object ID."""
            with self.session() as session:
                return session.scalar(
                    select(LocalCommunityMessage).where(
                        LocalCommunityMessage.ap_object_id == ap_object_id
                    )
                )

    def list_local_community_messages_for_thread(
            self, local_community_thread_id: int
        ) -> list[LocalCommunityMessage]:
            """Load all mapped messages for one local-community thread."""
            with self.session() as session:
                return list(
                    session.scalars(
                        select(LocalCommunityMessage).where(
                            LocalCommunityMessage.local_community_thread_id == local_community_thread_id
                        ).order_by(LocalCommunityMessage.created_at, LocalCommunityMessage.id)
                    )
                )

    def get_local_community_thread_by_id(self, local_community_thread_id: int) -> LocalCommunityThread | None:
            """Load one local-community thread row by its primary key."""
            with self.session() as session:
                return session.get(LocalCommunityThread, local_community_thread_id)
