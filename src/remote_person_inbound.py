"""Canonical Remote Person Note reservation, fanout, and mutation admission.

The Fedify gateway authenticates the signed outer actor. This module enforces
owner, audience, current accepted Follow and per-target dedup inside SQLite.
It performs no Discord side effects: delivery claims belong to CP10.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select

from .activitypub_models import RemotePersonContentEvent, activity_actor_id
from .db import Database
from .models import RemotePersonPost, RemotePersonPostDelivery, RemotePersonFollow, RemotePersonChannelAssignment, utcnow

PUBLIC = 'https://www.w3.org/ns/activitystreams#Public'


@dataclass(frozen=True, slots=True)
class RemotePersonReservation:
    """Explain one authenticated event's canonical persistence disposition."""

    status: str
    post_id: int | None = None
    target_count: int = 0


class RemotePersonInboundService:
    """Reserve each canonical remote root independently of its Discord copies."""

    def __init__(self, database: Database) -> None:
        """Share the app's serialized SQLite writer and Follow generation state."""
        self.database = database

    @staticmethod
    def _validated_source(event: RemotePersonContentEvent) -> dict | None:
        """Reject unverified attribution, non-public roots, and mismatched IDs."""
        if event.actor_id != event.verified_sender_actor_id:
            return None
        source = event.source_activity_json
        if not isinstance(source, dict) or activity_actor_id(source.get('actor')) != event.actor_id:
            return None
        if event.event_type.endswith('.deleted'):
            target = source.get('object')
            identifier = target if isinstance(target, str) else target.get('id') if isinstance(target, dict) else None
            return source if source.get('type') == 'Delete' and identifier == event.object.ap_id else None
        obj = source.get('object')
        if not isinstance(obj, dict) or obj.get('type') != 'Note':
            return None
        if (obj.get('id') != event.object.ap_id or activity_actor_id(obj.get('attributedTo')) != event.actor_id
                or obj.get('inReplyTo') is not None):
            return None
        if (source.get('type') not in ('Create', 'Update')
                or PUBLIC not in _audience(obj.get('to'))
                or PUBLIC not in _audience(source.get('to'))):
            return None
        return source

    def reserve(self, event: RemotePersonContentEvent, *, guild_allowed=lambda _id: True) -> RemotePersonReservation:
        """Create one remote root and DISTINCT eligible channel jobs transactionally."""
        source = self._validated_source(event)
        if source is None:
            return RemotePersonReservation('invalid_identity_or_audience')
        with self.database.immediate_session() as session:
            post = session.scalar(select(RemotePersonPost).where(RemotePersonPost.object_id == event.object.ap_id))
            if post is not None:
                if post.remote_actor_id != event.actor_id:
                    # A signed Delete arriving before Create cannot establish
                    # someone else's authorship. Its provisional tombstone may
                    # be superseded by the object's verified actual author.
                    if (post.deleted_at is not None and post.create_activity_id is None
                            and event.event_type.endswith('.created')):
                        session.delete(post)
                        session.flush()
                        post = None
                    else:
                        return RemotePersonReservation('wrong_owner', post.id)
                if post is not None and post.deleted_at is not None:
                    return RemotePersonReservation('deleted', post.id)
            if post is not None:
                if event.event_type.endswith('.created'):
                    return RemotePersonReservation('duplicate', post.id)
                if event.event_type.endswith('.deleted'):
                    post.deleted_at = utcnow()
                    for target in session.scalars(select(RemotePersonPostDelivery).where(
                        RemotePersonPostDelivery.post_id == post.id,
                        RemotePersonPostDelivery.state.in_(('pending', 'claimed')),
                    )):
                        target.state = 'suppressed'
                        target.claim_token = None
                        target.last_error = 'remote source deleted before Discord create'
                    return RemotePersonReservation('deleted', post.id)
                # A known author may update the same Note but never move its
                # publication backwards to an earlier authenticated revision.
                if post.updated_at_remote is not None and _utc(event.occurred_at) <= _utc(post.updated_at_remote):
                    return RemotePersonReservation('stale', post.id)
                post.content_markdown = event.object.body_markdown or ''
                post.revision_fingerprint = _fingerprint(source)
                post.source_activity_json = json.dumps(source, sort_keys=True)
                post.updated_at_remote = event.occurred_at
                return RemotePersonReservation('updated', post.id)
            if event.event_type.endswith('.updated'):
                # An Update cannot establish a never-seen Create or its audience.
                return RemotePersonReservation('unknown_object')
            if event.event_type.endswith('.deleted'):
                # Suppress a later out-of-order Create from this same actor.
                # No deliveries are queued for an unknown Delete; the tombstone
                # remains reversible only if a different verified author later
                # proves canonical ownership with their own signed Create.
                accepted = session.scalar(select(RemotePersonFollow.id).where(
                    RemotePersonFollow.remote_actor_id == event.actor_id,
                    RemotePersonFollow.relationship_status == 'accepted',
                ).limit(1))
                if accepted is None:
                    return RemotePersonReservation('unknown_object')
                tombstone = RemotePersonPost(
                    object_id=event.object.ap_id, create_activity_id=None,
                    remote_actor_id=event.actor_id, remote_handle=None,
                    remote_avatar_url=None, content_markdown='',
                    source_activity_json=json.dumps(source, sort_keys=True),
                    revision_fingerprint=_fingerprint(source),
                    deleted_at=utcnow(), updated_at_remote=event.occurred_at,
                )
                session.add(tombstone)
                session.flush()
                return RemotePersonReservation('deleted', tombstone.id)
            # Never persist unfollowed authors, even if Fedify has delivered to
            # the bridge's shared inbox or a stale actor-specific inbox.
            eligible = set(session.execute(select(
                RemotePersonChannelAssignment.discord_guild_id,
                RemotePersonChannelAssignment.discord_channel_id,
            ).join(RemotePersonFollow, RemotePersonFollow.id == RemotePersonChannelAssignment.follow_id)
             .where(RemotePersonFollow.remote_actor_id == event.actor_id,
                    RemotePersonFollow.relationship_status == 'accepted')).all())
            eligible = {(guild, channel) for guild, channel in eligible if guild_allowed(guild)}
            if not eligible:
                return RemotePersonReservation('no_accepted_assignment')
            post = RemotePersonPost(
                object_id=event.object.ap_id, create_activity_id=event.source_activity_id or event.delivery_id,
                remote_actor_id=event.actor_id, remote_handle=event.object.author_name,
                remote_avatar_url=event.object.avatar_url, content_markdown=event.object.body_markdown or '',
                source_activity_json=json.dumps(source, sort_keys=True),
                revision_fingerprint=_fingerprint(source), published_at=event.object.published_at,
                updated_at_remote=event.occurred_at,
            )
            session.add(post)
            session.flush()
            for guild_id, channel_id in sorted(eligible):
                session.add(RemotePersonPostDelivery(post_id=post.id,
                    discord_guild_id=guild_id, discord_channel_id=channel_id, state='pending'))
            return RemotePersonReservation('reserved', post.id, len(eligible))


def _audience(value: object) -> set[str]:
    """Read only explicit ActivityStreams recipient IRIs from a typed source."""
    return {value} if isinstance(value, str) else {
        x for x in value if isinstance(x, str)
    } if isinstance(value, list) else set()


def _fingerprint(source: dict) -> str:
    """Fingerprint an immutable authenticated wire event without random IDs."""
    return hashlib.sha256(json.dumps(source, sort_keys=True).encode()).hexdigest()


def _utc(value: datetime) -> datetime:
    """Interpret SQLite's naive UTC DATETIME consistently with aware AP times."""
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
