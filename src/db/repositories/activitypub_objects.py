from __future__ import annotations

from sqlalchemy import select

from ...models import (
    MessageMapping,
    PublishedActivityObject,
    RemoteActor,
    utcnow,
)
from .base import BaseRepository


"""Persisted ActivityPub object JSON lookup persistence."""


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

    def get_published_activity_object_by_discord_message_id(
            self, discord_message_id: int
        ) -> PublishedActivityObject | None:
            """Load one stored gateway-published object by Discord message ID."""
            with self.session() as session:
                return session.scalar(
                    select(PublishedActivityObject).where(
                        PublishedActivityObject.discord_message_id == discord_message_id
                    )
                )
