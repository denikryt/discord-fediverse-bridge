"""Publish registered Discord replies once and mirror them into other feed copies.

A single locally authored source is the only ActivityPub Create. Other
Discord copies are bot placements of the same canonical Note, not publishers.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timezone
from hashlib import sha256

from sqlalchemy import select

from .activitypub_identity_urls import (
    build_user_comment_object_id, build_user_create_comment_activity_id,
)
from .content_sync.persistence import inspect_publish_artifacts, persist_publish_artifacts
from .fedify_gateway_client import RenderPersonalFeedRequest
from .models import (InboundConversationObject, MessageMapping, PublishedActivityObject,
                     RemotePersonCommentPlacement, RemotePersonPost, RemotePersonPostDelivery)
from .remote_person_feeds import RemotePersonChannelService

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class LocalRemotePersonReplyResult:
    """Observable disposition of one Discord-originated comment."""

    status: str
    reason: str
    object_id: str | None = None


class RemotePersonConversationRuntime:
    """Bridge messages in remote-root threads through the existing Person renderer."""

    def __init__(self, *, database: object, fedify_gateway: object,
                 policy_service: object, user_ban_service: object,
                 fedify_origin: str, delivery_worker: object | None = None,
                 user_delivery_worker: object | None = None) -> None:
        """Use one canonical publisher and existing source-bound user delivery."""
        self.database = database
        self.fedify_gateway = fedify_gateway
        self.policy_service = policy_service
        self.user_ban_service = user_ban_service
        self.fedify_origin = fedify_origin.rstrip('/')
        self.delivery_worker = delivery_worker
        self.user_delivery_worker = user_delivery_worker

    def get_remote_root_copy(self, thread_id: int) -> tuple[int, str] | None:
        """Find only a retained webhook thread, not an unrelated Discord thread."""
        with self.database.session() as session:
            copy = session.scalar(select(RemotePersonPostDelivery).where(
                RemotePersonPostDelivery.discord_thread_id == thread_id,
                RemotePersonPostDelivery.state == 'delivered',
            ))
            if copy is None:
                return None
            root = session.get(RemotePersonPost, copy.post_id)
            return (copy.id, root.object_id) if root is not None else None

    @staticmethod
    def _reply_parent(session: object, root: RemotePersonPost, copy: RemotePersonPostDelivery,
                      ref_id: int | None) -> str:
        """Resolve source/mirror/inbound in *this* root copy; default to its root."""
        if ref_id is None or ref_id == copy.discord_root_message_id:
            return root.object_id
        source = session.scalar(select(MessageMapping).where(
            MessageMapping.discord_message_id == ref_id,
            MessageMapping.publication_context == 'remote_person_feed',
            MessageMapping.discord_channel_id == copy.discord_thread_id,
        ))
        if source is not None:
            published = session.scalar(select(PublishedActivityObject).where(
                PublishedActivityObject.object_id == source.object_id,
                PublishedActivityObject.conversation_root_object_id == root.object_id,
            ))
            if published is not None:
                return published.object_id
        copy_placement = session.scalar(select(RemotePersonCommentPlacement).where(
            RemotePersonCommentPlacement.root_delivery_id == copy.id,
            RemotePersonCommentPlacement.discord_message_id == ref_id,
            RemotePersonCommentPlacement.state == 'delivered',
        ))
        if copy_placement is not None:
            if copy_placement.inbound_object_id is not None:
                inbound = session.get(InboundConversationObject, copy_placement.inbound_object_id)
                if inbound is not None and inbound.conversation_root_object_id == root.object_id:
                    return inbound.object_id
            if copy_placement.published_object_id is not None:
                published = session.get(PublishedActivityObject, copy_placement.published_object_id)
                if published is not None and published.conversation_root_object_id == root.object_id:
                    return published.object_id
        return root.object_id

    async def publish_local_reply(self, *, message: object) -> LocalRemotePersonReplyResult:
        """Create one signed Person Note then reserve bot mirrors in active copies."""
        channel = getattr(message, 'channel', None)
        guild_id = getattr(getattr(message, 'guild', None), 'id', None)
        thread_id = getattr(channel, 'id', None)
        message_id = getattr(message, 'id', None)
        if not all(isinstance(v, int) and v > 0 for v in (guild_id, thread_id, message_id)):
            return LocalRemotePersonReplyResult('ignored', 'invalid_discord_identity')
        with self.database.session() as session:
            copy = session.scalar(select(RemotePersonPostDelivery).where(
                RemotePersonPostDelivery.discord_thread_id == thread_id,
                RemotePersonPostDelivery.discord_guild_id == guild_id,
                RemotePersonPostDelivery.state == 'delivered',
            ))
            if copy is None:
                return LocalRemotePersonReplyResult('ignored', 'not_remote_person_thread')
            root = session.get(RemotePersonPost, copy.post_id)
            if root is None or root.deleted_at is not None:
                return LocalRemotePersonReplyResult('ignored', 'root_deleted')
            if not (self.policy_service.is_discord_guild_allowed(guild_id)
                    and self.policy_service.federation_decision(root.remote_actor_id).allowed
                    and RemotePersonChannelService._active_owner_exists(
                        session, root.remote_actor_id, guild_id, copy.discord_channel_id)):
                return LocalRemotePersonReplyResult('rejected', 'inactive_subscription')
            ref = getattr(getattr(message, 'reference', None), 'message_id', None)
            parent_id = self._reply_parent(session, root, copy, int(ref) if ref else None)
            root_id, parent_actor = root.object_id, root.remote_actor_id
            if parent_id != root_id:
                inbound = session.scalar(select(InboundConversationObject).where(
                    InboundConversationObject.object_id == parent_id,
                    InboundConversationObject.publication_context == 'remote_person_feed',
                ))
                if inbound is not None:
                    parent_actor = inbound.actor_id
        author_id = str(getattr(getattr(message, 'author', None), 'id', ''))
        if self.user_ban_service.check_global_discord_user(author_id).banned:
            return LocalRemotePersonReplyResult('rejected', 'user_banned')
        user = self.database.users.get_user_by_discord_user_id(author_id)
        if user is None:
            return LocalRemotePersonReplyResult('rejected', 'not_registered')
        body = str(getattr(message, 'content', ''))
        if not body.strip():
            return LocalRemotePersonReplyResult('ignored', 'empty_content')
        operation_id = sha256(f'remote-person-reply:{guild_id}:{thread_id}:{message_id}:{user.id}'.encode()).hexdigest()
        object_id = build_user_comment_object_id(
            origin=self.fedify_origin, username=user.activitypub_username, object_id=operation_id,
        )
        activity_id = build_user_create_comment_activity_id(
            origin=self.fedify_origin, username=user.activitypub_username, activity_id=operation_id,
        )
        created = getattr(message, 'created_at').astimezone(timezone.utc)
        published_at = created.isoformat(timespec='milliseconds').replace('+00:00', 'Z')
        existing = inspect_publish_artifacts(
            self.database, source_id=str(message_id), actor_url=user.actor_url,
            publication_context='remote_person_feed', community_actor_url=None,
            discord_guild_id=guild_id, discord_channel_id=thread_id,
            discord_message_id=message_id, activity_id=activity_id,
            object_id=object_id, published_at=created,
            kind='comment', in_reply_to_object_id=parent_id,
            conversation_root_object_id=root_id,
        )
        if existing is None:
            recipients = []
            for actor_id in sorted({root.remote_actor_id, parent_actor} - {user.actor_url}):
                if not self.policy_service.federation_decision(actor_id).allowed:
                    return LocalRemotePersonReplyResult('rejected', 'remote_actor_denied')
                try:
                    resolved = await self.fedify_gateway.resolve_remote_person(actor_id)
                    if resolved.actor_id != actor_id or not self.policy_service.federation_decision(resolved.inbox_url).allowed:
                        raise ValueError('verified actor/inbox mismatch or blocked endpoint')
                    recipients.append({'actorId': actor_id, 'inboxUrl': resolved.inbox_url})
                except Exception as exc:
                    logger.warning('Cannot resolve remote Person comment recipient %s: %s', actor_id, exc)
                    return LocalRemotePersonReplyResult('rejected', 'remote_parent_unresolvable')
            rendered = await self.fedify_gateway.render_personal_feed(RenderPersonalFeedRequest(
                actor_username=user.activitypub_username, operation_id=operation_id,
                activity_id=activity_id, object_id=object_id,
                body_markdown=body, published_at=published_at, kind='comment',
                in_reply_to_object_id=parent_id, conversation_root_object_id=root_id,
                direct_person_actor_ids=[r['actorId'] for r in recipients],
            ))
            persisted = persist_publish_artifacts(
                self.database, source_id=str(message_id), actor_username=user.activitypub_username,
                actor_url=user.actor_url, publication_context='remote_person_feed',
                community_actor_url=None, activity_id=activity_id, object_id=object_id,
                activity_json=rendered.activity_json, kind='comment', title=None,
                body_markdown=body, in_reply_to_object_id=parent_id,
                conversation_root_object_id=root_id, discord_guild_id=guild_id,
                discord_channel_id=thread_id, discord_message_id=message_id,
                published_at=created, direct_person_recipients=recipients,
            )
        else:
            persisted = existing
        with self.database.immediate_session() as session:
            published = session.scalar(select(PublishedActivityObject).where(
                PublishedActivityObject.object_id == object_id,
            ))
            root = session.scalar(select(RemotePersonPost).where(RemotePersonPost.object_id == root_id))
            if published is None or root is None:
                raise RuntimeError('canonical remote-Person reply persistence incomplete')
            # A mirror is never created in its original thread; the user's
            # original message is the placement in that destination.
            copies = session.scalars(select(RemotePersonPostDelivery).where(
                RemotePersonPostDelivery.post_id == root.id,
                # Reserve a mirror before the destination webhook root exists.
                # The comment worker waits for that root and its Discord thread.
                RemotePersonPostDelivery.state.in_((
                    'pending', 'claimed', 'sending', 'uncertain', 'delivered',
                )),
            )).all()
            for target in copies:
                if (target.discord_thread_id == thread_id
                        or not self.policy_service.is_discord_guild_allowed(target.discord_guild_id)
                        or not RemotePersonChannelService._active_owner_exists(
                            session, root.remote_actor_id,
                            target.discord_guild_id, target.discord_channel_id)):
                    continue
                duplicate = session.scalar(select(RemotePersonCommentPlacement.id).where(
                    RemotePersonCommentPlacement.root_delivery_id == target.id,
                    RemotePersonCommentPlacement.published_object_id == published.id,
                ))
                if duplicate is None:
                    session.add(RemotePersonCommentPlacement(
                        root_delivery_id=target.id, published_object_id=published.id,
                        destination_thread_id=target.discord_thread_id,
                        # Preserve the destination even when its webhook root
                        # exists but thread creation is still being retried.
                        state='pending' if target.discord_thread_id is not None else 'pending_thread',
                    ))
        if self.user_delivery_worker is not None:
            self.user_delivery_worker.wake()
        if self.delivery_worker is not None:
            self.delivery_worker.wake()
        return LocalRemotePersonReplyResult('published' if existing is None else 'already_published',
                                            'published' if existing is None else 'duplicate', object_id)
