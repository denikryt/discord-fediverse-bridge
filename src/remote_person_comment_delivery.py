"""Durable bot-authored comment delivery into retained Remote Person threads.

The canonical Note and its destination-specific Discord identity remain
separate. A known nested parent is never silently flattened to a root reply.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import or_, select

from .models import (InboundConversationObject, MessageMapping, PublishedActivityObject,
                     RemotePersonCommentPlacement, RemotePersonPost,
                     RemotePersonPostDelivery, utcnow)
from .remote_person_feeds import RemotePersonChannelService

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CommentDispatch:
    """Copy-local immutable claim before any Discord side effect."""

    placement_id: int
    claim_token: str
    root_id: int
    guild_id: int
    channel_id: int
    thread_id: int
    root_actor_id: str
    object_id: str
    body: str
    actor_name: str
    parent_message_id: int | None
    revision: str


class BotCommentTransport:
    """Use discord.py only at the Discord boundary, injectable for tests."""

    def __init__(self, bot: object) -> None:
        """Retain the already authenticated bot client."""
        self.bot = bot

    async def send(self, claim: CommentDispatch) -> int:
        """Submit a bot message, with native Reply only when parent is known."""
        import discord
        thread = self.bot.get_channel(claim.thread_id) or await self.bot.fetch_channel(claim.thread_id)
        if thread.guild.id != claim.guild_id:
            raise PermissionError('thread belongs to an unexpected guild')
        perms = thread.permissions_for(thread.guild.me)
        if not (perms.view_channel and perms.read_message_history and perms.send_messages_in_threads):
            raise PermissionError('missing effective thread permissions')
        options = {'allowed_mentions': discord.AllowedMentions.none(), 'suppress_embeds': True}
        if claim.parent_message_id is not None:
            options['reference'] = discord.MessageReference(
                message_id=claim.parent_message_id,
                channel_id=claim.thread_id,
                fail_if_not_exists=True,
            )
        message = await thread.send(
            f'**{claim.actor_name}:** {claim.body}\n<{claim.object_id}>', **options,
        )
        return int(message.id)

    async def edit(self, thread_id: int, message_id: int, content: str) -> None:
        """Change only the bot's existing comment without creating a new message."""
        import discord
        thread = self.bot.get_channel(thread_id) or await self.bot.fetch_channel(thread_id)
        permissions = thread.permissions_for(thread.guild.me)
        if not (permissions.view_channel and permissions.read_message_history):
            raise PermissionError('cannot fetch existing bot comment for edit')
        message = await thread.fetch_message(message_id)
        if message.author.id != self.bot.user.id:
            raise PermissionError('cannot mutate a message from another Discord author')
        await message.edit(content=content, allowed_mentions=discord.AllowedMentions.none())

    async def delete(self, thread_id: int, message_id: int) -> None:
        """Delete only the original bot-owned comment, never a user's source."""
        thread = self.bot.get_channel(thread_id) or await self.bot.fetch_channel(thread_id)
        permissions = thread.permissions_for(thread.guild.me)
        if not (permissions.view_channel and permissions.read_message_history):
            raise PermissionError('cannot fetch existing bot comment for deletion')
        message = await thread.fetch_message(message_id)
        if message.author.id != self.bot.user.id:
            raise PermissionError('cannot delete a message from another Discord author')
        await message.delete()

    async def find_uncertain(self, claim: CommentDispatch) -> int | None:
        """Identify a possible prior send by its canonical AP URL, without repost."""
        thread = self.bot.get_channel(claim.thread_id) or await self.bot.fetch_channel(claim.thread_id)
        perms = thread.permissions_for(thread.guild.me)
        if not (perms.view_channel and perms.read_message_history):
            raise PermissionError('cannot reconcile without message history')
        async for message in thread.history(limit=100):
            if getattr(message.author, 'id', None) == getattr(self.bot.user, 'id', None) \
                    and f'<{claim.object_id}>' in message.content:
                return int(message.id)
        return None


class RemotePersonCommentDeliveryWorker:
    """Share the root scheduler and enforce per-placement sender eligibility."""

    MAX_ATTEMPTS = 3

    def __init__(self, *, database: object, bot: object, policy_service: object,
                 transport: object | None = None) -> None:
        """Inject SQLite ownership and a single testable Discord delivery edge."""
        self.database = database
        self.bot = bot
        self.policy_service = policy_service
        self.transport = transport or BotCommentTransport(bot)

    def recover(self) -> None:
        """Restore safe claims and quarantine network-started sends after restart."""
        with self.database.immediate_session() as session:
            for row in session.scalars(select(RemotePersonCommentPlacement).where(
                RemotePersonCommentPlacement.state.in_(('claimed', 'sending')),
            )):
                row.state = 'pending' if row.state == 'claimed' else 'uncertain'
                row.last_error = 'recovered interrupted Discord comment delivery'
                row.claim_token = None

    def _policy_ok(self, guild_id: int, actor_id: str) -> bool:
        """Recheck the original remote root actor and Discord guild policy."""
        snapshot = self.policy_service.snapshot()
        return (snapshot.is_discord_guild_allowed(guild_id)
                and snapshot.federation_decision(actor_id).allowed)

    @staticmethod
    def _parent_message(session: object, root: RemotePersonPost, copy: RemotePersonPostDelivery,
                        parent_id: str) -> tuple[bool, int | None]:
        """Return the parent message only when it belongs to this exact thread."""
        if parent_id == root.object_id:
            return True, None
        inbound = session.scalar(select(InboundConversationObject).where(
            InboundConversationObject.object_id == parent_id,
            InboundConversationObject.publication_context == 'remote_person_feed',
        ))
        if inbound is not None:
            placement = session.scalar(select(RemotePersonCommentPlacement).where(
                RemotePersonCommentPlacement.root_delivery_id == copy.id,
                RemotePersonCommentPlacement.inbound_object_id == inbound.id,
                RemotePersonCommentPlacement.state == 'delivered',
            ))
            return placement is not None, placement.discord_message_id if placement else None
        published = session.scalar(select(PublishedActivityObject).where(
            PublishedActivityObject.object_id == parent_id,
            PublishedActivityObject.publication_context == 'remote_person_feed',
        ))
        if published is not None:
            original = session.scalar(select(MessageMapping).where(
                MessageMapping.object_id == parent_id,
                MessageMapping.discord_channel_id == copy.discord_thread_id,
            ))
            if original is not None:
                return True, original.discord_message_id
            placement = session.scalar(select(RemotePersonCommentPlacement).where(
                RemotePersonCommentPlacement.root_delivery_id == copy.id,
                RemotePersonCommentPlacement.published_object_id == published.id,
                RemotePersonCommentPlacement.state == 'delivered',
            ))
            return placement is not None, placement.discord_message_id if placement else None
        return False, None

    def _claim(self) -> CommentDispatch | None:
        """Atomically claim only parent-resolved, accepted destination work."""
        with self.database.immediate_session() as session:
            now = utcnow()
            rows = session.scalars(select(RemotePersonCommentPlacement).where(
                RemotePersonCommentPlacement.state.in_(('pending', 'pending_parent', 'pending_thread')),
                or_(RemotePersonCommentPlacement.next_attempt_at.is_(None),
                    RemotePersonCommentPlacement.next_attempt_at <= now),
            ).order_by(RemotePersonCommentPlacement.id).limit(80)).all()
            for row in rows:
                copy = session.get(RemotePersonPostDelivery, row.root_delivery_id)
                if copy is None:
                    continue
                root = session.get(RemotePersonPost, copy.post_id)
                if root is None:
                    continue
                canonical = session.get(InboundConversationObject, row.inbound_object_id) \
                    if row.inbound_object_id is not None else session.get(PublishedActivityObject, row.published_object_id)
                if canonical is None:
                    row.state = 'failed'
                    row.last_error = 'canonical comment missing'
                    continue
                if canonical.deleted_at is not None or root.deleted_at is not None:
                    row.state = 'suppressed'
                    row.last_error = 'source deleted before Discord delivery'
                    continue
                if (not self._policy_ok(copy.discord_guild_id, root.remote_actor_id)
                        or not RemotePersonChannelService._active_owner_exists(
                            session, root.remote_actor_id, copy.discord_guild_id, copy.discord_channel_id)):
                    row.state = 'suppressed'
                    row.last_error = 'no accepted channel assignment'
                    continue
                if copy.state != 'delivered' or copy.discord_thread_id is None:
                    row.state = 'pending_thread'
                    continue
                parent_id = canonical.parent_object_id if isinstance(canonical, InboundConversationObject) \
                    else canonical.in_reply_to_object_id
                ready, parent_message = self._parent_message(session, root, copy, parent_id)
                if not ready:
                    row.state = 'pending_parent'
                    continue
                row.state = 'claimed'
                row.attempt_count += 1
                row.claim_token = uuid4().hex
                row.destination_thread_id = copy.discord_thread_id
                row.next_attempt_at = None
                return CommentDispatch(
                    placement_id=row.id, claim_token=row.claim_token,
                    root_id=copy.id, guild_id=copy.discord_guild_id,
                    channel_id=copy.discord_channel_id, thread_id=copy.discord_thread_id,
                    root_actor_id=root.remote_actor_id,
                    object_id=canonical.object_id,
                    body=(getattr(canonical, 'content_markdown', None) or getattr(canonical, 'body_markdown', None) or ''),
                    actor_name=getattr(canonical, 'actor_name', None) or getattr(canonical, 'actor_username', None) or 'Remote user',
                    parent_message_id=parent_message,
                    revision=(canonical.revision_fingerprint if isinstance(canonical, InboundConversationObject)
                              else f'published:{canonical.content_revision}'),
                )
        return None

    def _authorize(self, claim: CommentDispatch) -> bool:
        """Linearize side-effect permission at the claimed→sending transition."""
        with self.database.immediate_session() as session:
            row = session.get(RemotePersonCommentPlacement, claim.placement_id)
            copy = session.get(RemotePersonPostDelivery, claim.root_id)
            if row is None or copy is None or row.claim_token != claim.claim_token or row.state != 'claimed':
                return False
            if (not self._policy_ok(claim.guild_id, claim.root_actor_id)
                    or not RemotePersonChannelService._active_owner_exists(
                        session, claim.root_actor_id, claim.guild_id, claim.channel_id)):
                row.state = 'suppressed'
                row.claim_token = None
                row.last_error = 'assignment removed before Discord send authorization'
                return False
            row.state = 'sending'
            return True

    def _complete(self, claim: CommentDispatch, message_id: int) -> None:
        """Commit Discord ID after confirmed send to prevent duplicate creates."""
        with self.database.immediate_session() as session:
            row = session.get(RemotePersonCommentPlacement, claim.placement_id)
            if row is not None and row.state in ('sending', 'uncertain'):
                row.discord_message_id = int(message_id)
                row.last_applied_revision = claim.revision
                row.state = 'delivered'
                row.claim_token = None
                row.last_error = None

    def _failed(self, claim: CommentDispatch, error: Exception) -> None:
        """Fail missing parent closed; quarantine ambiguous network results."""
        with self.database.immediate_session() as session:
            row = session.get(RemotePersonCommentPlacement, claim.placement_id)
            if row is None or row.claim_token != claim.claim_token or row.state != 'sending':
                return
            detail = str(error).lower()
            if isinstance(error, (PermissionError, LookupError)) or 'unknown message' in detail:
                row.state = 'failed' if row.attempt_count >= self.MAX_ATTEMPTS else 'pending_parent'
                row.next_attempt_at = utcnow() + timedelta(seconds=20 * row.attempt_count)
            else:
                row.state = 'uncertain'
            row.claim_token = None
            row.last_error = str(error)[:500]

    async def _send(self, claim: CommentDispatch) -> None:
        """Send one already-authorized bot comment, preserving exact ancestry."""
        if not self._authorize(claim):
            return
        try:
            message_id = await self.transport.send(claim)
        except asyncio.CancelledError:
            self._failed(claim, RuntimeError('cancelled after network side effect may have started'))
            raise
        except Exception as exc:
            logger.warning('Remote Person comment send unresolved placement=%s', claim.placement_id)
            self._failed(claim, exc)
            return
        self._complete(claim, message_id)

    async def _mutate_retained(self) -> None:
        """Apply authenticated canonical revisions to all delivered Discord copies."""
        with self.database.session() as session:
            pending = []
            for row in session.scalars(select(RemotePersonCommentPlacement).where(
                RemotePersonCommentPlacement.state == 'delivered',
                RemotePersonCommentPlacement.discord_message_id.is_not(None),
            ).order_by(RemotePersonCommentPlacement.id)):
                copy = session.get(RemotePersonPostDelivery, row.root_delivery_id)
                canonical = session.get(InboundConversationObject, row.inbound_object_id) \
                    if row.inbound_object_id is not None else session.get(PublishedActivityObject, row.published_object_id)
                if canonical is None or copy is None or row.destination_thread_id is None:
                    continue
                revision = (canonical.revision_fingerprint if isinstance(canonical, InboundConversationObject)
                            else f'published:{canonical.content_revision}')
                desired = 'deleted' if canonical.deleted_at is not None else revision
                if row.last_applied_revision == desired:
                    continue
                name = getattr(canonical, 'actor_name', None) or getattr(canonical, 'actor_username', None) or 'Remote user'
                content = getattr(canonical, 'content_markdown', None) or getattr(canonical, 'body_markdown', None) or ''
                pending.append((row.id, row.destination_thread_id, row.discord_message_id,
                                desired, f'**{name}:** {content}\n<{canonical.object_id}>'))
        for placement_id, thread_id, message_id, revision, content in pending[:40]:
            try:
                if revision == 'deleted':
                    await self.transport.delete(thread_id, message_id)
                else:
                    await self.transport.edit(thread_id, message_id, content)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                with self.database.immediate_session() as session:
                    row = session.get(RemotePersonCommentPlacement, placement_id)
                    if row is not None and row.state == 'delivered':
                        row.last_error = ('comment mutation failed: ' + str(exc))[:500]
                continue
            with self.database.immediate_session() as session:
                row = session.get(RemotePersonCommentPlacement, placement_id)
                if row is not None and row.state == 'delivered':
                    row.last_applied_revision = revision
                    row.last_error = None

    async def drain_once(self) -> None:
        """Progress waiting parents and dispatch bounded independent destinations."""
        for _ in range(20):
            claim = self._claim()
            if claim is None:
                break
            await self._send(claim)
        # Unknown sends are reconciled from Discord history; never repost them.
        with self.database.session() as session:
            uncertain = [row.id for row in session.scalars(select(RemotePersonCommentPlacement).where(
                RemotePersonCommentPlacement.state == 'uncertain',
            ).order_by(RemotePersonCommentPlacement.id).limit(20))]
        for placement_id in uncertain:
            with self.database.session() as session:
                row = session.get(RemotePersonCommentPlacement, placement_id)
                if row is None or row.destination_thread_id is None:
                    continue
                copy = session.get(RemotePersonPostDelivery, row.root_delivery_id)
                root = session.get(RemotePersonPost, copy.post_id)
                canonical = session.get(InboundConversationObject, row.inbound_object_id) \
                    if row.inbound_object_id is not None else session.get(PublishedActivityObject, row.published_object_id)
                if canonical is None:
                    continue
                claim = CommentDispatch(placement_id=row.id, claim_token='', root_id=copy.id,
                    guild_id=copy.discord_guild_id, channel_id=copy.discord_channel_id,
                    thread_id=row.destination_thread_id, root_actor_id=root.remote_actor_id,
                    object_id=canonical.object_id, body=(getattr(canonical, 'content_markdown', None) or getattr(canonical, 'body_markdown', None) or ''),
                    actor_name=getattr(canonical, 'actor_name', None) or getattr(canonical, 'actor_username', None) or 'Remote user',
                    parent_message_id=None, revision=canonical.revision_fingerprint)
            try:
                found = await self.transport.find_uncertain(claim)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning('Remote Person comment reconciliation failed placement=%s', placement_id)
                continue
            if found is not None:
                self._complete(claim, found)
        await self._mutate_retained()
