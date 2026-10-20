"""Personal-feed conversation runtime for Discord replies and inbound remote comments."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable
import logging
from hashlib import sha256

from .content_sync.persistence import (
    inspect_publish_artifacts,
    persist_publish_artifacts,
)
from .fedify_gateway_client import RenderPersonalFeedRequest
from .platform_types import (
    ThreadCreated, MessageCreated, PlatformOperationError, CreateAttempt,
    ContentDocument, DeliveryPresentation, validate_message_reply,
)
from .discord_legacy_projection import discord_id_to_legacy_int
from .activitypub_identity_urls import (
    build_user_comment_object_id,
    build_user_create_comment_activity_id,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PersonalFeedConversationResult:
    """Describe one routed personal-feed conversation action."""

    status: str
    reason: str
    object_id: str | None = None


class PersonalFeedConversationRuntime:
    """Own personal-feed thread identity, local replies, and canonical inbound admission."""

    def __init__(
        self,
        *,
        database: object,
        personal_feed_service: object,
        fedify_gateway: object,
        user_ban_service: object,
        bridge_policy_service: object,
        fedify_origin: str,
        delivery_worker: object | None = None,
    ) -> None:
        """Bind non-secret routing services and the gateway rendering adapter."""
        self.database = database
        self.personal_feed_service = personal_feed_service
        self.fedify_gateway = fedify_gateway
        self.user_ban_service = user_ban_service
        self.bridge_policy_service = bridge_policy_service
        self.fedify_origin = fedify_origin.rstrip("/")
        self.delivery_worker = delivery_worker


    def bind_delivery_wake(self, delivery_worker: object) -> None:
        """Attach the wake-only Discord delivery edge once after worker creation."""
        if self.delivery_worker is not None:
            raise RuntimeError("Personal-feed conversation delivery wake is already configured")
        self.delivery_worker = delivery_worker

    def adopt_normalized_thread(self, event: ThreadCreated) -> PersonalFeedConversationResult:
        """Adopt a root-anchored personal discussion from verified platform identity."""
        if event.connector_key != 'discord-primary':
            raise PlatformOperationError('invalid_request', 'wrong Personal Feed thread connector')
        if (event.thread.channel.kind not in ('text', 'announcement')
            or event.external_guild_id is None):
            return PersonalFeedConversationResult('ignored', 'not_personal_feed_channel')
        # Both root and parent use local subscriber synchronization checked legacy projections. Never
        # infer a parent from the thread ID or a guessed starter-message ID.
        thread_id = discord_id_to_legacy_int(event.thread)
        parent_id = discord_id_to_legacy_int(event.thread, component='parent')
        return self._adopt_thread_ids(thread_id=thread_id, parent_id=parent_id)

    def adopt_manual_thread(self, *, thread: object) -> PersonalFeedConversationResult:
        """Retain existing direct caller while all forum Create paths are migrating."""
        return self._adopt_thread_ids(
            thread_id=int(getattr(thread, 'id')),
            parent_id=int(getattr(thread, 'parent_id')),
        )

    def _adopt_thread_ids(
        self, *, thread_id: int, parent_id: int,
    ) -> PersonalFeedConversationResult:
        """Verify persisted canonical root and matching parent before adoption."""
        root = self.database.activitypub_objects.get_published_activity_object_by_discord_message_id(
            thread_id
        )
        if root is None or root.publication_context != "personal_feed":
            return PersonalFeedConversationResult(
                "ignored", "unknown_personal_feed_root"
            )
        if root.discord_channel_id != parent_id:
            return PersonalFeedConversationResult("ignored", "wrong_parent_channel")
        self.database.personal_feed_conversations.adopt_thread(
            root_object_id=root.object_id,
            channel_id=parent_id,
            root_message_id=thread_id,
            thread_id=thread_id,
        )
        return PersonalFeedConversationResult("adopted", "mapped", root.object_id)

    def handle_inbound_event(self, event: object) -> PersonalFeedConversationResult:
        """Admit one normalized remote Create, Update, or Delete before Discord effects."""
        target = getattr(event, "target", None)
        root_object_id = getattr(target, "root_object_id", None)
        obj = getattr(event, "object")
        event_type = str(getattr(event, "event_type"))
        if event_type == "comment.created":
            parent_id = getattr(obj, "parent_ap_id", None)
            parent_known = bool(
                parent_id == root_object_id
                or self.database.personal_feed_conversations.resolve_conversation_object(
                    parent_id
                )
            )
            root = self.database.activitypub_objects.get_published_activity_object_by_object_id(
                root_object_id
            )
            if root is None:
                return PersonalFeedConversationResult("skipped", "unknown_root")
            result = self.database.personal_feed_conversations.admit_create(
                activity_id=getattr(event, "source_activity_id", None)
                or getattr(event, "delivery_id"),
                object_id=obj.ap_id,
                root_object_id=root_object_id,
                parent_object_id=parent_id,
                activity_actor_id=event.actor_id,
                attributed_to_actor_id=obj.attributed_to_actor_id,
                actor_name=obj.author_name,
                content_markdown=obj.body_markdown or "",
                published_at=obj.published_at,
                source_activity=getattr(event, "source_activity_json", None),
                destination_channel_id=int(root.discord_channel_id),
                parent_known=parent_known,
            )
            if result.status in {"created", "duplicate"}:
                self.database.personal_feed_conversations.wake_children(obj.ap_id)
                if self.delivery_worker is not None:
                    self.delivery_worker.wake()
            return PersonalFeedConversationResult(
                "accepted" if result.status in {"created", "duplicate"} else "rejected",
                result.status,
                obj.ap_id,
            )
        if event_type == "comment.updated":
            result = self.database.personal_feed_conversations.admit_update(
                object_id=obj.ap_id,
                activity_actor_id=event.actor_id,
                attributed_to_actor_id=obj.attributed_to_actor_id,
                content_markdown=obj.body_markdown or "",
                updated_at=obj.published_at,
                source_activity=getattr(event, "source_activity_json", None),
            )
            if result.status == "accepted" and self.delivery_worker is not None:
                self.delivery_worker.wake()
            return PersonalFeedConversationResult(
                "accepted"
                if result.status in {"accepted", "duplicate", "stale"}
                else "rejected",
                result.status,
                obj.ap_id,
            )
        if event_type == "comment.deleted":
            result = self.database.personal_feed_conversations.admit_delete(
                object_id=obj.ap_id, activity_actor_id=event.actor_id
            )
            if result.status == "accepted" and self.delivery_worker is not None:
                self.delivery_worker.wake()
            return PersonalFeedConversationResult(
                "accepted"
                if result.status in {"accepted", "duplicate"}
                else "rejected",
                result.status,
                obj.ap_id,
            )
        return PersonalFeedConversationResult("ignored", "unsupported_event")

    async def publish_local_reply(
        self, *, message: object
    ) -> PersonalFeedConversationResult:
        """Retain the original SDK-shaped caller until the forum cutover is complete.

        This compatibility entry extracts observed fields and delegates to the
        same canonical publication logic as the normalized ingress. No new
        ActivityPub identity is computed from SDK-specific objects.
        """
        thread = getattr(message, 'channel', None)
        reference = getattr(message, 'reference', None)
        reply = getattr(message, 'reply', None)

        async def notice() -> None:
            """Keep the existing registration notice for legacy direct callers."""
            if callable(reply):
                await reply('Register your bridge account before posting federated replies.')

        return await self._publish_reply_fields(
            guild_id=getattr(getattr(message, 'guild', None), 'id', None),
            thread_id=getattr(thread, 'id', None),
            message_id=int(getattr(message, 'id')),
            author_id=str(getattr(getattr(message, 'author', None), 'id', '')),
            body=str(getattr(message, 'content', '')),
            created_at=getattr(message, 'created_at'),
            reference_id=getattr(reference, 'message_id', None),
            notice=notice,
        )

    async def publish_normalized_reply(
        self, event: MessageCreated, *, adapter: object | None = None,
    ) -> PersonalFeedConversationResult:
        """Publish one verified neutral Personal Feed reply with unchanged AP IDs.

        The current persisted root and registration policy own the decision;
        the adapter is used only for an optional best-effort physical notice.
        """
        if event.connector_key != 'discord-primary':
            raise PlatformOperationError('invalid_request', 'wrong Personal Feed reply connector')
        if (event.thread_context is None or event.parent_channel is None
            or event.parent_channel.kind not in ('text', 'announcement')
            or event.author.kind != 'human'
            or event.author.external_actor_id is None
            or event.external_guild_id is None or event.created_at is None):
            return PersonalFeedConversationResult('ignored', 'invalid_source')
        if event.content.attachments:
            raise PlatformOperationError('unsupported', 'Personal Feed reply attachments are unsupported')
        if (event.message.external_container_id != event.thread_context.external_thread_id
            or event.parent_channel != event.thread_context.channel):
            raise PlatformOperationError('invalid_request', 'reply thread identity mismatch')
        validate_message_reply(event.thread_context, event.reply_to)
        if (event.created_at.tzinfo is None or event.created_at.utcoffset() is None):
            return PersonalFeedConversationResult('ignored', 'invalid_timestamp')
        thread_id = discord_id_to_legacy_int(event.thread_context)
        parent_id = discord_id_to_legacy_int(event.thread_context, component='parent')
        message_id = discord_id_to_legacy_int(event.message)
        guild_id = int(event.external_guild_id) if event.external_guild_id.isdecimal() else None
        reference_id = (discord_id_to_legacy_int(event.reply_to)
                        if event.reply_to is not None else None)

        async def notice() -> None:
            """Reply once with a registration hint, without federating the notice."""
            if adapter is None:
                return
            await adapter.send_message(
                event.thread_context,
                ContentDocument('Register your bridge account before posting federated replies.'),
                reply_to=event.message,
                presentation=DeliveryPresentation(),
                attempt=CreateAttempt(
                    f'personal-feed-registration-notice:{event.connector_key}:{thread_id}:{message_id}',
                    event.connector_key, 'message', event.thread_context,
                ),
            )

        return await self._publish_reply_fields(
            guild_id=guild_id, thread_id=thread_id, message_id=message_id,
            author_id=event.author.external_actor_id, body=event.content.body,
            created_at=event.created_at, reference_id=reference_id,
            parent_channel_id=parent_id, notice=notice,
        )

    async def _publish_reply_fields(
        self, *, guild_id: int | None, thread_id: int | None,
        message_id: int, author_id: str, body: str, created_at: datetime,
        reference_id: int | None,
        parent_channel_id: int | None = None,
        notice: Callable[[], Awaitable[None]] | None = None,
    ) -> PersonalFeedConversationResult:
        """Persist the same canonical reply for both legacy and normalized callers."""
        root = (
            self.database.personal_feed_conversations.get_root_for_thread(
                int(thread_id)
            )
            if thread_id
            else None
        )
        if root is None:
            return PersonalFeedConversationResult("ignored", "unknown_thread")
        # The persisted source parent is authoritative even if a Gateway DTO
        # references an identically numbered thread under a different channel.
        if (parent_channel_id is not None and
            int(root.discord_channel_id) != parent_channel_id):
            return PersonalFeedConversationResult('ignored', 'wrong_parent_channel')
        if (guild_id is not None and root.discord_guild_id is not None
            and int(root.discord_guild_id) != guild_id):
            return PersonalFeedConversationResult('ignored', 'wrong_guild')
        # A terminally deleted root no longer accepts outbound Discord replies.
        # The thread mapping remains archival, but it must not mint new objects
        # after the conversation owner deleted the root publication.
        if root.deleted_at is not None:
            return PersonalFeedConversationResult("ignored", "root_deleted")
        if not self.bridge_policy_service.is_discord_guild_allowed(guild_id):
            return PersonalFeedConversationResult("ignored", "guild_disallowed")
        root_user = self.database.users.get_user_by_actor_url(root.actor_url)
        if (
            root_user is None
            or not self.personal_feed_service.is_active_personal_feed_channel(
                root_user.id, int(root.discord_channel_id)
            )
        ):
            return PersonalFeedConversationResult("ignored", "inactive_binding")
        decision = self.user_ban_service.check_global_discord_user(author_id)
        if decision.banned:
            return PersonalFeedConversationResult("rejected", "user_banned")
        user = self.database.users.get_user_by_discord_user_id(author_id)
        if user is None:
            if notice is not None:
                try:
                    await notice()
                except Exception:
                    # A registration notice is best-effort. Its physical send
                    # cannot convert a rejected reply into a canonical Create.
                    logger.exception('Could not deliver registration notice for %s', message_id)
            return PersonalFeedConversationResult("rejected", "not_registered")
        if not body.strip():
            return PersonalFeedConversationResult("ignored", "empty_content")
        operation_id = sha256(
            f"personal-feed-reply:{guild_id}:{thread_id}:{message_id}:{user.id}".encode()
        ).hexdigest()
        username = str(user.activitypub_username)
        # Replies share the same typed User Actor identity as root posts.
        object_id = build_user_comment_object_id(
            origin=self.fedify_origin, username=username, object_id=operation_id
        )
        activity_id = build_user_create_comment_activity_id(
            origin=self.fedify_origin, username=username, activity_id=operation_id
        )
        created_at = created_at.astimezone(timezone.utc)
        published_at = created_at.isoformat(timespec="milliseconds").replace(
            "+00:00", "Z"
        )
        parent_object_id = root.object_id
        if reference_id is not None:
            mapped = self.database.message_mappings.get_message_mapping_by_discord_message_id(
                int(reference_id)
            )
            if mapped is not None and mapped.publication_context == 'personal_feed':
                # A Discord reply ID is not proof that its ActivityPub object
                # belongs to this conversation. Validate the stored canonical
                # root before adopting a nested parent from a generic mapping.
                context = self.database.personal_feed_conversations.resolve_conversation_object(
                    mapped.object_id
                )
                if (context is not None and context[0] == 'personal_feed'
                    and context[1] == root.object_id):
                    parent_object_id = mapped.object_id
        existing = inspect_publish_artifacts(
            self.database,
            source_id=str(message_id),
            actor_url=user.actor_url,
            publication_context="personal_feed",
            community_actor_url=None,
            discord_guild_id=guild_id,
            discord_channel_id=root.discord_channel_id,
            discord_message_id=message_id,
            activity_id=activity_id,
            object_id=object_id,
            published_at=created_at,
            kind="comment",
            in_reply_to_object_id=parent_object_id,
            conversation_root_object_id=root.object_id,
        )
        if existing is not None:
            return PersonalFeedConversationResult(
                "already_published", "duplicate", object_id
            )
        direct_target_inbox_urls: list[str] = []
        parent_actor_id: str | None = None
        parent_inbound = self.database.personal_feed_conversations.get_inbound_object(
            parent_object_id
        )
        if parent_inbound is not None:
            parent_actor_id = parent_inbound.actor_id
        if parent_actor_id is not None:
            remote_actor = self.database.remote_actors.get_remote_actor_by_actor_url(
                parent_actor_id
            )
            if remote_actor is not None:
                direct_target_inbox_urls.append(
                    remote_actor.shared_inbox_url or remote_actor.inbox_url
                )
        rendered = await self.fedify_gateway.render_personal_feed(
            RenderPersonalFeedRequest(
                actor_username=user.activitypub_username,
                operation_id=operation_id,
                activity_id=activity_id,
                object_id=object_id,
                body_markdown=body,
                published_at=published_at,
                kind="comment",
                in_reply_to_object_id=parent_object_id,
                conversation_root_object_id=root.object_id,
            )
        )
        persisted = persist_publish_artifacts(
            self.database,
            source_id=str(message_id),
            actor_username=user.activitypub_username,
            actor_url=user.actor_url,
            publication_context="personal_feed",
            community_actor_url=None,
            activity_id=activity_id,
            object_id=object_id,
            activity_json=rendered.activity_json,
            kind="comment",
            title=None,
            body_markdown=body,
            in_reply_to_object_id=parent_object_id,
            conversation_root_object_id=root.object_id,
            discord_guild_id=guild_id,
            discord_channel_id=root.discord_channel_id,
            discord_message_id=message_id,
            published_at=created_at,
            direct_target_inbox_urls=direct_target_inbox_urls,
        )
        if persisted.created and self.delivery_worker is not None:
            self.delivery_worker.wake()
        return PersonalFeedConversationResult("published", "published", object_id)
