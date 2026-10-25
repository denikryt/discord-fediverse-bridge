"""Persist canonical ActivityPub objects and verify exact authored source provenance."""

from __future__ import annotations

from .platform_surfaces import PlatformSurfaceRepository, discord_copy_query

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...models import (
    PublishedActivityObject,
    User,
    PlatformConnector,
    ChannelSurface, MessageMapping, LocalSubscriber, LocalCommunityThread,
    LocalCommunityMessage, CommunityThreadGroup, CommunityMessageGroup,
    utcnow,
)
from .base import BaseRepository
from ...platform_types import ChannelRef, MessageRef


@dataclass(frozen=True)
class UserOutboxSlice:
    """Return one internally consistent public outbox response slice."""

    total_items: int
    items: tuple[PublishedActivityObject, ...]


class ActivityPubObjectRepository(BaseRepository):
    """Persist the activitypub objects domain."""

    def create_published_activity_object(
        self,
        *,
        actor_username: str,
        actor_url: str,
        community_actor_url: str,
        activity_id: str,
        object_id: str,
        kind: str,
        title: str | None,
        body_markdown: str,
        in_reply_to_object_id: str | None,
        discord_channel_id: int | None,
        discord_message_id: int | None,
        published_at: datetime | None = None,
    ) -> PublishedActivityObject:
        """Persist one canonical AP object emitted by the gateway publish path."""
        with self.session() as session:
            published_object = PublishedActivityObject(
                **(
                    PlatformSurfaceRepository.discord_source_location(
                        session, discord_channel_id
                    )
                    if discord_channel_id is not None or discord_message_id is not None
                    else {}
                ),
                source_external_message_id=(
                    str(discord_message_id) if discord_message_id is not None else None
                ),
                actor_username=actor_username,
                actor_url=actor_url,
                community_actor_url=community_actor_url,
                activity_id=activity_id,
                object_id=object_id,
                kind=kind,
                title=title,
                body_markdown=body_markdown,
                in_reply_to_object_id=in_reply_to_object_id,
                discord_channel_id=discord_channel_id,
                discord_message_id=discord_message_id,
                published_at=published_at or utcnow(),
            )
            session.add(published_object)
            session.flush()
            return published_object

    def get_published_activity_object_by_object_id(
        self, object_id: str
    ) -> PublishedActivityObject | None:
        """Load one stored gateway-published object by its canonical AP URL."""
        with self.session() as session:
            return session.scalar(
                select(PublishedActivityObject).where(
                    PublishedActivityObject.object_id == object_id
                )
            )

    def get_published_activity_object_by_activity_id(
        self, activity_id: str
    ) -> PublishedActivityObject | None:
        """Load one stored gateway-published object by Create activity URL."""
        with self.session() as session:
            return session.scalar(
                select(PublishedActivityObject).where(
                    PublishedActivityObject.activity_id == activity_id
                )
            )

    def get_by_source_ref(self, source: MessageRef) -> PublishedActivityObject | None:
        """Resolve a canonical published object by the full physical source tuple."""
        if not isinstance(source, MessageRef):
            raise TypeError('typed MessageRef is required')
        with self.session() as session:
            return session.scalar(
                select(PublishedActivityObject)
                .join(PlatformConnector, PublishedActivityObject.source_connector_id == PlatformConnector.id)
                .where(
                    PlatformConnector.connector_key == source.connector_key,
                    PublishedActivityObject.source_external_container_id == source.external_container_id,
                    PublishedActivityObject.source_external_message_id == source.external_message_id,
                )
            )

    def get_published_activity_object_by_discord_message_id(
        self, discord_message_id: int
    ) -> PublishedActivityObject | None:
        """Load one stored gateway-published object by Discord message ID."""
        with self.session() as session:
            rows = list(session.scalars(
                discord_copy_query(PublishedActivityObject, source=True).where(
                    PublishedActivityObject.discord_message_id == discord_message_id
                ).limit(2)
            ))
            if len(rows) > 1 or any(r.source_external_container_id is not None for r in rows):
                raise ValueError('Published source lookup requires exact MessageRef')
            return rows[0] if rows else None

    def load_user_outbox_slice(
        self, *, user: User, offset: int, limit: int
    ) -> UserOutboxSlice:
        """Load count and rows from one transaction-owned SQLite snapshot."""
        if offset < 0 or limit <= 0:
            raise ValueError("offset must be non-negative and limit must be positive")
        # remote subscription repositories removes legacy /users outbox compatibility; retained rows are
        # migrated to the canonical typed actor URL before this query runs.
        predicate = (
            PublishedActivityObject.actor_username == user.activitypub_username,
            PublishedActivityObject.actor_url == user.actor_url,
            PublishedActivityObject.kind.in_(("post", "comment")),
            PublishedActivityObject.deleted_at.is_(None),
        )
        with self.session() as session:
            # The first SELECT establishes the read snapshot. Keeping the same
            # transaction through the page query prevents count/list drift.
            total_items = int(
                session.scalar(
                    select(func.count(PublishedActivityObject.id)).where(*predicate)
                )
                or 0
            )
            items = tuple(
                session.scalars(
                    select(PublishedActivityObject)
                    .where(*predicate)
                    .order_by(
                        PublishedActivityObject.published_at.desc(),
                        PublishedActivityObject.id.desc(),
                    )
                    .offset(offset)
                    .limit(limit)
                )
            )
            return UserOutboxSlice(total_items=total_items, items=items)


# Discord-specific registered-author proof belongs with ActivityPub source
# mappings, not the connector-neutral forum-owner resolver.

@dataclass(frozen=True, slots=True)
class VerifiedCommunityMutationSource:
    """Immutable identity evidence, rechecked when canonical mutation commits."""

    object_id: str
    actor_url: str
    title: str | None
    guild_id: int


def resolve_community_mutation_source(
    session: Session, source: MessageRef, *, external_guild_id: str | None,
    actor_external_id: str | None,
) -> VerifiedCommunityMutationSource | None:
    """Require one current authored Community object for the exact physical ref.

    Never reinterpret an inbound/mirror message as a human-authored source, and
    never use old numeric projections to choose between identical message IDs.
    An absent raw Delete actor is permitted only because the persisted source
    tuple and actor ownership remain independently verifiable here.
    """
    from ...content_sync.surface_resolution import resolve_forum_source

    connector = session.scalar(select(PlatformConnector).where(
        PlatformConnector.connector_key == source.connector_key,
        PlatformConnector.platform == "discord",
    ))
    if connector is None:
        return None
    published = session.scalar(select(PublishedActivityObject).where(
        PublishedActivityObject.source_connector_id == connector.id,
        PublishedActivityObject.source_external_container_id == source.external_container_id,
        PublishedActivityObject.source_external_message_id == source.external_message_id,
        PublishedActivityObject.publication_context == "community",
    ))
    if published is None or published.kind not in ("post", "comment"):
        return None
    mapping = session.scalar(select(MessageMapping).where(
        MessageMapping.source_connector_id == connector.id,
        MessageMapping.source_external_container_id == source.external_container_id,
        MessageMapping.source_id == source.external_message_id,
    ))
    if (mapping is None or mapping.object_id != published.object_id
        or mapping.activity_id != published.activity_id
        or mapping.source_channel_surface_id != published.source_channel_surface_id
        or mapping.actor_url != published.actor_url
        or mapping.community_actor_url != published.community_actor_url):
        return None
    user = session.scalar(select(User).where(User.actor_url == published.actor_url))
    if user is None or (actor_external_id is not None and user.discord_user_id != actor_external_id):
        return None
    if published.source_channel_surface_id is None:
        return None
    surface = session.get(ChannelSurface, published.source_channel_surface_id)
    if surface is None or surface.connector_id != connector.id or surface.kind != "forum":
        return None
    # A legacy numeric forum projection may coexist for Discord, but it must
    # never contradict the owner of the exact typed source tuple.
    if (mapping.discord_channel_id is not None and (
        not surface.external_channel_id.isdecimal()
        or mapping.discord_channel_id != int(surface.external_channel_id)
    )):
        return None
    if (surface.external_guild_id is None
        or not surface.external_guild_id.isdecimal()
        or (external_guild_id is not None and external_guild_id != surface.external_guild_id)):
        return None
    current = resolve_forum_source(session, ChannelRef(
        source.connector_key, surface.external_channel_id,
        kind="forum", external_guild_id=surface.external_guild_id,
    ))
    if (current is None or current.channel_surface_id != surface.id
        or current.community_key != published.community_actor_url):
        return None
    is_post = published.kind == "post"
    # Source rows and the published AP object must identify exactly the same
    # immutable event; the surface role by itself is never author provenance.
    if current.owner_domain == "local":
        if is_post:
            row = session.scalar(select(LocalCommunityThread).where(
                LocalCommunityThread.ap_object_id == published.object_id,
                LocalCommunityThread.source_connector_id == connector.id,
                LocalCommunityThread.source_external_thread_id == source.external_container_id,
                LocalCommunityThread.source_external_starter_message_id == source.external_message_id,
            ))
            row_community_id = row.local_community_id if row else None
        else:
            row = session.scalar(select(LocalCommunityMessage).where(
                LocalCommunityMessage.ap_object_id == published.object_id,
                LocalCommunityMessage.source_connector_id == connector.id,
                LocalCommunityMessage.source_external_container_id == source.external_container_id,
                LocalCommunityMessage.source_external_message_id == source.external_message_id,
            ))
            parent = session.get(LocalCommunityThread, row.local_community_thread_id) if row else None
            row_community_id = parent.local_community_id if parent else None
        if current.owner_role == "local_host":
            expected_community_id = current.owner_id
        else:
            subscriber = session.get(LocalSubscriber, current.owner_id)
            expected_community_id = subscriber.local_community_id if subscriber else None
        if (row is None or row_community_id != expected_community_id
            or row.source_channel_surface_id != surface.id
            or row.direction != 'discord_to_ap'):
            return None
    elif current.owner_domain == "remote":
        if is_post:
            row = session.scalar(select(CommunityThreadGroup).where(
                CommunityThreadGroup.ap_object_id == published.object_id,
                CommunityThreadGroup.community_actor_id == current.community_key,
                CommunityThreadGroup.source_connector_id == connector.id,
                CommunityThreadGroup.source_external_thread_id == source.external_container_id,
            ))
            # Remote canonical post starter remains separate from its thread ID.
            if (row is None or not source.external_message_id.isdecimal()
                or row.source_starter_message_id != int(source.external_message_id)):
                return None
        else:
            row = session.scalar(select(CommunityMessageGroup).where(
                CommunityMessageGroup.ap_object_id == published.object_id,
                CommunityMessageGroup.community_actor_id == current.community_key,
                CommunityMessageGroup.source_connector_id == connector.id,
                CommunityMessageGroup.source_external_container_id == source.external_container_id,
                CommunityMessageGroup.source_external_message_id == source.external_message_id,
            ))
        if row is None or row.source_channel_surface_id != surface.id:
            return None
    else:
        return None
    return VerifiedCommunityMutationSource(
        published.object_id, published.actor_url, published.title,
        int(surface.external_guild_id),
    )
