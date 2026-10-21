from __future__ import annotations


from sqlalchemy import select

from ...models import (
    LocalCommunity,
    LocalCommunityMessage,
    LocalCommunityMessageSurface,
    LocalCommunityThread,
    LocalCommunityThreadSurface,
)
from .base import BaseRepository
from .platform_surfaces import discord_copy_query
from ...models import ChannelSurface, PlatformConnector

"""Canonical local-community thread and message persistence."""


class LocalCommunityContentRepository(BaseRepository):
    """Persist the local community content domain."""

    def __init__(self, session_provider, immediate_session_provider) -> None:
        """Keep ordinary reads but serialize canonical receipt writes at SQLite admission."""
        super().__init__(session_provider)
        self._immediate_session_provider = immediate_session_provider

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

        community surface mapping keeps the public repository entry point small for callers while
        moving the actual Discord ownership into `LocalCommunityThreadSurface`.
        """
        with self._immediate_session_provider() as session:
            # A second handler may have passed the pre-POST lookup. The receipt
            # is deduplicated here under the SQLite writer reservation, not
            # by a speculative check before the physical network operation.
            thread = session.scalar(select(LocalCommunityThread).where(
                LocalCommunityThread.ap_object_id == ap_object_id))
            activity_owner = session.scalar(select(LocalCommunityThread).where(
                LocalCommunityThread.ap_activity_id == ap_activity_id))
            if thread is not None or activity_owner is not None:
                if thread is None or activity_owner is None or thread.id != activity_owner.id:
                    raise ValueError('local community thread AP identity conflict')
                if (thread.local_community_id, thread.ap_activity_id, thread.ap_object_id,
                    thread.direction, thread.origin_kind) != (
                    local_community_id, ap_activity_id, ap_object_id, direction, origin_kind):
                    raise ValueError('local community thread immutable identity conflict')
            else:
                thread = LocalCommunityThread(
                    local_community_id=local_community_id,
                    ap_activity_id=ap_activity_id,
                    ap_object_id=ap_object_id,
                    direction=direction,
                    origin_kind=origin_kind,
                )
                session.add(thread)
                session.flush()
            # community surface mapping only creates host surfaces. later work can add more
            # surfaces for the same canonical thread without rewriting callers.
            local_community = session.get(LocalCommunity, local_community_id)
            if local_community is None:
                raise RuntimeError(
                    f"Missing LocalCommunity {local_community_id} while creating host thread surface"
                )
            placement = session.scalar(
                select(ChannelSurface)
                .join(PlatformConnector)
                .where(
                    ChannelSurface.local_community_id == local_community_id,
                    PlatformConnector.connector_key == "discord-primary",
                    PlatformConnector.platform == "discord",
                )
            )
            if placement is None:
                raise ValueError("Local community has no current Discord host")
            prior = session.scalar(select(LocalCommunityThreadSurface).where(
                LocalCommunityThreadSurface.local_community_thread_id == thread.id,
                LocalCommunityThreadSurface.role == 'host'))
            if prior is not None:
                if (prior.connector_id, prior.channel_surface_id,
                    prior.discord_forum_channel_id, prior.external_thread_id,
                    prior.external_starter_message_id, prior.discord_thread_id,
                    prior.discord_starter_message_id, prior.role) != (
                    placement.connector_id, placement.id, int(placement.external_channel_id),
                    str(discord_thread_id), str(discord_starter_message_id),
                    discord_thread_id, discord_starter_message_id, 'host'):
                    raise ValueError('local community host thread receipt conflict')
                return thread
            # A canonical pre-existing row without its host surface is not a
            # verified receipt; adopting a new Discord ID would fabricate one.
            if activity_owner is not None:
                raise ValueError('existing thread lacks a verified host receipt')
            session.add(
                LocalCommunityThreadSurface(
                    connector_id=placement.connector_id,
                    channel_surface_id=placement.id,
                    external_thread_id=str(discord_thread_id),
                    external_starter_message_id=str(discord_starter_message_id),
                    local_community_thread_id=thread.id,
                    discord_forum_channel_id=int(placement.external_channel_id),
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

        local subscriber publishing local-subscriber source events create the source surface first
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
        with self._immediate_session_provider() as session:
            # Canonical comment identity and host receipt must be checked in
            # the same reserved writer transaction after SDK singleflight.
            message = session.scalar(select(LocalCommunityMessage).where(
                LocalCommunityMessage.ap_object_id == ap_object_id))
            activity_owner = session.scalar(select(LocalCommunityMessage).where(
                LocalCommunityMessage.ap_activity_id == ap_activity_id))
            if message is not None or activity_owner is not None:
                if message is None or activity_owner is None or message.id != activity_owner.id:
                    raise ValueError('local community message AP identity conflict')
                if (message.local_community_thread_id, message.ap_activity_id,
                    message.ap_object_id, message.parent_ap_object_id, message.direction) != (
                    local_community_thread_id, ap_activity_id, ap_object_id,
                    parent_ap_object_id, direction):
                    raise ValueError('local community message immutable identity conflict')
            else:
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
            prior = session.scalar(select(LocalCommunityMessageSurface).where(
                LocalCommunityMessageSurface.local_community_message_id == message.id,
                LocalCommunityMessageSurface.local_community_thread_surface_id == thread_surface.id))
            if prior is not None:
                if (prior.connector_id, prior.channel_surface_id,
                    prior.external_message_id, prior.external_parent_message_id,
                    prior.discord_message_id, prior.parent_discord_message_id,
                    prior.role) != (
                    thread_surface.connector_id, thread_surface.channel_surface_id,
                    str(discord_message_id),
                    str(parent_discord_message_id) if parent_discord_message_id is not None else None,
                    discord_message_id, parent_discord_message_id, 'host'):
                    raise ValueError('local community host message receipt conflict')
                return message
            if activity_owner is not None:
                raise ValueError('existing message lacks a verified host receipt')
            session.add(
                LocalCommunityMessageSurface(
                    connector_id=thread_surface.connector_id,
                    channel_surface_id=thread_surface.channel_surface_id,
                    external_message_id=str(discord_message_id),
                    external_parent_message_id=(
                        str(parent_discord_message_id)
                        if parent_discord_message_id is not None
                        else None
                    ),
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
                    select(LocalCommunityMessage)
                    .where(
                        LocalCommunityMessage.local_community_thread_id
                        == local_community_thread_id
                    )
                    .order_by(
                        LocalCommunityMessage.created_at, LocalCommunityMessage.id
                    )
                )
            )

    def get_local_community_thread_by_id(
        self, local_community_thread_id: int
    ) -> LocalCommunityThread | None:
        """Load one local-community thread row by its primary key."""
        with self.session() as session:
            return session.get(LocalCommunityThread, local_community_thread_id)
