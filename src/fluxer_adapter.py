"""Scoped Fluxer reads and bot-only Create attempts over authenticated RPC.

The adapter authenticates persistent Create claims without deciding canonical AP
ownership; the Node transport owns Fluxer SDK details and physical HTTP writes.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any
from weakref import WeakKeyDictionary, WeakSet

from .fluxer_rpc_client import FluxerRpcClient
from .platform_types import (
    AuthorRef, ChannelRef, ChannelView, ConnectorRef, ContentDocument,
    CreateAttempt, MessageRef, MessageView, PlatformAction, PlatformCapabilities,
    PlatformOperationError, PermissionResult, ThreadRef, ThreadView,
    CreatedThread, CreatedMessage, DeliveryPresentation,
    CoordinatorDispatchPermit, CommittedCreateDispatchProof, CommittedCreateAckProof,
    validate_create_attempt, validate_created_receipt, validate_message_reply,
    _verify_committed_create_dispatch_proof, _verify_committed_create_ack_proof,
    _issue_verified_create_reconciliation_proof,
)


def _physical_identity(ref: object) -> object:
    """Include kind/guild in immutable Create signatures, unlike ChannelRef equality."""
    if isinstance(ref, ChannelRef):
        return (ref.connector_key, ref.external_channel_id, ref.kind, ref.external_guild_id)
    if isinstance(ref, ThreadRef):
        return (_physical_identity(ref.channel), ref.external_thread_id)
    if isinstance(ref, MessageRef):
        return (ref.connector_key, ref.external_container_id, ref.external_message_id)
    return ref


class _FluxerCreateFlight:
    """Retain the exact attempted write and its result until durable SQLite ACK."""

    def __init__(self, signature: tuple, permit: CoordinatorDispatchPermit | None,
                 claim: tuple | None) -> None:
        """Allocate process-local singleflight state without authorizing a POST."""
        self.signature = signature
        self.permit = permit
        self.claim = claim
        self.future: asyncio.Future = asyncio.get_running_loop().create_future()


class FluxerAdapter:
    """Read one exact Fluxer physical object without granting AP ownership."""

    _ACTIONS = frozenset({
        'view_channel', 'read_history', 'create_forum_post',
        'start_message_thread', 'send_message', 'edit_message', 'delete_message',
    })
    _KINDS = frozenset({'forum', 'text', 'announcement', 'unknown'})

    def __init__(self, *, connector_key: str, instance_origin: str,
                 rpc: FluxerRpcClient, bot_actor_id: str | None = None,
                 forum_applied_tags: tuple[tuple[str, tuple[str, ...]], ...] = ()) -> None:
        """Bind an immutable connector; bot identity is optional until login."""
        self._connector = ConnectorRef(connector_key, 'fluxer', instance_origin)
        self._rpc = rpc
        self._bot_id = bot_actor_id
        # Config parsing fixes this exact selection before a CreateAttempt can enter.
        self._forum_applied_tags = dict(forum_applied_tags)
        self._create_lock = asyncio.Lock()
        self._creates: dict[tuple[str, str], _FluxerCreateFlight] = {}
        self._issued_create_permits: WeakKeyDictionary[CoordinatorDispatchPermit, tuple] = WeakKeyDictionary()
        self._used_dispatch_proofs: WeakSet[CommittedCreateDispatchProof] = WeakSet()
        self._active: set[asyncio.Task] = set()
        self._admission_open = True
        # Optimistic RPC connection is not a READY SDK session. Recovery must
        # never durably claim on the strength of configured endpoints alone.
        self._dispatch_ready = False
        self._physical_dispatch_gate = None

    def bind_physical_dispatch_gate(self, gate) -> None:
        """Bind the process-owned slot to direct and scheduled Fluxer sends.

        Creating a coordinator from this adapter must not allocate a parallel
        capacity owner or bypass an existing sender waiting on the same slot.
        """
        if (self._physical_dispatch_gate is not None
            or self._connector.key not in gate.connector_keys):
            raise RuntimeError('Fluxer physical capacity already bound or wrong connector')
        self._physical_dispatch_gate = gate

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

    async def _attested_bot_id(self) -> str | None:
        """Check the *current* Node READY identity before claiming bot ownership.

        A caller-supplied constructor ID is only an expected identity, never a
        login proof. Once attested, a different bot after a sidecar restart
        cannot silently inherit this connector's mutation authority.
        """
        try:
            health = await self._call('health', {})
        except PlatformOperationError:
            return None
        current = health.get('bot_id')
        if (health.get('connector_key') != self._connector.key
            or health.get('instance_origin') != self._connector.instance_origin
            or health.get('state') != 'ready'
            or health.get('gateway_gap') is True
            or not isinstance(current, str) or not current
            or current != current.strip() or any(ord(ch) < 32 for ch in current)):
            return None
        if self._bot_id is not None and current != self._bot_id:
            return None
        if self._bot_id is None:
            self._bot_id = current
        return current

    def dispatch_ready(self) -> bool:
        """Expose last authenticated Node session readiness for pre-claim admission.

        The runtime scheduler refreshes this snapshot on the connector's own
        lane; a stopped sidecar cannot be mistaken for a configured sender.
        """
        return self._admission_open and self._dispatch_ready

    async def refresh_readiness(self) -> bool:
        """Poll authenticated SDK READY without blocking the Discord lane.

        A changing bot ID, disconnected Gateway or missing Node RPC refuses
        new durable claims. This is not proof of whether older writes ran.
        """
        self._dispatch_ready = False
        if not self._admission_open:
            return False
        self._dispatch_ready = await self._attested_bot_id() is not None
        return self._dispatch_ready

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
            # Only Node can attest the bot logged in on this connector. The
            # physical read must also explicitly confirm absent webhook origin;
            # an omitted provenance field cannot establish bot ownership.
            webhook_id = result.get('webhook_id')
            if webhook_id is not None and (not isinstance(webhook_id, str) or not webhook_id):
                raise PlatformOperationError('invalid_request', 'Fluxer webhook provenance malformed')
            bot_id = (await self._attested_bot_id()
                      if author_data['kind'] == 'bot' and 'webhook_id' in result
                      and webhook_id is None else None)
            try:
                author = AuthorRef(
                    self._connector.key, author_data.get('id'), author_data['kind'],
                    author_data.get('display_name', ''),
                    is_bridge_owned=bool(bot_id is not None
                        and author_data.get('id') == bot_id),
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
        current_bot_id = await self._attested_bot_id() if principal.kind == 'bot' else None
        if (current_bot_id is None or principal.external_actor_id != current_bot_id):
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

    @staticmethod
    def _validate_body(content: ContentDocument) -> None:
        """Refuse unknown rendering, media and notification side effects before POST."""
        if not isinstance(content, ContentDocument) or not content.body.strip():
            raise PlatformOperationError('invalid_request', 'Fluxer message body must not be empty')
        if content.attachments or content.format != 'plain':
            raise PlatformOperationError('unsupported', 'Fluxer rich content is not yet verified')
        if len(content.body.encode('utf-8')) > 8192:
            raise PlatformOperationError('unsupported', 'Fluxer body exceeds the verified RPC envelope limit')

    @staticmethod
    def _bot_mode(presentation: DeliveryPresentation) -> None:
        """Never silently degrade requested webhook identity to bot mode."""
        if not isinstance(presentation, DeliveryPresentation):
            raise PlatformOperationError('invalid_request', 'Fluxer presentation malformed')
        if presentation.mode != 'bot':
            raise PlatformOperationError('unsupported', 'Fluxer webhook transport not implemented')
        if presentation.display_name or presentation.avatar_url:
            raise PlatformOperationError('unsupported', 'Fluxer bot cannot impersonate presentation')

    async def _preflight(self, channel: ChannelRef, *, is_forum: bool,
                         action: PlatformAction) -> tuple[str, ...]:
        """Verify live channel/capability/bot ACL and return verified tag selection."""
        self._scope(channel.connector_key)
        if await self._attested_bot_id() is None:
            raise PlatformOperationError('unavailable', 'Fluxer bot identity not attested')
        verified = (await self.fetch_channel(channel)).channel
        if is_forum and verified.kind != 'forum':
            raise PlatformOperationError('unsupported', 'destination is not a verified Fluxer forum')
        if verified.external_guild_id is None:
            raise PlatformOperationError('forbidden', 'Fluxer channel guild not verified')
        flags = await self.capabilities(verified)
        applied_tags: tuple[str, ...] = ()
        if is_forum:
            if not flags.forum_posts:
                raise PlatformOperationError('unsupported', 'forum posts not verified on Fluxer build')
            # Required tags must be measured on this instance. A boolean forum
            # capability alone does not prove an untagged create will succeed.
            tags = await self._call('capabilities', {'channel_id': verified.external_channel_id})
            if (tags.get('channel_id') != verified.external_channel_id
                or tags.get('guild_id') != verified.external_guild_id
                or tags.get('tag_requirements_verified') is not True
                or type(tags.get('requires_tag')) is not bool
                or not isinstance(tags.get('available_tags'), list)):
                raise PlatformOperationError('unsupported', 'Fluxer forum tag requirements unverified')
            applied_tags = self._forum_applied_tags.get(verified.external_channel_id, ())
            available: dict[str, bool] = {}
            for item in tags['available_tags']:
                if (not isinstance(item, dict) or set(item) != {'id', 'moderated'}
                    or not isinstance(item['id'], str) or not item['id']
                    or type(item['moderated']) is not bool or item['id'] in available):
                    raise PlatformOperationError('unsupported', 'Fluxer forum tag inventory unverified')
                available[item['id']] = item['moderated']
            if tags['requires_tag'] and not applied_tags:
                raise PlatformOperationError('unsupported', 'Fluxer required applied tag not selected')
            if len(applied_tags) > 5 or len(set(applied_tags)) != len(applied_tags):
                raise PlatformOperationError('invalid_request', 'Fluxer applied tags invalid')
            if any(tag not in available or available[tag] for tag in applied_tags):
                # Moderated tags need independent moderator proof; no such grant
                # can be inferred from ordinary SendMessages permissions.
                raise PlatformOperationError('unsupported', 'Fluxer applied tag missing or restricted')
        if not is_forum and action == 'send_message' and verified.kind not in ('forum', 'text', 'announcement'):
            raise PlatformOperationError('unsupported', 'message destination kind not verified')
        principal = AuthorRef(self._connector.key, self._bot_id, 'bot', 'Fluxer bridge')
        actions = frozenset({'view_channel', action})
        grants = await self.check_permissions(principal, verified, actions)
        if any(grants.get(permission) != 'allowed' for permission in actions):
            raise PlatformOperationError('forbidden', 'Fluxer bot permission not verified')
        return applied_tags

    async def _preflight_thread_send(self, thread: ThreadRef) -> None:
        """Authorize sends in the thread, independently of parent forum SendMessages.

        Physical thread identity and guild membership are verified before ACL.
        The Node executor repeats fresh thread/parent reads and bit-38 checks
        immediately before the actual SDK write to close permission races.
        """
        self._scope(thread.connector_key)
        if await self._attested_bot_id() is None:
            raise PlatformOperationError('unavailable', 'Fluxer bot identity not attested')
        verified = (await self.fetch_thread(thread)).thread
        parent = (await self.fetch_channel(verified.channel)).channel
        guild = verified.channel.external_guild_id
        if (guild is None or parent.external_guild_id != guild
            or parent.kind not in ('forum', 'text', 'announcement')):
            raise PlatformOperationError('forbidden', 'Fluxer thread parent/guild not verified')
        # A ThreadRef stores the parent forum as channel; creating a scoped
        # ChannelRef for ACL is only an address for an already verified thread,
        # never a fabricated forum channel or permission inheritance shortcut.
        physical = ChannelRef(self._connector.key, verified.external_thread_id,
                              'unknown', guild)
        principal = AuthorRef(self._connector.key, self._bot_id, 'bot', 'Fluxer bridge')
        actions = frozenset({'view_channel', 'read_history', 'send_message'})
        grants = await self.check_permissions(principal, physical, actions)
        parent_grants = await self.check_permissions(
            principal, parent, frozenset({'view_channel'}),
        )
        if (any(grants.get(action) != 'allowed' for action in actions)
            or parent_grants.get('view_channel') != 'allowed'):
            raise PlatformOperationError('forbidden', 'Fluxer thread send permission not verified')
        # A current ACL grant is not sufficient to advertise a server-verified
        # operation. Check this exact thread after permission denial so a
        # revoked right stays `forbidden`, not `unsupported`.
        if not (await self.capabilities(physical)).thread_replies:
            raise PlatformOperationError('unsupported', 'Fluxer thread replies not verified')

    def _mint_coordinator_dispatch_permit(
        self, *, proof: CommittedCreateDispatchProof,
        intent_id: int, attempt: CreateAttempt, request_fingerprint: str,
        dispatch_token: str, operation: str,
        destination: ChannelRef | ThreadRef | MessageRef,
        content: ContentDocument | None, presentation: DeliveryPresentation | None,
        title: str | None, reply_to: MessageRef | None,
    ) -> CoordinatorDispatchPermit:
        """Require repository-signed committed claim before issuing one local POST permit."""
        validate_create_attempt(attempt, operation, destination)
        self._scope(attempt.connector_key)
        if (not _verify_committed_create_dispatch_proof(proof)
            or (proof.intent_id, proof.attempt_key, proof.request_fingerprint,
                proof.dispatch_token) != (intent_id, attempt.key, request_fingerprint,
                                          dispatch_token)
            or proof in self._used_dispatch_proofs or intent_id <= 0
            or not attempt.key.startswith('fanout:v1:')):
            raise PlatformOperationError('invalid_request', 'invalid Fluxer SQLite Create claim')
        signature = (attempt.connector_key, operation, _physical_identity(destination),
                     title, content, _physical_identity(reply_to), presentation)
        self._used_dispatch_proofs.add(proof)
        permit = object.__new__(CoordinatorDispatchPermit)
        self._issued_create_permits[permit] = (
            intent_id, attempt.key, request_fingerprint, dispatch_token, signature)
        return permit

    async def _create(
        self, *, attempt: CreateAttempt, operation: str,
        destination: ChannelRef | ThreadRef,
        content: ContentDocument, presentation: DeliveryPresentation,
        title: str | None, reply_to: MessageRef | None,
        dispatch_permit: CoordinatorDispatchPermit | None,
        perform,
    ) -> CreatedThread | CreatedMessage:
        """One immutable attempt performs at most one physical bot POST in this process."""
        validate_create_attempt(attempt, operation, destination)
        self._scope(attempt.connector_key)
        signature = (attempt.connector_key, operation, _physical_identity(destination),
                     title, content, _physical_identity(reply_to), presentation)
        claim = None
        if attempt.key.startswith('fanout:v1:'):
            # Raw CreateAttempt keys do not authorize durable fanout writes.
            issued = (self._issued_create_permits.get(dispatch_permit)
                      if isinstance(dispatch_permit, CoordinatorDispatchPermit) else None)
            if issued is None or issued[1] != attempt.key or issued[4] != signature:
                raise PlatformOperationError('invalid_request', 'Fluxer fanout Create requires current permit')
            claim = issued[:4]
            del self._issued_create_permits[dispatch_permit]
        elif dispatch_permit is not None:
            raise PlatformOperationError('invalid_request', 'direct Fluxer Create cannot borrow permit')
        async with self._create_lock:
            flight = self._creates.get((attempt.connector_key, attempt.key))
            if flight is not None:
                if flight.signature != signature or (claim is not None and flight.claim != claim):
                    raise PlatformOperationError('invalid_request', 'Fluxer Create attempt payload/claim changed')
                owner = False
            else:
                if not self._admission_open or len(self._creates) >= 100_000:
                    raise PlatformOperationError('unavailable', 'Fluxer Create admission closed or saturated')
                flight = _FluxerCreateFlight(signature, dispatch_permit, claim)
                self._creates[(attempt.connector_key, attempt.key)] = flight
                owner = True
        if not owner:
            result = await asyncio.shield(flight.future)
            if isinstance(result, PlatformOperationError):
                raise result
            return result
        task = asyncio.current_task()
        if task is not None:
            self._active.add(task)
        try:
            try:
                result = await perform()
            except asyncio.CancelledError:
                # The Node SDK may have completed the request after Python cancelled.
                flight.future.set_result(PlatformOperationError('unknown_delivery'))
                raise
            except PlatformOperationError as exc:
                if exc.reason in ('forbidden', 'not_found', 'unsupported', 'invalid_request',
                                  'rate_limited', 'unavailable'):
                    # Explicit pre-I/O refusals and authenticated definitive HTTP errors
                    # may be retried after conditions change, but never an unknown write.
                    if self._creates.get((attempt.connector_key, attempt.key)) is flight:
                        del self._creates[(attempt.connector_key, attempt.key)]
                flight.future.set_result(exc)
                raise
            except Exception as exc:
                unknown = PlatformOperationError('unknown_delivery', 'Fluxer create response unavailable')
                flight.future.set_result(unknown)
                raise unknown from exc
            try:
                validate_created_receipt(attempt, result, presentation=presentation)
            except Exception as exc:
                # POST already returned: a malformed/mismatched receipt is uncertainty,
                # not a definite rejection authorizing another remote POST.
                unknown = PlatformOperationError('unknown_delivery', 'Fluxer receipt not verified')
                flight.future.set_result(unknown)
                raise unknown from exc
            flight.future.set_result(result)
            return result
        finally:
            if task is not None:
                self._active.discard(task)

    async def create_forum_thread(
        self, forum: ChannelRef, content: ContentDocument, *, title: str,
        presentation: DeliveryPresentation, attempt: CreateAttempt,
        dispatch_permit: CoordinatorDispatchPermit | None = None,
    ) -> CreatedThread:
        """Create one bot forum thread and verify the separately observed physical IDs."""
        self._scope(forum.connector_key)
        validate_create_attempt(attempt, 'forum_thread', forum)
        self._bot_mode(presentation)
        self._validate_body(content)
        if not isinstance(title, str) or not title.strip():
            raise PlatformOperationError('invalid_request', 'Fluxer forum title required')

        async def perform() -> CreatedThread:
            """Verify rights before entering the potentially effectful Node SDK boundary."""
            applied_tags = await self._preflight(forum, is_forum=True, action='create_forum_post')
            response = await self._call('create_forum_thread', {
                'forum_id': forum.external_channel_id,
                'guild_id': forum.external_guild_id, 'title': title,
                'content': {'body': content.body, 'format': 'plain'},
                'allowed_mentions': {'parse': []}, 'attempt_key': attempt.key,
                'delivery_mode': 'bot', 'applied_tags': list(applied_tags),
            })
            try:
                if (self._id(response, 'forum_id') != forum.external_channel_id
                    or response.get('guild_id') != forum.external_guild_id
                    or response.get('delivery_mode') != 'bot'):
                    raise ValueError('wrong forum or mode')
                thread_id = self._id(response, 'thread_id')
                starter_id = self._id(response, 'starter_id')
                return CreatedThread(ThreadRef(forum, thread_id),
                                     MessageRef(self._connector.key, thread_id, starter_id), 'bot')
            except (ValueError, PlatformOperationError) as exc:
                raise PlatformOperationError('unknown_delivery', 'Fluxer forum receipt not verified') from exc

        return await self._create(attempt=attempt, operation='forum_thread',
                                  destination=forum, content=content,
                                  presentation=presentation, title=title, reply_to=None,
                                  dispatch_permit=dispatch_permit, perform=perform)

    async def send_message(
        self, destination: ThreadRef | ChannelRef, content: ContentDocument, *,
        reply_to: MessageRef | None, presentation: DeliveryPresentation,
        attempt: CreateAttempt, dispatch_permit: CoordinatorDispatchPermit | None = None,
    ) -> CreatedMessage:
        """Send one bot reply into its verified immediate thread/channel container."""
        self._scope(destination.connector_key)
        validate_create_attempt(attempt, 'message', destination)
        validate_message_reply(destination, reply_to)
        self._bot_mode(presentation)
        self._validate_body(content)

        async def perform() -> CreatedMessage:
            """Verify parent and bot ACL before issuing one physical RPC write."""
            if isinstance(destination, ThreadRef):
                await self.fetch_thread(destination)
                if reply_to is not None:
                    # The reply's immediate container is necessary but not
                    # sufficient: confirm its exact physical parent exists.
                    await self.fetch_message(reply_to)
                channel = destination.channel
                container_id = destination.external_thread_id
            else:
                channel = destination
                container_id = destination.external_channel_id
            if isinstance(destination, ThreadRef):
                await self._preflight_thread_send(destination)
            else:
                await self._preflight(channel, is_forum=False, action='send_message')
            response = await self._call('send_message', {
                'container_id': container_id, 'guild_id': channel.external_guild_id,
                'reply_to_message_id': reply_to.external_message_id if reply_to else None,
                'content': {'body': content.body, 'format': 'plain'},
                'allowed_mentions': {'parse': []}, 'attempt_key': attempt.key,
                'delivery_mode': 'bot',
            })
            try:
                if (self._id(response, 'container_id') != container_id
                    or response.get('delivery_mode') != 'bot'):
                    raise ValueError('wrong physical reply destination/mode')
                return CreatedMessage(MessageRef(self._connector.key, container_id,
                                                 self._id(response, 'message_id')), 'bot')
            except (ValueError, PlatformOperationError) as exc:
                raise PlatformOperationError('unknown_delivery', 'Fluxer message receipt not verified') from exc

        return await self._create(attempt=attempt, operation='message', destination=destination,
                                  content=content, presentation=presentation, title=None,
                                  reply_to=reply_to, dispatch_permit=dispatch_permit,
                                  perform=perform)

    async def _mutate_bot_message(
        self, operation: str, message: MessageRef,
        content: ContentDocument | None = None,
    ) -> None:
        """Recheck current physical author and rights before one scoped RPC mutation."""
        self._scope(message.connector_key)
        if not self._admission_open:
            raise PlatformOperationError('unavailable', 'Fluxer adapter is closing')
        if operation == 'edit_message':
            self._validate_body(content)
        elif operation != 'delete_message' or content is not None:
            raise PlatformOperationError('invalid_request', 'invalid Fluxer mutation payload')
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError('Fluxer mutation requires asyncio task')
        self._active.add(task)
        try:
            # Physical MessageRef establishes the immediate container. The
            # fresh provider read must attest bot authorship, not only ID scope.
            observed = await self.fetch_message(message)
            if (observed.author is None or observed.author.kind != 'bot'
                or not observed.author.is_bridge_owned
                or observed.author.external_actor_id != self._bot_id):
                raise PlatformOperationError('forbidden', 'Fluxer message is not bot-owned')
            channel = ChannelRef(self._connector.key, message.external_container_id)
            await self._preflight(channel, is_forum=False, action=operation)
            flags = await self.capabilities(channel)
            if not flags.message_edit_delete:
                raise PlatformOperationError('unsupported', 'Fluxer message mutation unavailable')
            args: dict[str, Any] = {
                'container_id': message.external_container_id,
                'message_id': message.external_message_id,
                'bot_id': self._bot_id,
            }
            if content is not None:
                args['content'] = {'body': content.body, 'format': 'plain'}
                args['allowed_mentions'] = {'parse': []}
            # This is the sole possibly side-effecting SDK boundary. A timeout
            # or cancellation cannot establish that an edit/delete was unsent;
            # the caller's existing SQLite mutation state owns that decision.
            response = await self._call(operation, args)
            try:
                if (self._id(response, 'container_id') != message.external_container_id
                    or self._id(response, 'message_id') != message.external_message_id):
                    raise ValueError('mutation response addresses another physical copy')
            except (PlatformOperationError, ValueError) as exc:
                raise PlatformOperationError('unknown_delivery', 'Fluxer mutation ACK not verified') from exc
        finally:
            self._active.discard(task)

    async def edit_message(self, message: MessageRef, content: ContentDocument) -> None:
        """Edit only the exact currently bot-owned physical message."""
        await self._mutate_bot_message('edit_message', message, content)

    async def delete_message(self, message: MessageRef) -> None:
        """Delete only the bot's physical message, never its surrounding thread."""
        await self._mutate_bot_message('delete_message', message)

    async def create_reply_thread(self, root: MessageRef, *, title: str,
                                  attempt: CreateAttempt,
                                  dispatch_permit: CoordinatorDispatchPermit | None = None) -> ThreadRef:
        """Refuse message-root threading until its exact SDK ownership rule is verified."""
        self._scope(root.connector_key)
        validate_create_attempt(attempt, 'reply_thread', root)
        raise PlatformOperationError('unsupported', 'Fluxer message-root thread semantics not verified')

    async def _reconcile_persisted_create(
        self, *, intent_id: int, attempt: CreateAttempt,
        request_fingerprint: str, dispatch_token: str,
    ):
        """Prove only an exact finished same-process, same-generation receipt."""
        self._scope(attempt.connector_key)
        async with self._create_lock:
            flight = self._creates.get((attempt.connector_key, attempt.key))
            if (flight is None or not flight.future.done() or flight.claim != (
                intent_id, attempt.key, request_fingerprint, dispatch_token)):
                return None
            receipt = flight.future.result()
            if not isinstance(receipt, (CreatedThread, CreatedMessage)):
                return None
            if (flight.signature[1] != attempt.operation or
                flight.signature[2] != _physical_identity(attempt.destination)):
                return None
            proof = _issue_verified_create_reconciliation_proof(
                intent_id, attempt.key, request_fingerprint, dispatch_token, receipt)
            return receipt, proof, flight.permit

    async def acknowledge_persisted_create(
        self, *, permit: CoordinatorDispatchPermit, proof: CommittedCreateAckProof,
        intent_id: int, attempt: CreateAttempt, request_fingerprint: str,
        dispatch_token: str, terminal_status: str,
        receipt: CreatedThread | CreatedMessage | None = None,
    ) -> bool:
        """Evict an exact terminal flight only after repository-signed SQLite ACK."""
        if (terminal_status not in ('delivered', 'unresolved')
            or not _verify_committed_create_ack_proof(proof)
            or (proof.intent_id, proof.attempt_key, proof.request_fingerprint,
                proof.dispatch_token, proof.terminal_status) != (
                intent_id, attempt.key, request_fingerprint, dispatch_token, terminal_status)):
            return False
        async with self._create_lock:
            key = (attempt.connector_key, attempt.key)
            flight = self._creates.get(key)
            if (flight is None or not flight.future.done() or flight.permit is not permit
                or flight.claim != (intent_id, attempt.key, request_fingerprint, dispatch_token)):
                return False
            value = flight.future.result()
            if terminal_status == 'unresolved':
                if not (isinstance(value, PlatformOperationError)
                        and value.reason == 'unknown_delivery' and receipt is None):
                    return False
            elif isinstance(value, PlatformOperationError) or receipt != value:
                return False
            del self._creates[key]
            return True

    async def reconcile_create(self, attempt: CreateAttempt):
        """Return only an already observed exact receipt; never search by title."""
        self._scope(attempt.connector_key)
        async with self._create_lock:
            flight = self._creates.get((attempt.connector_key, attempt.key))
            if flight is not None and flight.future.done() and (
                flight.signature[1] == attempt.operation
                and flight.signature[2] == _physical_identity(attempt.destination)):
                value = flight.future.result()
                if isinstance(value, (CreatedThread, CreatedMessage)):
                    return value
        return None

    async def quiesce(self) -> None:
        """Close Create admission and await all physical RPC calls before DB teardown."""
        self._admission_open = False
        self._dispatch_ready = False
        current = asyncio.current_task()
        tasks = tuple(task for task in self._active if task is not current)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
