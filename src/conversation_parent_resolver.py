"""Resolve Personal Feed reply ancestry against durable Discord placements.

A local Discord reply has exactly one current Personal Feed thread.  Canonical
ActivityPub identities must not be inferred from bare Discord snowflakes: both
remote and locally authored parents are verified against that thread's root.
"""

from __future__ import annotations

from sqlalchemy import select

from .models import (
    InboundConversationObject,
    InboundDiscordDelivery,
    MessageMapping,
    PublishedActivityObject,
)


class ConversationParentResolver:
    """Map reply parents between AP and Discord within one Personal Feed root."""

    def __init__(self, database: object) -> None:
        """Use the authoritative persisted root/placement rows, not in-memory caches."""
        self.database = database

    def resolve_local_reply(
        self, *, root_object_id: str, thread_id: int, referenced_message_id: int | None
    ) -> str:
        """Resolve an author's native Discord reply or default to its own root.

        An unknown, cross-root or cross-thread snowflake never grants access to
        a different conversation's AP object.  A verified incoming placement
        and a verified original local comment are both legitimate parents.
        """
        if referenced_message_id is None:
            return root_object_id
        with self.database.session() as session:
            root = self._personal_root(session, root_object_id, thread_id)
            if root is None:
                return root_object_id
            if referenced_message_id == root.discord_message_id:
                return root_object_id

            # Existing local source messages have one MessageMapping, but their
            # channel ID is the feed channel rather than the Discord thread.
            local = session.scalar(
                select(PublishedActivityObject)
                .join(MessageMapping, MessageMapping.object_id == PublishedActivityObject.object_id)
                .where(
                    MessageMapping.discord_message_id == referenced_message_id,
                    MessageMapping.publication_context == "personal_feed",
                    MessageMapping.discord_channel_id == root.discord_channel_id,
                    MessageMapping.discord_guild_id == root.discord_guild_id,
                    PublishedActivityObject.kind == "comment",
                    PublishedActivityObject.publication_context == "personal_feed",
                    PublishedActivityObject.conversation_root_object_id == root_object_id,
                )
            )
            if local is not None:
                return local.object_id

            # Federated comments are deliberately *not* in message_mappings;
            # the receipt-to-Discord identity lives in inbound deliveries.
            remote = session.scalar(
                select(InboundConversationObject)
                .join(InboundDiscordDelivery,
                      InboundDiscordDelivery.inbound_object_id == InboundConversationObject.id)
                .where(
                    InboundDiscordDelivery.discord_message_id == referenced_message_id,
                    InboundDiscordDelivery.root_published_object_id == root.id,
                    InboundDiscordDelivery.destination_thread_id == thread_id,
                    InboundDiscordDelivery.state == "delivered",
                    InboundConversationObject.publication_context == "personal_feed",
                    InboundConversationObject.conversation_root_object_id == root_object_id,
                )
            )
            return remote.object_id if remote is not None else root_object_id

    def resolve_remote_reply(
        self, *, root_object_id: str, thread_id: int, parent_object_id: str | None
    ) -> int | None:
        """Find an immediate Discord parent in this thread for a remote child.

        A direct child of the root uses the existing thread's normal message
        send.  Other children must wait for a delivered parent, or Discord's
        native reply would be silently flattened into a root-level comment.
        """
        if parent_object_id is None or parent_object_id == root_object_id:
            return None
        with self.database.session() as session:
            root = self._personal_root(session, root_object_id, thread_id)
            if root is None:
                return None
            remote = session.scalar(
                select(InboundDiscordDelivery.discord_message_id)
                .join(InboundConversationObject,
                      InboundConversationObject.id == InboundDiscordDelivery.inbound_object_id)
                .where(
                    InboundConversationObject.object_id == parent_object_id,
                    InboundConversationObject.publication_context == "personal_feed",
                    InboundConversationObject.conversation_root_object_id == root_object_id,
                    InboundDiscordDelivery.root_published_object_id == root.id,
                    InboundDiscordDelivery.destination_thread_id == thread_id,
                    InboundDiscordDelivery.state == "delivered",
                    InboundDiscordDelivery.discord_message_id.is_not(None),
                )
            )
            if remote is not None:
                return int(remote)

            # A reply can target a locally authored comment.  It references
            # the original Discord message, never a copy in a foreign thread.
            local = session.scalar(
                select(PublishedActivityObject)
                .join(MessageMapping, MessageMapping.object_id == PublishedActivityObject.object_id)
                .where(
                    PublishedActivityObject.object_id == parent_object_id,
                    PublishedActivityObject.kind == "comment",
                    PublishedActivityObject.publication_context == "personal_feed",
                    PublishedActivityObject.conversation_root_object_id == root_object_id,
                    PublishedActivityObject.discord_channel_id == root.discord_channel_id,
                    PublishedActivityObject.discord_guild_id == root.discord_guild_id,
                    MessageMapping.publication_context == "personal_feed",
                    MessageMapping.discord_message_id.is_not(None),
                )
            )
            return int(local.discord_message_id) if local is not None else None

    @staticmethod
    def _personal_root(
        session: object, root_object_id: str, thread_id: int
    ) -> PublishedActivityObject | None:
        """Admit only the deterministic Personal Feed root for this thread."""
        return session.scalar(
            select(PublishedActivityObject).where(
                PublishedActivityObject.object_id == root_object_id,
                PublishedActivityObject.publication_context == "personal_feed",
                PublishedActivityObject.kind == "post",
                PublishedActivityObject.discord_message_id == thread_id,
            )
        )
