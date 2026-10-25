from __future__ import annotations

from .platform_surfaces import PlatformSurfaceRepository, discord_copy_query

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select

from ...models import (
    PublishedActivityObject,
    User,
    PlatformConnector,
    utcnow,
)
from .base import BaseRepository
from ...platform_types import MessageRef

"""Persisted ActivityPub object JSON lookup persistence."""


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
