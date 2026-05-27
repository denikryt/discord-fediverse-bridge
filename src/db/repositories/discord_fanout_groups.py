from __future__ import annotations

from sqlalchemy import select

from ...models import (
    CommentLink,
    CommunityMessageGroup,
    CommunityMessageGroupDelivery,
    CommunityThreadGroup,
    CommunityThreadGroupDelivery,
    PostLink,
)
from .base import BaseRepository


"""Discord fanout group and delivery persistence."""


class DiscordFanoutGroupRepository(BaseRepository):
    """Persist the discord fanout groups domain."""

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
                    select(CommunityThreadGroupDelivery).where(
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

    def get_message_group_by_id(
            self, message_group_id: int
        ) -> CommunityMessageGroup | None:
            """Retrieve a message group by its primary key ID."""
            with self.session() as session:
                return session.get(CommunityMessageGroup, message_group_id)

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
                    select(CommunityMessageGroupDelivery).where(
                        CommunityMessageGroupDelivery.discord_message_id == discord_message_id
                    )
                )

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
