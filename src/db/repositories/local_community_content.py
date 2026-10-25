from __future__ import annotations


from sqlalchemy import select
import json

from ...models import (
    PlatformConnector,
    LocalCommunity,
    LocalCommunityMessage,
    LocalCommunityMessageSurface,
    LocalCommunityThread,
    LocalCommunityThreadSurface,
)
from .base import BaseRepository
from .legacy_mutation_provenance import LegacyMutationProvenanceRequired, source_identity
from .platform_delivery_intents import cancel_deleted_source_intents, validate_authenticated_update
from ...platform_types import MessageRef
from .platform_surfaces import discord_copy_query
from ...models import ChannelSurface, PlatformConnector

"""Canonical local-community thread and message persistence."""


class LocalCommunityContentRepository(BaseRepository):
    """Persist the local community content domain."""

    def __init__(self, session_provider, immediate_session_provider) -> None:
        """Keep ordinary reads but serialize canonical receipt writes at SQLite admission."""
        super().__init__(session_provider)
        self._immediate_session_provider = immediate_session_provider

    @staticmethod
    def _verified_inbound_snapshot(snapshot: str, *, kind: str, object_id: str) -> str:
        """Reject incomplete or conflicting canonical content before physical I/O."""
        if not isinstance(snapshot, str) or len(snapshot.encode('utf-8')) > 131072:
            raise ValueError('invalid bounded inbound content snapshot')
        try:
            data = json.loads(snapshot)
        except (TypeError, ValueError) as exc:
            raise ValueError('invalid inbound canonical JSON') from exc
        if (not isinstance(data, dict) or data.get('schemaVersion') != 1
            or data.get('kind') != kind or data.get('apObjectId') != object_id
            or not isinstance(data.get('bodyMarkdown'), str)
            or not isinstance(data.get('authorActorId'), str)
            or not data.get('authorActorId')
            or not isinstance(data.get('authorName'), str)
            or not data.get('authorName')):
            raise ValueError('inbound canonical snapshot does not match object')
        return json.dumps(data, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                          allow_nan=False)

    def admit_authenticated_update(self, *, kind: str, ap_object_id: str,
                                   local_community_id: int, actor_id: str,
                                   new_content_json: str, updated_at,
                                   source_activity_id: str | None) -> bool:
        """Commit the latest author-verified AP content with no recipient count gate."""
        if kind not in ('post', 'comment'):
            raise ValueError('invalid local canonical Update kind')
        model = LocalCommunityThread if kind == 'post' else LocalCommunityMessage
        with self._immediate_session_provider() as session:
            row = session.scalar(select(model).where(model.ap_object_id == ap_object_id))
            if row is None or row.deleted_at is not None:
                raise ValueError('missing or deleted local canonical AP object')
            thread = row if kind == 'post' else session.get(LocalCommunityThread, row.local_community_thread_id)
            community = session.get(LocalCommunity, local_community_id)
            if (thread is None or thread.deleted_at is not None
                or thread.local_community_id != local_community_id or community is None):
                raise ValueError('local Update has wrong or deleted owning thread')
            snapshot, order, identity, changed = validate_authenticated_update(
                current_content_json=source_identity(
                    session, row, domain='local', kind=kind,
                    community_actor_id=community.actor_url,
                    local_community_id=local_community_id),
                new_content_json=new_content_json, ap_object_id=ap_object_id,
                actor_id=actor_id, updated_at=updated_at, source_activity_id=source_activity_id,
                previous_order_key=row.last_source_update_order_key,
                previous_identity=row.last_source_update_identity,
            )
            if changed:
                row.current_content_json = snapshot
                row.content_revision += 1
                row.last_source_update_order_key = order
                row.last_source_update_identity = identity
            return changed

    def mark_inbound_deleted_if_live(self, *, kind: str, ap_object_id: str,
                                    local_community_id: int, actor_id: str) -> bool:
        """Tombstone one authenticated inbound object before physical effects.

        A matching persisted canonical actor is required: neither a guessed
        source row nor an unauthenticated AP ID grants deletion permission.
        Returns False on a verified duplicate Delete without new revision.
        """
        if kind not in ('post', 'comment'):
            raise ValueError('invalid local canonical object kind')
        model = LocalCommunityThread if kind == 'post' else LocalCommunityMessage
        with self._immediate_session_provider() as session:
            row = session.scalar(select(model).where(model.ap_object_id == ap_object_id))
            if row is None:
                raise ValueError('unknown local canonical object')
            thread = row if kind == 'post' else session.get(LocalCommunityThread, row.local_community_thread_id)
            community = session.get(LocalCommunity, local_community_id)
            if (thread is None or thread.local_community_id != local_community_id
                or community is None):
                raise ValueError('local canonical source community mismatch')
            try:
                snapshot = json.loads(source_identity(
                    session, row, domain='local', kind=kind,
                    community_actor_id=community.actor_url,
                    local_community_id=local_community_id))
            except LegacyMutationProvenanceRequired:
                raise
            except (ValueError, TypeError) as exc:
                raise ValueError('missing authenticated local source provenance') from exc
            if (not isinstance(snapshot, dict) or snapshot.get('authorActorId') != actor_id
                or not actor_id or snapshot.get('apObjectId') != ap_object_id):
                raise ValueError('unauthorized local canonical Delete')
            if row.deleted_at is not None:
                return False
            from datetime import datetime, timezone
            row.deleted_at = datetime.now(timezone.utc)
            row.content_revision += 1
            cancel_deleted_source_intents(
                session, domain='local', kind=kind, canonical_id=row.id,
                revision=row.content_revision,
            )
            return True

    def admit_inbound_post(self, *, local_community_id: int, ap_activity_id: str,
                           ap_object_id: str, current_content_json: str) -> LocalCommunityThread:
        """Commit an authenticated AP post *before* any Discord Create.

        The existing canonical LocalCommunityThread is the sole durable AP
        identity; a second handler may reuse it only with identical origin,
        community, activity and normalized payload. No host receipt is invented.
        """
        snapshot = self._verified_inbound_snapshot(
            current_content_json, kind='post', object_id=ap_object_id)
        with self._immediate_session_provider() as session:
            community = session.get(LocalCommunity, local_community_id)
            if community is None or community.status != 'active':
                raise ValueError('inactive or unknown local community')
            row = session.scalar(select(LocalCommunityThread).where(
                LocalCommunityThread.ap_object_id == ap_object_id))
            owner = session.scalar(select(LocalCommunityThread).where(
                LocalCommunityThread.ap_activity_id == ap_activity_id))
            if row is not None or owner is not None:
                if (row is None or owner is None or row.id != owner.id
                    or row.local_community_id != local_community_id
                    or row.direction != 'ap_to_discord'
                    or row.origin_kind != 'remote_follower'
                    or row.deleted_at is not None
                    or row.current_content_json != snapshot):
                    raise ValueError('conflicting inbound AP post admission')
                return row
            row = LocalCommunityThread(
                local_community_id=local_community_id, ap_activity_id=ap_activity_id,
                ap_object_id=ap_object_id, direction='ap_to_discord',
                origin_kind='remote_follower', current_content_json=snapshot)
            session.add(row)
            session.flush()
            return row

    def admit_inbound_comment(self, *, local_community_thread_id: int,
                              ap_activity_id: str, ap_object_id: str,
                              parent_ap_object_id: str | None,
                              current_content_json: str) -> LocalCommunityMessage:
        """Commit one canonical inbound reply with same-thread AP parent proof."""
        snapshot = self._verified_inbound_snapshot(
            current_content_json, kind='comment', object_id=ap_object_id)
        with self._immediate_session_provider() as session:
            thread = session.get(LocalCommunityThread, local_community_thread_id)
            if thread is None or thread.deleted_at is not None:
                raise ValueError('missing or deleted canonical reply thread')
            community = session.get(LocalCommunity, thread.local_community_id)
            if community is None or community.status != 'active':
                raise ValueError('inactive local community')
            if parent_ap_object_id and parent_ap_object_id != thread.ap_object_id:
                parent = session.scalar(select(LocalCommunityMessage).where(
                    LocalCommunityMessage.ap_object_id == parent_ap_object_id,
                    LocalCommunityMessage.local_community_thread_id == thread.id))
                if parent is None or parent.deleted_at is not None:
                    raise ValueError('nested AP parent is absent or foreign')
            row = session.scalar(select(LocalCommunityMessage).where(
                LocalCommunityMessage.ap_object_id == ap_object_id))
            owner = session.scalar(select(LocalCommunityMessage).where(
                LocalCommunityMessage.ap_activity_id == ap_activity_id))
            if row is not None or owner is not None:
                if (row is None or owner is None or row.id != owner.id
                    or row.local_community_thread_id != thread.id
                    or row.direction != 'ap_to_discord'
                    or row.parent_ap_object_id != parent_ap_object_id
                    or row.deleted_at is not None
                    or row.current_content_json != snapshot):
                    raise ValueError('conflicting inbound AP comment admission')
                return row
            row = LocalCommunityMessage(
                local_community_thread_id=thread.id, ap_activity_id=ap_activity_id,
                ap_object_id=ap_object_id, parent_ap_object_id=parent_ap_object_id,
                direction='ap_to_discord', current_content_json=snapshot)
            session.add(row)
            session.flush()
            return row

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
            existing = session.scalar(select(LocalCommunityThread).where(
                LocalCommunityThread.ap_object_id == ap_object_id,
            ))
            if existing is not None:
                if (existing.local_community_id,existing.ap_activity_id,
                    existing.direction,existing.origin_kind) != (
                    local_community_id,ap_activity_id,direction,origin_kind,
                ):
                    raise ValueError('authored canonical local thread identity conflict')
                return existing
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
            existing = session.scalar(select(LocalCommunityMessage).where(
                LocalCommunityMessage.ap_object_id == ap_object_id,
            ))
            if existing is not None:
                if (existing.local_community_thread_id, existing.ap_activity_id,
                    existing.parent_ap_object_id, existing.direction) != (
                    local_community_thread_id, ap_activity_id,
                    parent_ap_object_id, direction,
                ):
                    raise ValueError('local community comment canonical identity conflict')
                return existing
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

    def get_local_message_by_source_ref(self, source: MessageRef) -> LocalCommunityMessage | None:
        """Find only the source-authored comment in its immediate container.

        A historical mirror with the same message ID is not a source and
        changing a channel owner cannot reassign an immutable canonical row.
        """
        if not isinstance(source, MessageRef):
            raise TypeError('Source lookup requires typed MessageRef')
        with self.session() as session:
            return session.scalar(
                select(LocalCommunityMessage)
                .join(PlatformConnector, LocalCommunityMessage.source_connector_id == PlatformConnector.id)
                .where(
                    PlatformConnector.connector_key == source.connector_key,
                    LocalCommunityMessage.source_external_container_id == source.external_container_id,
                    LocalCommunityMessage.source_external_message_id == source.external_message_id,
                )
            )

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
