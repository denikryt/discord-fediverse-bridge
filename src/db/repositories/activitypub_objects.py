from __future__ import annotations

from dataclasses import dataclass
import json
from datetime import datetime

from sqlalchemy import func, select

from ...models import (
    PublishedActivityObject,
    UserActorOutboundSource,
    User,
    utcnow,
)
from .base import BaseRepository


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

    def get_original_create_object_shape(
        self, published_object: PublishedActivityObject,
    ) -> tuple[str | None, str | None]:
        """Recover public type/context from the immutable persisted Create.

        Do not guess remote software from a path, title or current Follow: the
        original serialized Create is the evidence for public dereference and
        outbox reconstruction. Older published rows without an outbox source
        retain their existing Page/Note rendering contract.
        """
        with self.session() as session:
            source = session.scalar(select(UserActorOutboundSource).where(
                UserActorOutboundSource.published_object_id == published_object.id,
                UserActorOutboundSource.source_kind == "content_create",
                UserActorOutboundSource.activity_id == published_object.activity_id,
            ))
            if source is None:
                return None, None
            if source.activity_json is None:
                raise ValueError("Published Create source is missing immutable JSON")
            try:
                activity = json.loads(source.activity_json)
                obj = activity["object"]
                if (activity["type"] != "Create"
                        or activity["id"] != published_object.activity_id
                        or activity["actor"] != published_object.actor_url
                        or not isinstance(obj, dict)
                        or obj["id"] != published_object.object_id
                        or obj["attributedTo"] != published_object.actor_url
                        or obj["type"] not in ("Note", "Page")):
                    raise ValueError("Create identity conflicts with published object")
                if published_object.publication_context == "community" and (
                    obj.get("audience") != published_object.community_actor_url
                ):
                    raise ValueError("Create Group differs from published object")
                context = obj.get("context")
                if context is not None and (not isinstance(context, str) or not context):
                    raise ValueError("Invalid immutable Create context")
            except (KeyError, TypeError, json.JSONDecodeError) as exc:
                raise ValueError("Malformed immutable published Create") from exc
            return obj["type"], context

    def get_original_direct_person_actor_ids(
        self, published_object: PublishedActivityObject,
    ) -> list[str]:
        """Reconstruct only the original verified direct Person audience."""
        with self.session() as session:
            source = session.scalar(select(UserActorOutboundSource).where(
                UserActorOutboundSource.published_object_id == published_object.id,
                UserActorOutboundSource.source_kind == "content_create",
                UserActorOutboundSource.activity_id == published_object.activity_id,
            ))
            if source is None or source.activity_json is None:
                return []
            snapshot = json.loads(source.snapshot_json)
            activity = json.loads(source.activity_json)
            if "directPersonRecipients" not in snapshot:
                return []
            recipients = snapshot["directPersonRecipients"]
            if not isinstance(recipients, list):
                raise ValueError("invalid original direct Person recipients")
            actor_ids = [r["actorId"] for r in recipients]
            if (sorted(set(actor_ids)) != actor_ids
                    or activity.get("cc") != [published_object.actor_url, f"{published_object.actor_url}/followers", *actor_ids]
                    or not isinstance(activity.get("object"), dict)
                    or activity["object"].get("cc") != [f"{published_object.actor_url}/followers", *actor_ids]):
                # Do not invent recipient proof when existing JSON conflicts.
                raise ValueError("original direct Person audience conflicts with canonical Create")
            return actor_ids

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

    def load_user_outbox_slice(
        self, *, user: User, offset: int, limit: int
    ) -> UserOutboxSlice:
        """Load count and rows from one transaction-owned SQLite snapshot."""
        if offset < 0 or limit <= 0:
            raise ValueError("offset must be non-negative and limit must be positive")
        # Stage 4 removes legacy /users outbox compatibility; retained rows are
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
