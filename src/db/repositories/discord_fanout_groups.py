from __future__ import annotations

import json

from .platform_surfaces import PlatformSurfaceRepository, discord_copy_query

from sqlalchemy import select

from ...models import (
    CommunityMessageGroup,
    CommunityMessageGroupDelivery,
    CommunityThreadGroup,
    CommunityThreadGroupDelivery,
    ChannelSurface,
    PlatformConnector,
)
from .base import BaseRepository
from .platform_delivery_intents import cancel_deleted_source_intents, validate_authenticated_update
from ...platform_types import MessageRef, ThreadRef

"""Discord fanout group and delivery persistence."""


class DiscordFanoutGroupRepository(BaseRepository):
    """Persist the discord fanout groups domain."""

    def __init__(self, session_provider, immediate_session_provider) -> None:
        """Reuse regular reads while serializing authenticated canonical Deletes."""
        super().__init__(session_provider)
        self._immediate_session_provider = immediate_session_provider

    def admit_authenticated_update(self, *, kind: str, ap_object_id: str,
                                   community_actor_id: str, actor_id: str,
                                   new_content_json: str, updated_at,
                                   source_activity_id: str | None) -> bool:
        """Persist a verified inbound AP Update independently of physical copies."""
        if kind not in ('post', 'comment'):
            raise ValueError('invalid remote canonical Update kind')
        model = CommunityThreadGroup if kind == 'post' else CommunityMessageGroup
        with self._immediate_session_provider() as session:
            row = session.scalar(select(model).where(model.ap_object_id == ap_object_id))
            if (row is None or row.community_actor_id != community_actor_id
                or row.deleted_at is not None):
                raise ValueError('unknown or deleted remote canonical AP object')
            if kind == 'comment':
                thread = session.get(CommunityThreadGroup, row.thread_group_id)
                if thread is None or thread.deleted_at is not None:
                    raise ValueError('comment Update has deleted remote canonical parent')
            snapshot, order, identity, changed = validate_authenticated_update(
                current_content_json=row.current_content_json,
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
                                    community_actor_id: str, actor_id: str) -> bool:
        """Commit a Remote Community tombstone and all unsent intent cancellations.

        An inbound author's identity comes from the previously authenticated
        canonical AP Create snapshot, not from a naked Delete object URL.
        """
        if kind not in ('post', 'comment'):
            raise ValueError('invalid remote canonical object kind')
        model = CommunityThreadGroup if kind == 'post' else CommunityMessageGroup
        with self._immediate_session_provider() as session:
            row = session.scalar(select(model).where(model.ap_object_id == ap_object_id))
            if row is None or row.community_actor_id != community_actor_id:
                raise ValueError('unknown or foreign remote canonical object')
            try:
                snapshot = json.loads(row.current_content_json or '')
            except (ValueError, TypeError) as exc:
                raise ValueError('missing authenticated remote source provenance') from exc
            if (not isinstance(snapshot, dict) or snapshot.get('authorActorId') != actor_id
                or not actor_id or snapshot.get('apObjectId') != ap_object_id):
                raise ValueError('unauthorized remote canonical Delete')
            if row.deleted_at is not None:
                return False
            from datetime import datetime, timezone
            row.deleted_at = datetime.now(timezone.utc)
            row.content_revision += 1
            cancel_deleted_source_intents(
                session, domain='remote', kind=kind, canonical_id=row.id,
                revision=row.content_revision,
            )
            return True

    @staticmethod
    def _validate_content_snapshot(
        snapshot: str | None, *, kind: str, ap_object_id: str | None
    ) -> str | None:
        """Accept only a bounded canonical snapshot for the same AP object.

        Historical rows may have no reconstructible content, but a newly
        admitted inbound object cannot replace one snapshot with another.
        """
        if snapshot is None:
            return None
        if not isinstance(snapshot, str) or len(snapshot.encode('utf-8')) > 131072:
            raise ValueError('unbounded inbound AP content snapshot')
        try:
            data = json.loads(snapshot)
        except (TypeError, ValueError) as exc:
            raise ValueError('invalid inbound AP content JSON') from exc
        if (not isinstance(data, dict) or data.get('schemaVersion') != 1
            or data.get('kind') != kind or data.get('apObjectId') != ap_object_id
            or not isinstance(data.get('bodyMarkdown'), str)):
            raise ValueError('inbound AP snapshot differs from canonical object')
        return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(',', ':'))

    def create_thread_group(
        self,
        *,
        community_actor_id: str,
        source_channel_id: int | None,
        source_thread_id: int | None,
        source_starter_message_id: int | None,
        ap_activity_id: str | None = None,
        ap_object_id: str | None = None,
        current_content_json: str | None = None,
    ) -> CommunityThreadGroup:
        """Create the canonical thread-group record for one source thread event.

        source_* fields are None for inbound AP events, which create Discord threads
        in all subscribed channels simultaneously without a single source channel.
        """
        snapshot = self._validate_content_snapshot(
            current_content_json, kind='post', ap_object_id=ap_object_id
        )
        with self.session() as session:
            # The authored AP Create transaction may already have admitted
            # the exact canonical group. Reuse only its immutable source and
            # actor identity, never insert a second group on callback replay.
            existing = session.scalar(select(CommunityThreadGroup).where(
                CommunityThreadGroup.ap_object_id == ap_object_id,
            )) if ap_object_id else None
            if existing is not None:
                if ((existing.community_actor_id, existing.ap_activity_id,
                     existing.source_channel_id,existing.source_thread_id,
                     existing.source_starter_message_id)
                    != (community_actor_id,ap_activity_id,source_channel_id,
                        source_thread_id,source_starter_message_id)):
                    raise ValueError('canonical authored thread group identity conflict')
                if snapshot is not None and existing.current_content_json != snapshot:
                    raise ValueError('replayed AP post content snapshot conflict')
                return existing
            group = CommunityThreadGroup(
                **(
                    PlatformSurfaceRepository.discord_source_location(
                        session, source_channel_id
                    )
                    if source_channel_id is not None
                    else {}
                ),
                source_external_thread_id=(
                    str(source_thread_id) if source_thread_id is not None else None
                ),
                community_actor_id=community_actor_id,
                source_channel_id=source_channel_id,
                source_thread_id=source_thread_id,
                source_starter_message_id=source_starter_message_id,
                ap_activity_id=ap_activity_id,
                ap_object_id=ap_object_id,
                current_content_json=snapshot,
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
                discord_copy_query(CommunityThreadGroup, source=True).where(
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

    def get_thread_group_by_id(
        self, thread_group_id: int
    ) -> CommunityThreadGroup | None:
        """Retrieve a thread group by its primary key ID."""
        with self.session() as session:
            return session.get(CommunityThreadGroup, thread_group_id)

    def get_thread_group_by_any_thread(
        self, discord_thread_id: int
    ) -> CommunityThreadGroup | None:
        """Load the thread group for any thread (source, mirror, or inbound).

        Phase 9: Looks up the thread group via CommunityThreadGroupDelivery,
        allowing mirror and inbound threads to resolve their post context.
        Unlike get_thread_group_by_source_thread, this works for any role.
        """
        with self.session() as session:
            delivery = session.scalar(
                discord_copy_query(CommunityThreadGroupDelivery).where(
                    CommunityThreadGroupDelivery.discord_thread_id == discord_thread_id
                )
            )
            if delivery is None:
                return None
            return session.scalar(
                select(CommunityThreadGroup).where(
                    CommunityThreadGroup.id == delivery.thread_group_id
                )
            )

    def get_thread_delivery_by_ref(self, thread: ThreadRef) -> CommunityThreadGroupDelivery | None:
        """Find only the receipt at the exact connector/forum/thread placement."""
        with self.session() as session:
            return session.scalar(
                select(CommunityThreadGroupDelivery)
                .join(ChannelSurface, ChannelSurface.id == CommunityThreadGroupDelivery.channel_surface_id)
                .join(PlatformConnector, PlatformConnector.id == CommunityThreadGroupDelivery.connector_id)
                .where(PlatformConnector.connector_key == thread.connector_key,
                       ChannelSurface.connector_id == CommunityThreadGroupDelivery.connector_id,
                       ChannelSurface.external_channel_id == thread.channel.external_channel_id,
                       ChannelSurface.external_guild_id == thread.channel.external_guild_id,
                       ChannelSurface.kind == 'forum',
                       CommunityThreadGroupDelivery.external_thread_id == thread.external_thread_id)
            )

    def get_thread_group_by_ref(self, thread: ThreadRef) -> CommunityThreadGroup | None:
        """Resolve a canonical remote post only through its scoped physical thread."""
        delivery = self.get_thread_delivery_by_ref(thread)
        return self.get_thread_group_by_id(delivery.thread_group_id) if delivery else None

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
            existing = session.scalar(select(CommunityThreadGroupDelivery).where(
                CommunityThreadGroupDelivery.thread_group_id == thread_group_id,
                CommunityThreadGroupDelivery.discord_channel_id == discord_channel_id,
            ))
            if existing is not None:
                if ((existing.discord_thread_id,existing.discord_starter_message_id,existing.role)
                    != (discord_thread_id,discord_starter_message_id,role)):
                    raise ValueError('canonical thread physical receipt identity conflict')
                return existing
            delivery = CommunityThreadGroupDelivery(
                **PlatformSurfaceRepository.discord_copy_location(
                    session, discord_channel_id
                ),
                external_thread_id=str(discord_thread_id),
                external_starter_message_id=str(discord_starter_message_id),
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
                    discord_copy_query(CommunityThreadGroupDelivery).where(
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
                discord_copy_query(CommunityThreadGroupDelivery).where(
                    CommunityThreadGroupDelivery.discord_thread_id == discord_thread_id
                )
            )

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
        current_content_json: str | None = None,
    ) -> CommunityMessageGroup:
        """Create the canonical message-group record for one source message event.

        source_* fields are None for inbound AP events, which deliver into all
        subscribed threads simultaneously without a single source message.
        """
        snapshot = self._validate_content_snapshot(
            current_content_json, kind='comment', ap_object_id=ap_object_id
        )
        with self.session() as session:
            if ap_object_id is not None and ap_activity_id is not None:
                existing = session.scalar(select(CommunityMessageGroup).where(
                    CommunityMessageGroup.ap_object_id == ap_object_id,
                ))
                if existing is not None:
                    if (existing.ap_activity_id, existing.community_actor_id,
                        existing.thread_group_id, existing.source_channel_id,
                        existing.source_thread_id, existing.source_message_id,
                        existing.parent_message_group_id) != (
                        ap_activity_id, community_actor_id, thread_group_id,
                        source_channel_id, source_thread_id, source_message_id,
                        parent_message_group_id,
                    ):
                        raise ValueError('community comment canonical identity conflict')
                    if snapshot is not None and existing.current_content_json != snapshot:
                        raise ValueError('replayed AP comment content snapshot conflict')
                    return existing
            group = CommunityMessageGroup(
                **(
                    PlatformSurfaceRepository.discord_source_location(
                        session, source_channel_id
                    )
                    if source_channel_id is not None
                    else {}
                ),
                source_external_container_id=(
                    str(source_thread_id) if source_message_id is not None and source_thread_id is not None else None
                ),
                source_external_message_id=(
                    str(source_message_id) if source_message_id is not None else None
                ),
                community_actor_id=community_actor_id,
                thread_group_id=thread_group_id,
                source_channel_id=source_channel_id,
                source_thread_id=source_thread_id,
                source_message_id=source_message_id,
                ap_activity_id=ap_activity_id,
                ap_object_id=ap_object_id,
                parent_message_group_id=parent_message_group_id,
                current_content_json=snapshot,
            )
            session.add(group)
            session.flush()
            return group

    def get_message_group_by_id(
        self, message_group_id: int
    ) -> CommunityMessageGroup | None:
        """Retrieve a message group by its primary key ID."""
        with self.session() as session:
            return session.get(CommunityMessageGroup, message_group_id)

    def get_message_group_by_source_ref(self, source: MessageRef) -> CommunityMessageGroup | None:
        """Resolve only the exact remote-domain authored source comment.

        Mirror receipts and their message IDs are not source-author evidence.
        """
        if not isinstance(source, MessageRef):
            raise TypeError('Source lookup requires typed MessageRef')
        with self.session() as session:
            return session.scalar(
                select(CommunityMessageGroup)
                .join(PlatformConnector, CommunityMessageGroup.source_connector_id == PlatformConnector.id)
                .where(
                    PlatformConnector.connector_key == source.connector_key,
                    CommunityMessageGroup.source_external_container_id == source.external_container_id,
                    CommunityMessageGroup.source_external_message_id == source.external_message_id,
                )
            )

    def get_message_group_by_source_message(
        self, source_message_id: int
    ) -> CommunityMessageGroup | None:
        """Load the message group that owns one source Discord message."""
        with self.session() as session:
            return session.scalar(
                discord_copy_query(CommunityMessageGroup, source=True).where(
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
                discord_copy_query(CommunityMessageGroupDelivery).where(
                    CommunityMessageGroupDelivery.discord_message_id
                    == discord_message_id
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
        parent_discord_message_id: int | None = None,
    ) -> CommunityMessageGroupDelivery:
        """Record one per-channel message delivery for a CommunityMessageGroup."""
        with self.session() as session:
            previous = session.scalar(select(CommunityMessageGroupDelivery).where(
                CommunityMessageGroupDelivery.message_group_id == message_group_id,
                CommunityMessageGroupDelivery.discord_channel_id == discord_channel_id,
            ))
            if previous is not None:
                if (previous.discord_thread_id, previous.discord_message_id,
                    previous.external_parent_message_id, previous.role) != (
                    discord_thread_id, discord_message_id,
                    str(parent_discord_message_id) if parent_discord_message_id is not None else None, role,
                ):
                    raise ValueError('community message physical receipt conflict')
                return previous
            delivery = CommunityMessageGroupDelivery(
                **PlatformSurfaceRepository.discord_copy_location(
                    session, discord_channel_id
                ),
                external_thread_id=str(discord_thread_id),
                external_message_id=str(discord_message_id),
                external_parent_message_id=(
                    str(parent_discord_message_id)
                    if parent_discord_message_id is not None
                    else None
                ),
                message_group_id=message_group_id,
                discord_channel_id=discord_channel_id,
                discord_thread_id=discord_thread_id,
                discord_message_id=discord_message_id,
                role=role,
            )
            session.add(delivery)
            session.flush()
            return delivery

    def get_message_delivery_by_ref(self, message: MessageRef) -> CommunityMessageGroupDelivery | None:
        """Resolve a physical comment by connector, immediate thread and message."""
        with self.session() as session:
            return session.scalar(select(CommunityMessageGroupDelivery)
                .join(PlatformConnector, PlatformConnector.id == CommunityMessageGroupDelivery.connector_id)
                .where(PlatformConnector.connector_key == message.connector_key,
                       CommunityMessageGroupDelivery.external_thread_id == message.external_container_id,
                       CommunityMessageGroupDelivery.external_message_id == message.external_message_id))

    def get_message_delivery_in_thread_ref(self, message_group_id: int,
                                           thread: ThreadRef) -> CommunityMessageGroupDelivery | None:
        """Find the parent's copy in the exact target thread, never by bare ID."""
        target = self.get_thread_delivery_by_ref(thread)
        if target is None:
            return None
        with self.session() as session:
            return session.scalar(select(CommunityMessageGroupDelivery).where(
                CommunityMessageGroupDelivery.message_group_id == message_group_id,
                CommunityMessageGroupDelivery.connector_id == target.connector_id,
                CommunityMessageGroupDelivery.channel_surface_id == target.channel_surface_id,
                CommunityMessageGroupDelivery.external_thread_id == thread.external_thread_id,
            ))

    def get_message_delivery_by_message(
        self, discord_message_id: int
    ) -> CommunityMessageGroupDelivery | None:
        """Load the delivery row for one Discord message ID.

        Used by on_raw_message_edit and on_raw_message_delete to check the
        delivery role before deciding whether to propagate the event to AP.
        Returns None if the message is not part of any tracked delivery.
        """
        with self.session() as session:
            return session.scalar(
                discord_copy_query(CommunityMessageGroupDelivery).where(
                    CommunityMessageGroupDelivery.discord_message_id
                    == discord_message_id
                )
            )

    def get_message_deliveries(
        self, message_group_id: int
    ) -> list[CommunityMessageGroupDelivery]:
        """Load all delivery rows for one message group."""
        with self.session() as session:
            return list(
                session.scalars(
                    discord_copy_query(CommunityMessageGroupDelivery).where(
                        CommunityMessageGroupDelivery.message_group_id
                        == message_group_id
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
                discord_copy_query(CommunityMessageGroupDelivery).where(
                    CommunityMessageGroupDelivery.message_group_id == message_group_id,
                    CommunityMessageGroupDelivery.discord_thread_id
                    == discord_thread_id,
                )
            )
