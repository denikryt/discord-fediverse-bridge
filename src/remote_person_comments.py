"""Admit verified public replies to retained Remote Person conversations.

Canonical incoming Note identity is stored in InboundConversationObject, which
is independent of both the remote root and each Discord destination. Placement
rows carry per-target send states; unrelated public comments are not retained.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy import select

from .activitypub_models import RemotePersonCommentEvent, activity_actor_id
from .models import (
    InboundConversationObject, PublishedActivityObject, RemotePersonPost,
    RemotePersonPostDelivery, RemotePersonCommentPlacement, utcnow,
)
from .remote_person_feeds import RemotePersonChannelService
from .remote_person_inbound import PUBLIC, _fingerprint, _utc


@dataclass(frozen=True, slots=True)
class RemoteCommentReservation:
    """Describe one signed comment and its committed Discord placements."""

    status: str
    comment_id: int | None = None
    targets: int = 0


class RemotePersonCommentService:
    """Map public signed Note ancestry to only known subscribed root copies."""

    def __init__(self, database: object) -> None:
        """Use the application's existing serialized SQLite write boundary."""
        self.database = database

    @staticmethod
    def _canonical_source(event: RemotePersonCommentEvent) -> dict | None:
        """Verify authenticated author, ID, Note type, parent and public address."""
        if event.actor_id != event.verified_sender_actor_id:
            return None
        source = event.source_activity_json
        if activity_actor_id(source.get('actor')) != event.actor_id:
            return None
        operation = event.event_type.rsplit('.', 1)[-1]
        if operation == 'deleted':
            target = source.get('object')
            target_id = target if isinstance(target, str) else target.get('id') if isinstance(target, dict) else None
            return source if source.get('type') == 'Delete' and target_id == event.object.ap_id else None
        obj = source.get('object')
        if (not isinstance(obj, dict) or obj.get('type') != 'Note'
                or source.get('type') != {'created': 'Create', 'updated': 'Update'}[operation]
                or obj.get('id') != event.object.ap_id
                or activity_actor_id(obj.get('attributedTo')) != event.actor_id
                or obj.get('inReplyTo') != event.object.parent_ap_id):
            return None
        def recipients(value: object) -> set[str]:
            """Require explicit public audience without granting inferred targets."""
            return {value} if isinstance(value, str) else {
                item for item in value if isinstance(item, str)
            } if isinstance(value, list) else set()
        if (PUBLIC not in recipients(source.get('to')) | recipients(source.get('cc'))
                or PUBLIC not in recipients(obj.get('to')) | recipients(obj.get('cc'))):
            return None
        return source

    @staticmethod
    def _resolve_parent(session: object, parent_id: str) -> tuple[RemotePersonPost, str] | None:
        """Resolve only parents already bound to a canonical remote root."""
        root = session.scalar(select(RemotePersonPost).where(RemotePersonPost.object_id == parent_id))
        if root is not None:
            return root, root.object_id
        inbound = session.scalar(select(InboundConversationObject).where(
            InboundConversationObject.object_id == parent_id,
            InboundConversationObject.publication_context == 'remote_person_feed',
        ))
        if inbound is not None:
            root = session.scalar(select(RemotePersonPost).where(
                RemotePersonPost.object_id == inbound.conversation_root_object_id,
            ))
            return (root, inbound.conversation_root_object_id) if root is not None else None
        published = session.scalar(select(PublishedActivityObject).where(
            PublishedActivityObject.object_id == parent_id,
            PublishedActivityObject.publication_context == 'remote_person_feed',
        ))
        if published is not None:
            root = session.scalar(select(RemotePersonPost).where(
                RemotePersonPost.object_id == published.conversation_root_object_id,
            ))
            return (root, published.conversation_root_object_id) if root is not None else None
        return None

    def resolve_conversation_target(self, object_id: str) -> tuple[str, str, str | None] | None:
        """Expose a narrow signed-inbox parent lookup without Discord identity."""
        with self.database.session() as session:
            resolved = self._resolve_parent(session, object_id)
            if resolved is None:
                return None
            root, _ = resolved
            if root.object_id == object_id:
                return 'remote_person_feed', root.object_id, root.remote_actor_id
            inbound = session.scalar(select(InboundConversationObject).where(
                InboundConversationObject.object_id == object_id,
                InboundConversationObject.publication_context == 'remote_person_feed',
            ))
            return ('remote_person_feed', root.object_id,
                    inbound.actor_id if inbound is not None else None)

    def reserve(self, event: RemotePersonCommentEvent, *, guild_allowed=lambda _guild: True) -> RemoteCommentReservation:
        """Persist one canonical reply and DISTINCT eligible per-root comment jobs."""
        source = self._canonical_source(event)
        if source is None:
            return RemoteCommentReservation('invalid_identity_or_audience')
        operation = event.event_type.rsplit('.', 1)[-1]
        with self.database.immediate_session() as session:
            existing = session.scalar(select(InboundConversationObject).where(
                InboundConversationObject.object_id == event.object.ap_id,
            ))
            if existing is not None:
                if existing.publication_context != 'remote_person_feed' or existing.actor_id != event.actor_id:
                    return RemoteCommentReservation('wrong_owner', existing.id)
                if operation == 'created':
                    return RemoteCommentReservation('duplicate', existing.id)
                if operation == 'deleted':
                    if existing.deleted_at is not None:
                        return RemoteCommentReservation('duplicate', existing.id)
                    existing.deleted_at = utcnow()
                    for placement in session.scalars(select(RemotePersonCommentPlacement).where(
                        RemotePersonCommentPlacement.inbound_object_id == existing.id,
                        RemotePersonCommentPlacement.state.in_(('pending_parent', 'pending_thread', 'pending', 'claimed')),
                    )):
                        placement.state = 'suppressed'
                        placement.claim_token = None
                        placement.last_error = 'remote reply deleted before Discord delivery'
                    return RemoteCommentReservation('deleted', existing.id)
                if existing.deleted_at is not None:
                    return RemoteCommentReservation('deleted', existing.id)
                if event.object.parent_ap_id != existing.parent_object_id:
                    return RemoteCommentReservation('parent_changed', existing.id)
                if (existing.updated_at_remote is not None
                        and _utc(event.occurred_at) <= _utc(existing.updated_at_remote)):
                    return RemoteCommentReservation('stale', existing.id)
                existing.content_markdown = event.object.body_markdown or ''
                existing.source_activity_json = json.dumps(source, sort_keys=True)
                existing.revision_fingerprint = _fingerprint(source)
                existing.updated_at_remote = event.occurred_at
                return RemoteCommentReservation('updated', existing.id)
            if operation != 'created':
                # Unknown Update/Delete cannot invent a canonical author/root.
                return RemoteCommentReservation('unknown_object')
            parent_id = event.object.parent_ap_id
            resolved = self._resolve_parent(session, parent_id)
            if resolved is None:
                return RemoteCommentReservation('unknown_parent')
            root, root_id = resolved
            if root.deleted_at is not None:
                return RemoteCommentReservation('root_deleted')
            copies = session.scalars(select(RemotePersonPostDelivery).where(
                RemotePersonPostDelivery.post_id == root.id,
                RemotePersonPostDelivery.state.in_(('pending', 'claimed', 'sending', 'uncertain', 'delivered')),
            )).all()
            eligible = [copy for copy in copies if guild_allowed(copy.discord_guild_id)
                and RemotePersonChannelService._active_owner_exists(session, root.remote_actor_id,
                    copy.discord_guild_id, copy.discord_channel_id)]
            if not eligible:
                return RemoteCommentReservation('no_accepted_assignment')
            comment = InboundConversationObject(
                object_id=event.object.ap_id,
                publication_context='remote_person_feed',
                activity_id=event.source_activity_id or event.delivery_id,
                conversation_root_object_id=root_id,
                parent_object_id=parent_id,
                actor_id=event.actor_id,
                actor_name=event.object.author_name,
                content_markdown=event.object.body_markdown or '',
                source_activity_json=json.dumps(source, sort_keys=True),
                published_at=event.object.published_at,
                updated_at_remote=event.occurred_at,
                revision_fingerprint=_fingerprint(source),
            )
            session.add(comment)
            session.flush()
            for copy in eligible:
                session.add(RemotePersonCommentPlacement(
                    root_delivery_id=copy.id, inbound_object_id=comment.id,
                    destination_thread_id=copy.discord_thread_id,
                    state='pending' if copy.discord_thread_id is not None else 'pending_thread',
                ))
            return RemoteCommentReservation('reserved', comment.id, len(eligible))
