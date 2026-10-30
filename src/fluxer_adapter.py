"""Fail-closed Fluxer physical lookups over the authenticated private RPC.

This read-only owner must not be registered as a writable PlatformAdapterPort:
real bot writes, receipts and durable dispatch permits are not yet implemented.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .fluxer_rpc_client import FluxerRpcClient
from .platform_types import (
    AuthorRef, ChannelRef, ChannelView, ConnectorRef, ContentDocument,
    CreateAttempt, MessageRef, MessageView, PlatformAction, PlatformCapabilities,
    PlatformOperationError, PermissionResult, ThreadRef, ThreadView,
)


class FluxerAdapter:
    """Read one exact Fluxer physical object without granting AP ownership."""

    _ACTIONS = frozenset({
        'view_channel', 'read_history', 'create_forum_post',
        'start_message_thread', 'send_message', 'edit_message', 'delete_message',
    })
    _KINDS = frozenset({'forum', 'text', 'announcement', 'unknown'})

    def __init__(self, *, connector_key: str, instance_origin: str,
                 rpc: FluxerRpcClient, bot_actor_id: str | None = None) -> None:
        """Bind an immutable connector; bot identity is optional until login."""
        self._connector = ConnectorRef(connector_key, 'fluxer', instance_origin)
        self._rpc = rpc
        self._bot_id = bot_actor_id

    @property
    def connector(self) -> ConnectorRef:
        """Return the immutable and nonsecret physical connector identity."""
        return self._connector

    def _scope(self, key: str) -> None:
        """Refuse wrong instances before any network call."""
        if key != self._connector.key:
            raise PlatformOperationError('invalid_request', 'Fluxer physical connector mismatch')

    async def _call(self, operation: str, args: dict[str, Any]) -> dict[str, Any]:
        """Route a fixed SDK-neutral method through the verified RPC envelope."""
        return await self._rpc.call(operation, args, connector_key=self._connector.key)

    @staticmethod
    def _id(data: dict[str, Any], field: str) -> str:
        """Require an actual opaque identifier, never a fabricated numeric alias."""
        value = data.get(field)
        if not isinstance(value, str) or not value or not value.strip():
            raise PlatformOperationError('invalid_request', 'Fluxer RPC physical identity missing')
        return value

    @staticmethod
    def _timestamp(value: object) -> datetime | None:
        """Preserve UTC timestamps only when the provider supplies a valid instant."""
        if value is None:
            return None
        if not isinstance(value, str):
            raise PlatformOperationError('invalid_request', 'Fluxer RPC timestamp malformed')
        try:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError as exc:
            raise PlatformOperationError('invalid_request', 'Fluxer RPC timestamp malformed') from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise PlatformOperationError('invalid_request', 'Fluxer RPC timestamp lacks timezone')
        return parsed

    async def fetch_channel(self, channel: ChannelRef) -> ChannelView:
        """Read exact channel kind/guild, never infer a forum from a hint."""
        self._scope(channel.connector_key)
        result = await self._call('fetch_channel', {'id': channel.external_channel_id})
        if self._id(result, 'id') != channel.external_channel_id:
            raise PlatformOperationError('invalid_request', 'Fluxer channel mismatch')
        kind = result.get('kind')
        if kind not in self._KINDS or (
            channel.kind != 'unknown' and channel.kind != kind
        ):
            raise PlatformOperationError('invalid_request', 'Fluxer channel type mismatch')
        guild = result.get('guild_id')
        if guild is not None and (not isinstance(guild, str) or not guild):
            raise PlatformOperationError('invalid_request', 'Fluxer channel guild malformed')
        if channel.external_guild_id is not None and guild != channel.external_guild_id:
            raise PlatformOperationError('invalid_request', 'Fluxer channel guild mismatch')
        name = result.get('name')
        if name is not None and not isinstance(name, str):
            raise PlatformOperationError('invalid_request', 'Fluxer channel name malformed')
        return ChannelView(ChannelRef(self._connector.key, self._id(result, 'id'),
                                      kind, guild), name)

    async def fetch_thread(self, thread: ThreadRef) -> ThreadView:
        """Read exact forum parent and starter as two separate identities."""
        self._scope(thread.connector_key)
        result = await self._call('fetch_thread', {'id': thread.external_thread_id})
        if (self._id(result, 'id') != thread.external_thread_id
            or self._id(result, 'parent_id') != thread.channel.external_channel_id
            or result.get('kind') not in ('forum', 'message')):
            raise PlatformOperationError('invalid_request', 'Fluxer thread parent/type mismatch')
        if thread.channel.kind == 'forum' and result['kind'] != 'forum':
            raise PlatformOperationError('invalid_request', 'Fluxer forum thread mismatch')
        guild = result.get('guild_id')
        if thread.channel.external_guild_id is not None and guild != thread.channel.external_guild_id:
            raise PlatformOperationError('invalid_request', 'Fluxer thread guild mismatch')
        if guild is not None and (not isinstance(guild, str) or not guild):
            raise PlatformOperationError('invalid_request', 'Fluxer thread guild malformed')
        starter = result.get('starter_id')
        if starter is not None and (not isinstance(starter, str) or not starter):
            raise PlatformOperationError('invalid_request', 'Fluxer thread starter malformed')
        name = result.get('name')
        if name is not None and not isinstance(name, str):
            raise PlatformOperationError('invalid_request', 'Fluxer thread name malformed')
        physical_parent = ChannelRef(self._connector.key, thread.channel.external_channel_id,
                                     thread.channel.kind, guild)
        confirmed = ThreadRef(physical_parent, thread.external_thread_id)
        starter_ref = (MessageRef(self._connector.key, thread.external_thread_id, starter)
                       if starter is not None else None)
        return ThreadView(confirmed, name, starter_ref, result['kind'])

    async def fetch_message(self, message: MessageRef) -> MessageView:
        """Never accept a message from another immediate container."""
        self._scope(message.connector_key)
        result = await self._call('fetch_message', {
            'id': message.external_message_id,
            'container_id': message.external_container_id,
        })
        if (self._id(result, 'id') != message.external_message_id
            or self._id(result, 'container_id') != message.external_container_id):
            raise PlatformOperationError('invalid_request', 'Fluxer message container mismatch')
        if result.get('attachments'):
            # Do not silently turn an attachment-bearing read into text-only data.
            raise PlatformOperationError('unsupported', 'Fluxer attachments not yet verified')
        author_data = result.get('author')
        author = None
        if author_data is not None:
            if not isinstance(author_data, dict) or author_data.get('kind') not in (
                'human', 'bot', 'webhook', 'unknown',
            ):
                raise PlatformOperationError('invalid_request', 'Fluxer message author malformed')
            try:
                author = AuthorRef(
                    self._connector.key, author_data.get('id'), author_data['kind'],
                    author_data.get('display_name', ''),
                )
            except ValueError as exc:
                raise PlatformOperationError('invalid_request', 'Fluxer message author malformed') from exc
        raw_content = result.get('content')
        if raw_content is not None and not isinstance(raw_content, str):
            raise PlatformOperationError('invalid_request', 'Fluxer message body malformed')
        content = ContentDocument(raw_content) if raw_content is not None else None
        return MessageView(
            message, author, content, self._timestamp(result.get('edited_at')),
            self._timestamp(result.get('created_at')),
        )

    async def capabilities(self, channel: ChannelRef) -> PlatformCapabilities:
        """Expose verified SDK flags, never turn unknown support into True."""
        self._scope(channel.connector_key)
        info = await self._call('capabilities', {'channel_id': channel.external_channel_id})
        attrs = ('forum_posts', 'message_threads', 'thread_replies', 'attachments',
                 'message_edit_delete')
        verified_scope = info.get('channel_id') == channel.external_channel_id and (
            channel.external_guild_id is None or info.get('guild_id') == channel.external_guild_id
        )
        flags = {name: verified_scope and info.get(name) is True for name in attrs}
        # Bot-only transport explicitly refuses requested webhook mode in #12.
        return PlatformCapabilities(**flags, webhook_send=False, webhook_edit_delete=False)

    async def check_permissions(
        self, principal: AuthorRef, channel: ChannelRef,
        actions: frozenset[PlatformAction],
    ) -> dict[PlatformAction, PermissionResult]:
        """Fail closed without an attested logged-in bot or current channel ACL."""
        self._scope(channel.connector_key)
        self._scope(principal.connector_key)
        if not isinstance(actions, frozenset) or actions - self._ACTIONS:
            raise PlatformOperationError('invalid_request', 'invalid Fluxer permission actions')
        if (principal.kind != 'bot' or self._bot_id is None
            or principal.external_actor_id != self._bot_id):
            return {action: 'unknown' for action in actions}
        info = await self._call('check_permissions', {
            'bot_id': self._bot_id, 'channel_id': channel.external_channel_id,
            'actions': sorted(actions),
        })
        if (info.get('bot_id') != self._bot_id
            or info.get('channel_id') != channel.external_channel_id
            or (channel.external_guild_id is not None
                and info.get('guild_id') != channel.external_guild_id)):
            return {action: 'unknown' for action in actions}
        return {action: (info[action] if info.get(action) in (
            'allowed', 'denied', 'unknown', 'unsupported',
        ) else 'unknown') for action in actions}

    async def reconcile_create(self, attempt: CreateAttempt) -> None:
        """Without a server-attested attempt receipt, never adopt a title match."""
        self._scope(attempt.connector_key)
        return None
