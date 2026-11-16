"""Admit verified public replies to retained Remote Person conversations.

Canonical incoming Note identity is stored in InboundConversationObject, which
is independent of both the remote root and each Discord destination. Placement
rows carry per-target send states; unrelated public comments are not retained.
"""
from __future__ import annotations

import json
from datetime import timedelta
from dataclasses import dataclass

from sqlalchemy import delete, func, select

from .activitypub_models import RemotePersonCommentEvent, activity_actor_id
from .models import (
    InboundConversationObject, PublishedActivityObject, RemotePersonPost,
    RemotePersonPostDelivery, RemotePersonCommentPlacement,
    RemotePersonFollow, RemotePersonChannelAssignment, utcnow,
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

    MAX_DEFERRED_OBJECTS = 1000
    DEFERRED_RETENTION = timedelta(days=7)

    @classmethod
    def _can_stage_unresolved(cls, session: object, guild_allowed: object) -> bool:
        """Bound unaffiliated signed events without admitting Discord fanout."""
        # Only a bridge with currently accepted destinations can retain an
        # unknown ancestry candidate. Staged rows are never channel claims.
        subscribers = session.execute(select(RemotePersonChannelAssignment.discord_guild_id)
            .join(RemotePersonFollow, RemotePersonFollow.id == RemotePersonChannelAssignment.follow_id)
            .where(RemotePersonFollow.relationship_status == 'accepted')).scalars().all()
        if not any(guild_allowed(guild) for guild in subscribers):
            return False
        session.execute(delete(InboundConversationObject).where(
            InboundConversationObject.publication_context == 'remote_person_feed',
            InboundConversationObject.conversation_root_object_id == '',
            InboundConversationObject.created_at < utcnow() - cls.DEFERRED_RETENTION,
        ))
        count = session.scalar(select(func.count(InboundConversationObject.id)).where(
            InboundConversationObject.publication_context == 'remote_person_feed',
            InboundConversationObject.conversation_root_object_id == '',
        )) or 0
        return count < cls.MAX_DEFERRED_OBJECTS

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

    @staticmethod
    def _reserve_placements(session: object, comment: InboundConversationObject,
                            root: RemotePersonPost, guild_allowed: object) -> int:
        """Create a destination placement once, even while its thread is pending."""
        copies = session.scalars(select(RemotePersonPostDelivery).where(
            RemotePersonPostDelivery.post_id == root.id,
            RemotePersonPostDelivery.state.in_(('pending', 'claimed', 'sending', 'uncertain', 'delivered')),
        )).all()
        count = 0
        for copy in copies:
            if (not guild_allowed(copy.discord_guild_id)
                    or not RemotePersonChannelService._active_owner_exists(
                        session, root.remote_actor_id, copy.discord_guild_id, copy.discord_channel_id)):
                continue
            existing = session.scalar(select(RemotePersonCommentPlacement.id).where(
                RemotePersonCommentPlacement.root_delivery_id == copy.id,
                RemotePersonCommentPlacement.inbound_object_id == comment.id,
            ))
            if existing is None:
                session.add(RemotePersonCommentPlacement(
                    root_delivery_id=copy.id, inbound_object_id=comment.id,
                    destination_thread_id=copy.discord_thread_id,
                    state='pending' if copy.discord_thread_id is not None else 'pending_thread',
                ))
                count += 1
        return count

    @classmethod
    def restore_deferred_descendants(cls, session: object, *, root: RemotePersonPost,
                                     parent_object_id: str, guild_allowed: object) -> int:
        """Resolve signed children stored before their canonical parent was known.

        Each object is visited once and retains its originally signed parent;
        the resulting Discord placements remain scoped to this root's copies.
        """
        pending = [parent_object_id]
        visited = {parent_object_id}
        total = 0
        while pending:
            parent = pending.pop(0)
            descendants = session.scalars(select(InboundConversationObject).where(
                InboundConversationObject.publication_context == 'remote_person_feed',
                InboundConversationObject.parent_object_id == parent,
            )).all()
            for child in descendants:
                if child.object_id in visited:
                    continue
                visited.add(child.object_id)
                child.conversation_root_object_id = root.object_id
                if child.deleted_at is None:
                    total += cls._reserve_placements(session, child, root, guild_allowed)
                pending.append(child.object_id)
        return total

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
                    # An unknown Delete cannot prove ownership of a Note that
                    # has never been created; a later signed real Create wins.
                    if (operation == 'created' and existing.publication_context == 'remote_person_feed'
                            and existing.deleted_at is not None and existing.parent_object_id is None):
                        session.delete(existing)
                        session.flush()
                        existing = None
                    else:
                        return RemoteCommentReservation('wrong_owner', existing.id)
            if existing is not None:
                if operation == 'created':
                    return RemoteCommentReservation(
                        'deleted' if existing.deleted_at is not None else 'duplicate', existing.id,
                    )
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
            if operation == 'deleted':
                # Signed deletion may precede the signed Create. Persist only a
                # bounded author-scoped tombstone; it grants no Discord fanout
                # and cannot defeat a future Create signed by a different actor.
                if not self._can_stage_unresolved(session, guild_allowed):
                    return RemoteCommentReservation('deferred_not_admitted')
                tombstone = InboundConversationObject(
                    object_id=event.object.ap_id, publication_context='remote_person_feed',
                    activity_id=event.source_activity_id or event.delivery_id,
                    conversation_root_object_id='', parent_object_id=None,
                    actor_id=event.actor_id, actor_name=event.object.author_name,
                    content_markdown='', source_activity_json=json.dumps(source, sort_keys=True),
                    updated_at_remote=event.occurred_at, deleted_at=utcnow(),
                    revision_fingerprint=_fingerprint(source),
                )
                session.add(tombstone)
                session.flush()
                return RemoteCommentReservation('deleted_before_create', tombstone.id)
            if operation != 'created':
                return RemoteCommentReservation('unknown_object')
            parent_id = event.object.parent_ap_id
            resolved = self._resolve_parent(session, parent_id)
            if resolved is None:
                # The parent may be a signed Note that arrives later. Store the
                # verified child without a root or Discord placement; no
                # unverified ancestry is guessed at this point.
                if not self._can_stage_unresolved(session, guild_allowed):
                    return RemoteCommentReservation('deferred_not_admitted')
                child = InboundConversationObject(
                    object_id=event.object.ap_id, publication_context='remote_person_feed',
                    activity_id=event.source_activity_id or event.delivery_id,
                    conversation_root_object_id='', parent_object_id=parent_id,
                    actor_id=event.actor_id, actor_name=event.object.author_name,
                    content_markdown=event.object.body_markdown or '',
                    source_activity_json=json.dumps(source, sort_keys=True),
                    published_at=event.object.published_at, updated_at_remote=event.occurred_at,
                    revision_fingerprint=_fingerprint(source),
                )
                session.add(child)
                session.flush()
                return RemoteCommentReservation('pending_parent', child.id)
            root, root_id = resolved
            if root.deleted_at is not None:
                return RemoteCommentReservation('root_deleted')
            # A canonical parent alone does not grant a new destination.
            if not any(guild_allowed(copy.discord_guild_id)
                       and RemotePersonChannelService._active_owner_exists(
                           session, root.remote_actor_id, copy.discord_guild_id, copy.discord_channel_id)
                       for copy in session.scalars(select(RemotePersonPostDelivery).where(
                           RemotePersonPostDelivery.post_id == root.id))):
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
            targets = self._reserve_placements(session, comment, root, guild_allowed)
            targets += self.restore_deferred_descendants(
                session, root=root, parent_object_id=comment.object_id, guild_allowed=guild_allowed,
            )
            return RemoteCommentReservation('reserved', comment.id, targets)
