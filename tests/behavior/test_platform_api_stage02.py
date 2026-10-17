"""Stage 02 adapter-contract scenarios against observed physical copies and claims."""

from dataclasses import dataclass, field

import pytest
from sqlalchemy import select

from src.db import Database
from src.db.repositories.platform_surfaces import PlatformSurfaceRepository
from src.models import ChannelSurface, PlatformConnector
from src.platform_adapters import PlatformAdapterRegistry
from src.platform_types import (
    AuthorRef, ChannelRef, ChannelView, ConnectorRef, ContentDocument,
    CreateAttempt, CreatedMessage, CreatedThread, DeliveryPresentation,
    MessageCreated, MessageRef, MessageView, PlatformCapabilities,
    PlatformOperationError, ThreadCreated, ThreadRef, ThreadView,
    validate_create_attempt, validate_created_receipt, validate_message_reply,
)
from src.runtime_ports import PlatformAdapterPort, PlatformInboundSinkPort


@dataclass
class PhysicalPlatform:
    """Stateful transport fake; it never writes bridge/AP records or caches mapping rows."""
    connector: ConnectorRef
    channels: dict[str, ChannelRef]
    capabilities_by_channel: dict[str, PlatformCapabilities] = field(default_factory=dict)
    receipts: dict[str, CreatedThread | ThreadRef | CreatedMessage] = field(default_factory=dict)
    requests: dict[str, tuple] = field(default_factory=dict)
    messages: dict[MessageRef, ContentDocument] = field(default_factory=dict)
    threads: dict[str, ThreadView] = field(default_factory=dict)
    creates: int = 0
    allow_proof: bool = True
    forbid_send: bool = False
    lose_response_keys: set[str] = field(default_factory=set)

    def _check(self, channel: ChannelRef) -> ChannelRef:
        """Read authoritative channel kind rather than trusting caller metadata."""
        if channel.connector_key != self.connector.key:
            raise PlatformOperationError('invalid_request')
        actual = self.channels.get(channel.external_channel_id)
        if actual is None:
            raise PlatformOperationError('not_found')
        if channel.kind != 'unknown' and actual.kind != channel.kind:
            raise PlatformOperationError('invalid_request')
        if channel.external_guild_id is not None and channel.external_guild_id != actual.external_guild_id:
            raise PlatformOperationError('invalid_request')
        return actual

    def _attempt(self, attempt: CreateAttempt, operation: str, destination, payload: tuple):
        """Ensure correlated attempts cannot be silently redirected or repurposed."""
        validate_create_attempt(attempt, operation, destination)
        previous = self.requests.get(attempt.key)
        if previous is not None and previous != (attempt, payload):
            raise PlatformOperationError('invalid_request')
        self.requests[attempt.key] = (attempt, payload)

    def _finish_create(self, attempt: CreateAttempt, result):
        """Simulate remote acceptance followed by lost HTTP response."""
        self.receipts[attempt.key] = result
        if attempt.key in self.lose_response_keys:
            raise PlatformOperationError('unknown_delivery')

    def _existing_reply_thread(self, root: MessageRef) -> ThreadRef | None:
        """Verify Discord's one-root-one-thread rule with actual parent metadata."""
        if self.connector.platform != 'discord':
            return None  # Fluxer uniqueness is deliberately not assumed.
        try:
            actual = self._check(ChannelRef(root.connector_key, root.external_container_id))
        except PlatformOperationError:
            return None
        if actual.kind not in ('text', 'announcement'):
            return None
        observed = self.threads.get(root.external_message_id)
        if observed is None or observed.kind != 'message':
            return None
        candidate = observed.thread
        try:
            verified_parent = self._check(candidate.channel)
        except PlatformOperationError:
            return None
        if (candidate.connector_key, candidate.external_thread_id, verified_parent.external_channel_id) != (
            root.connector_key, root.external_message_id, root.external_container_id
        ):
            return None
        return candidate

    async def fetch_channel(self, channel: ChannelRef) -> ChannelView:
        """Return actual channel metadata independent of its bridge ownership claim."""
        return ChannelView(self._check(channel), name='channel')

    async def fetch_thread(self, thread: ThreadRef) -> ThreadView:
        """Return only platform-observable thread metadata and starter ID."""
        self._check(thread.channel)
        if thread.channel.connector_key != self.connector.key:
            raise PlatformOperationError('invalid_request')
        try:
            observed = self.threads[thread.external_thread_id]
        except KeyError as exc:
            raise PlatformOperationError('not_found') from exc
        # A thread ID alone is insufficient: it must belong to the caller's
        # verified parent channel. Reuse the authoritative parent check here.
        actual_parent = self._check(observed.thread.channel)
        if actual_parent != thread.channel:
            raise PlatformOperationError('invalid_request', 'thread parent does not match destination')
        return observed

    async def fetch_message(self, message: MessageRef) -> MessageView:
        """Return real message content without consulting domain persistence."""
        if message.connector_key != self.connector.key:
            raise PlatformOperationError('invalid_request')
        if message not in self.messages:
            raise PlatformOperationError('not_found')
        return MessageView(message, content=self.messages[message])

    async def capabilities(self, channel: ChannelRef) -> PlatformCapabilities:
        """Feature availability is not authorization to send."""
        actual = self._check(channel)
        return self.capabilities_by_channel.get(actual.external_channel_id, PlatformCapabilities())

    async def check_permissions(self, principal, channel, actions):
        """Return unknown when the fake lacks a resolved member ACL."""
        self._check(channel)
        if principal.connector_key != self.connector.key:
            raise PlatformOperationError('invalid_request')
        return {action: 'unknown' for action in actions}

    async def create_forum_thread(self, forum, content, *, title, presentation, attempt):
        """Create forum post and its independently identified starter message."""
        self._attempt(attempt, 'forum_thread', forum, (content, title, presentation))
        actual = self._check(forum)
        if actual.kind != 'forum' or not (await self.capabilities(actual)).forum_posts:
            raise PlatformOperationError('unsupported')
        if presentation.mode == 'webhook' and not (await self.capabilities(actual)).webhook_send:
            raise PlatformOperationError('unsupported')
        if self.forbid_send:
            raise PlatformOperationError('forbidden')
        self.creates += 1
        # Use stable, distinct fake IDs per physical creation. Seeded threads
        # must not be overwritten by a later independent forum post either.
        thread_id = str(110 + self.creates)
        while thread_id in self.threads:
            thread_id = str(int(thread_id) + 1)
        starter_id = str(111 + int(thread_id))
        thread = ThreadRef(actual, thread_id)
        starter = MessageRef(self.connector.key, thread_id, starter_id)
        result = CreatedThread(thread, starter, presentation.mode)
        self.threads[thread.external_thread_id] = ThreadView(thread, name=title, starter=starter, kind='forum')
        self.messages[starter] = content
        validate_created_receipt(attempt, result, presentation=presentation)
        self._finish_create(attempt, result)
        return result

    async def create_reply_thread(self, root, *, title, attempt):
        """Adopt a verified root-anchored thread without repeating the create."""
        self._attempt(attempt, 'reply_thread', root, (title,))
        actual = self._check(ChannelRef(root.connector_key, root.external_container_id))
        if actual.kind not in ('text', 'announcement') or not (await self.capabilities(actual)).message_threads:
            raise PlatformOperationError('unsupported')
        if root not in self.messages:
            raise PlatformOperationError('not_found')
        existing = self.threads.get(root.external_message_id)
        if existing is not None:
            # An existing wrong-parent/kind thread is never adopted merely by ID.
            verified = self._existing_reply_thread(root)
            if verified is None:
                raise PlatformOperationError('invalid_request')
            self._finish_create(attempt, verified)
            return verified
        if self.forbid_send:
            raise PlatformOperationError('forbidden')
        self.creates += 1
        thread = ThreadRef(actual, root.external_message_id)
        self.threads[thread.external_thread_id] = ThreadView(thread, name=title, kind='message')
        self._finish_create(attempt, thread)
        return thread

    async def send_message(self, destination, content, *, reply_to, presentation, attempt):
        """Send to immediate container, rejecting cross-thread replies before I/O."""
        self._attempt(attempt, 'message', destination, (content, reply_to, presentation))
        validate_message_reply(destination, reply_to)
        actual = self._check(destination.channel if isinstance(destination, ThreadRef) else destination)
        if isinstance(destination, ThreadRef):
            # A parent claim is checked by lookup before a write; otherwise a
            # reused thread ID could silently send into a different forum.
            await self.fetch_thread(destination)
            if not (await self.capabilities(actual)).thread_replies:
                raise PlatformOperationError('unsupported')
            container = destination.external_thread_id
        else:
            container = destination.external_channel_id
        if presentation.mode == 'webhook' and not (await self.capabilities(actual)).webhook_send:
            raise PlatformOperationError('unsupported')
        if self.forbid_send:
            raise PlatformOperationError('forbidden')
        self.creates += 1
        message = MessageRef(self.connector.key, container, str(332 + self.creates))
        self.messages[message] = content
        result = CreatedMessage(message, presentation.mode)
        validate_created_receipt(attempt, result, presentation=presentation)
        self._finish_create(attempt, result)
        return result

    async def edit_message(self, message, content):
        """Edit physical copy only, without altering its canonical AP object."""
        if message.connector_key != self.connector.key:
            raise PlatformOperationError('invalid_request')
        if message not in self.messages:
            raise PlatformOperationError('not_found')
        if self.forbid_send:
            raise PlatformOperationError('forbidden')
        self.messages[message] = content

    async def delete_message(self, message):
        """Delete physical copy only and surface missing messages explicitly."""
        if message.connector_key != self.connector.key:
            raise PlatformOperationError('invalid_request')
        if self.forbid_send:
            raise PlatformOperationError('forbidden')
        try:
            del self.messages[message]
        except KeyError as exc:
            raise PlatformOperationError('not_found') from exc

    async def reconcile_create(self, attempt):
        """Observe a confirmed receipt; inaccessible or unproven evidence is unresolved."""
        if attempt.connector_key != self.connector.key:
            raise PlatformOperationError('invalid_request')
        previous = self.requests.get(attempt.key)
        if previous is not None and previous[0] != attempt:
            raise PlatformOperationError('invalid_request')
        if not self.allow_proof:
            return None
        result = self.receipts.get(attempt.key)
        if result is None and attempt.operation == 'reply_thread':
            # Recovery may observe a pre-existing rooted thread after restart.
            result = self._existing_reply_thread(attempt.destination)
        if result is None:
            return None
        # A retained receipt is attempt-linked; root threads additionally have
        # platform-proven unique root identity even with no prior attempt.
        validate_created_receipt(attempt, result)
        return result


@dataclass
class EventSink:
    """Collect SDK-independent inbound events for future Stage 03 routing."""
    events: list = field(default_factory=list)

    async def handle_platform_event(self, event):
        """Retain physical events without publishing any ActivityPub activity."""
        self.events.append(event)


@pytest.mark.asyncio
async def test_two_connectors_and_real_channel_claims_drive_sdk_free_post_and_reply(tmp_path):
    """Mixed physical forum IDs route by connector while AP/claim state stays stable."""
    database = Database(f'sqlite:///{tmp_path / "stage02.db"}')
    database.migrate()
    actor = 'https://remote.test/c/news'
    database.remote_subscriptions.create_subscription(
        discord_channel_id=900, discord_guild_id=1,
        lemmy_community_actor_id=actor, lemmy_community_name='news',
        lemmy_community_id=None, status='accepted',
    )
    with database.immediate_session() as session:
        PlatformSurfaceRepository.add_connector(session, connector_key='fluxer-a', platform='fluxer', instance_origin='https://fluxer.test')
        PlatformSurfaceRepository.ensure_channel(session, connector_key='discord-primary', external_channel_id='900', kind='forum', external_guild_id='1')
        fluxer_surface = PlatformSurfaceRepository.ensure_channel(session, connector_key='fluxer-a', external_channel_id='900', kind='forum', external_guild_id='1')
        database.remote_subscriptions.add_subscription(
            session, fluxer_surface.id, lemmy_community_actor_id=actor, status='accepted',
        )
        surfaces = session.scalars(select(ChannelSurface).where(ChannelSurface.external_channel_id == '900')).all()
        before = [(row.id, row.connector_id, row.local_community_id, row.remote_subscription_id) for row in surfaces]
    d = PhysicalPlatform(ConnectorRef('discord-primary', 'discord'), {'900': ChannelRef('discord-primary', '900', 'forum', '1')}, {'900': PlatformCapabilities(forum_posts=True, thread_replies=True, message_edit_delete=True)})
    f = PhysicalPlatform(ConnectorRef('fluxer-a', 'fluxer', 'https://fluxer.test'), {'900': ChannelRef('fluxer-a', '900', 'forum', '1')}, {'900': PlatformCapabilities(forum_posts=True, thread_replies=True, message_edit_delete=True)})
    registry = PlatformAdapterRegistry([d.connector, f.connector])
    registry.register(d)
    registry.register(f)
    assert isinstance(registry.get('fluxer-a'), PlatformAdapterPort)
    # A platform lookup does not grant a claim; check domain admission separately.
    for key in ('discord-primary', 'fluxer-a'):
        placement = database.platform_surfaces.resolve_claim(key, '900')
        assert placement is not None and placement.role == 'remote_subscription'
        assert placement.remote_subscription_id is not None
    assert database.platform_surfaces.resolve_claim('fluxer-a', 'unclaimed') is None
    assert registry.get_for_channel(ChannelRef('fluxer-a', '900')) is f
    assert registry.get_for_channel(ChannelRef('discord-primary', '900')) is d
    sink = EventSink()
    assert isinstance(sink, PlatformInboundSinkPort)
    source = ThreadRef(d.channels['900'], '111')
    starter = MessageRef('discord-primary', '111', '222')
    await sink.handle_platform_event(ThreadCreated('discord-primary', source, 'post', starter=starter, newly_created=True))
    await sink.handle_platform_event(MessageCreated('discord-primary', starter, AuthorRef('discord-primary', 'user', 'human', 'alice'), ContentDocument('post'), is_starter=True, thread_context=source))
    await sink.handle_platform_event(ThreadCreated('discord-primary', source, 'post', starter=starter, newly_created=False))
    assert len(sink.events) == 3 and sink.events[0].starter == sink.events[1].message
    forum = (await f.fetch_channel(ChannelRef('fluxer-a', '900'))).channel
    attempt = CreateAttempt('post-copy', 'fluxer-a', 'forum_thread', forum)
    created = await f.create_forum_thread(forum, ContentDocument('body'), title='post', presentation=DeliveryPresentation(), attempt=attempt)
    assert (created.thread.external_thread_id, created.starter.external_message_id) == ('111', '222')
    assert (await f.fetch_thread(created.thread)).starter == created.starter
    parent = created.starter
    reply_attempt = CreateAttempt('reply-copy', 'fluxer-a', 'message', created.thread)
    reply = await f.send_message(created.thread, ContentDocument('reply'), reply_to=parent, presentation=DeliveryPresentation(), attempt=reply_attempt)
    assert (await f.fetch_message(reply.message)).content.body == 'reply'
    await f.edit_message(reply.message, ContentDocument('edited'))
    assert f.messages[reply.message].body == 'edited'
    await f.delete_message(reply.message)
    assert reply.message not in f.messages
    with pytest.raises(PlatformOperationError):
        await f.delete_message(reply.message)
    with database.session() as session:
        after = [(row.id, row.connector_id, row.local_community_id, row.remote_subscription_id) for row in session.scalars(select(ChannelSurface).where(ChannelSurface.external_channel_id == '900')).all()]
    assert sorted(before) == sorted(after)
    assert database.remote_subscriptions.count_subscriptions_for_community(actor) == 2
    database.close()


@pytest.mark.asyncio
async def test_independent_forum_posts_keep_both_physical_threads_and_starters():
    """Creating two unrelated posts must never overwrite the first physical copy."""
    forum = ChannelRef('discord-primary', '900', 'forum', '1')
    platform = PhysicalPlatform(
        ConnectorRef('discord-primary', 'discord'),
        {'900': forum},
        {'900': PlatformCapabilities(forum_posts=True)},
    )
    first = await platform.create_forum_thread(
        forum, ContentDocument('first body'), title='first',
        presentation=DeliveryPresentation(),
        attempt=CreateAttempt('first', 'discord-primary', 'forum_thread', forum),
    )
    second = await platform.create_forum_thread(
        forum, ContentDocument('second body'), title='second',
        presentation=DeliveryPresentation(),
        attempt=CreateAttempt('second', 'discord-primary', 'forum_thread', forum),
    )
    assert first.thread != second.thread
    assert first.starter != second.starter
    assert platform.creates == len(platform.threads) == len(platform.messages) == 2
    assert (await platform.fetch_thread(first.thread)).starter == first.starter
    assert (await platform.fetch_thread(second.thread)).starter == second.starter
    assert (await platform.fetch_message(first.starter)).content.body == 'first body'
    assert (await platform.fetch_message(second.starter)).content.body == 'second body'



@pytest.mark.asyncio
async def test_forum_creation_does_not_overwrite_preexisting_thread_id():
    """A discovered physical thread must survive allocation of a new fake post."""
    forum = ChannelRef('discord-primary', '900', 'forum', '1')
    adapter = PhysicalPlatform(
        ConnectorRef('discord-primary', 'discord'),
        {'900': forum},
        {'900': PlatformCapabilities(forum_posts=True)},
    )
    old = ThreadRef(forum, '111')
    old_starter = MessageRef('discord-primary', '111', '222')
    adapter.threads['111'] = ThreadView(old, name='existing', starter=old_starter, kind='forum')
    adapter.messages[old_starter] = ContentDocument('existing body')
    created = await adapter.create_forum_thread(
        forum, ContentDocument('new body'), title='new',
        presentation=DeliveryPresentation(),
        attempt=CreateAttempt('new', 'discord-primary', 'forum_thread', forum),
    )
    assert created.thread.external_thread_id == '112'
    assert created.starter.external_message_id == '223'
    assert len(adapter.threads) == len(adapter.messages) == 2
    assert (await adapter.fetch_thread(old)).starter == old_starter
    assert (await adapter.fetch_message(old_starter)).content.body == 'existing body'
    assert (await adapter.fetch_message(created.starter)).content.body == 'new body'



@pytest.mark.asyncio
async def test_bad_target_attempt_and_missing_capability_fail_before_create():
    """Invalid target metadata, reused key and cross-container replies do not write copies."""
    f = PhysicalPlatform(ConnectorRef('fluxer-a', 'fluxer', 'https://fluxer.test'), {'900': ChannelRef('fluxer-a', '900', 'forum')})
    forum = f.channels['900']
    attempt = CreateAttempt('create', 'fluxer-a', 'forum_thread', forum)
    with pytest.raises(PlatformOperationError) as error:
        await f.create_forum_thread(forum, ContentDocument('hello'), title='post', presentation=DeliveryPresentation(), attempt=attempt)
    assert error.value.reason == 'unsupported' and f.creates == 0
    f.capabilities_by_channel['900'] = PlatformCapabilities(forum_posts=True)
    with pytest.raises(PlatformOperationError):
        await f.create_forum_thread(ChannelRef('fluxer-a', '900', 'text'), ContentDocument('hello'), title='post', presentation=DeliveryPresentation(), attempt=attempt)
    assert f.creates == 0
    await f.create_forum_thread(forum, ContentDocument('hello'), title='post', presentation=DeliveryPresentation(), attempt=attempt)
    with pytest.raises(PlatformOperationError):
        await f.create_forum_thread(forum, ContentDocument('changed'), title='post', presentation=DeliveryPresentation(), attempt=attempt)
    assert f.creates == 1
    reply_attempt = CreateAttempt('reply', 'fluxer-a', 'message', ThreadRef(forum, '111'))
    with pytest.raises(PlatformOperationError):
        await f.send_message(ThreadRef(forum, '111'), ContentDocument('x'), reply_to=MessageRef('fluxer-a', 'other', 'parent'), presentation=DeliveryPresentation(), attempt=reply_attempt)
    assert f.creates == 1
    with pytest.raises(PlatformOperationError):
        await f.create_forum_thread(forum, ContentDocument('body'), title='x', presentation=DeliveryPresentation('webhook'), attempt=CreateAttempt('webhook', 'fluxer-a', 'forum_thread', forum))
    assert f.creates == 1


@pytest.mark.asyncio
async def test_reconciliation_has_no_retry_and_verified_root_thread_can_preexist():
    """Unknown send stays unresolved; root-anchored recovery adopts an existing thread."""
    text = ChannelRef('discord-primary', '700', 'text', '1')
    root = MessageRef('discord-primary', '700', '888')
    adapter = PhysicalPlatform(ConnectorRef('discord-primary', 'discord'), {'700': text}, {'700': PlatformCapabilities(message_threads=True, thread_replies=True)})
    adapter.messages[root] = ContentDocument('root')
    existing = ThreadRef(text, '888')
    adapter.threads['888'] = ThreadView(existing, kind='message')
    attempt = CreateAttempt('preexisting', 'discord-primary', 'reply_thread', root)
    assert await adapter.reconcile_create(attempt) == existing  # Proven root, even without an attempt receipt.
    adopted = await adapter.create_reply_thread(root, title='discussion', attempt=attempt)
    assert adopted == existing and adapter.creates == 0
    assert await adapter.reconcile_create(attempt) == existing
    sent = await adapter.send_message(existing, ContentDocument('comment'), reply_to=None, presentation=DeliveryPresentation(), attempt=CreateAttempt('comment', 'discord-primary', 'message', existing))
    assert sent.message.external_container_id == existing.external_thread_id
    adapter.allow_proof = False
    before = adapter.creates
    assert await adapter.reconcile_create(CreateAttempt('comment', 'discord-primary', 'message', existing)) is None
    assert adapter.creates == before  # No blind resend.
    wrong = ThreadRef(ChannelRef('discord-primary', 'other', 'text'), '888')
    adapter.threads['888'] = ThreadView(wrong, kind='message')
    with pytest.raises(PlatformOperationError):
        await adapter.create_reply_thread(root, title='discussion', attempt=CreateAttempt('wrong', 'discord-primary', 'reply_thread', root))
    assert adapter.creates == before


def test_registry_rejects_unknown_mismatched_duplicate_and_never_closes_clients():
    """Registry selection never falls back to Discord and owns no SDK lifecycle."""
    a = ConnectorRef('discord-primary', 'discord')
    f = ConnectorRef('fluxer-a', 'fluxer', 'https://fluxer.test')
    registry = PlatformAdapterRegistry([a, f])
    discord = PhysicalPlatform(a, {})
    registry.register(discord)
    assert registry.get(a.key) is discord
    for key in ['not-found', 'fluxer-a']:
        with pytest.raises(LookupError):
            registry.get(key)
    with pytest.raises(ValueError):
        registry.register(discord)
    with pytest.raises(ValueError):
        registry.register(PhysicalPlatform(ConnectorRef('fluxer-a', 'discord'), {}))
    with pytest.raises(ValueError):
        registry.register(PhysicalPlatform(ConnectorRef('unknown', 'discord'), {}))
    with pytest.raises(LookupError):
        registry.get_for_channel(ChannelRef('unknown', '900'))


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['forum_thread', 'message'])
async def test_remote_create_succeeds_but_response_is_lost_reconciles_without_resend(operation):
    """An accepted physical copy remains discoverable after transport outcome is unknown."""
    forum = ChannelRef('discord-primary', '900', 'forum', '1')
    adapter = PhysicalPlatform(
        ConnectorRef('discord-primary', 'discord'),
        {'900': forum},
        {'900': PlatformCapabilities(forum_posts=True, thread_replies=True)},
        lose_response_keys={'lost'},
    )
    if operation == 'forum_thread':
        destination = forum
        attempt = CreateAttempt('lost', 'discord-primary', 'forum_thread', destination)
        async def perform():
            """Write a forum post and intentionally lose the successful HTTP result."""
            return await adapter.create_forum_thread(
                destination, ContentDocument('post'), title='title',
                presentation=DeliveryPresentation(), attempt=attempt,
            )
    else:
        source_thread = ThreadRef(forum, '111')
        adapter.threads['111'] = ThreadView(source_thread, kind='forum')
        destination = source_thread
        attempt = CreateAttempt('lost', 'discord-primary', 'message', destination)
        async def perform():
            """Write one reply and intentionally lose the successful HTTP result."""
            return await adapter.send_message(
                destination, ContentDocument('comment'), reply_to=None,
                presentation=DeliveryPresentation(), attempt=attempt,
            )
    with pytest.raises(PlatformOperationError) as exc:
        await perform()
    assert exc.value.reason == 'unknown_delivery'
    assert adapter.creates == 1  # remote already accepted the mutation
    receipt = await adapter.reconcile_create(attempt)
    assert isinstance(receipt, CreatedThread if operation == 'forum_thread' else CreatedMessage)
    assert adapter.creates == 1  # reconciliation performs no second write
    adapter.allow_proof = False
    assert await adapter.reconcile_create(attempt) is None
    assert adapter.creates == 1  # unresolved is neither a failure nor retry permission
    # Similar-looking content not linked to this attempt is not delivery evidence.
    other = CreateAttempt('unrelated', 'discord-primary', operation, destination)
    adapter.allow_proof = True
    assert await adapter.reconcile_create(other) is None
    altered = CreateAttempt('lost', 'discord-primary', operation,
                            ThreadRef(forum, 'other') if operation == 'message'
                            else ChannelRef('discord-primary', '901', 'forum'))
    with pytest.raises(PlatformOperationError):
        await adapter.reconcile_create(altered)


@pytest.mark.asyncio
async def test_verified_root_thread_recovery_rejects_wrong_kind_parent_and_unavailable_lookup():
    """Only the one-root-one-message-thread relationship permits adoption."""
    text = ChannelRef('discord-primary', '700', 'text', '1')
    adapter = PhysicalPlatform(ConnectorRef('discord-primary', 'discord'), {'700': text})
    root = MessageRef('discord-primary', '700', '888')
    attempt = CreateAttempt('recover', 'discord-primary', 'reply_thread', root)
    verified = ThreadRef(text, '888')
    adapter.threads['888'] = ThreadView(verified, kind='message')
    assert await adapter.reconcile_create(attempt) == verified
    assert adapter.creates == 0
    adapter.threads['888'] = ThreadView(verified, kind='forum')
    assert await adapter.reconcile_create(attempt) is None
    adapter.threads['888'] = ThreadView(ThreadRef(ChannelRef('discord-primary', 'other', 'text'), '888'), kind='message')
    assert await adapter.reconcile_create(attempt) is None
    adapter.threads['888'] = ThreadView(ThreadRef(ChannelRef('discord-primary', '700', 'text', 'wrong-guild'), '888'), kind='message')
    assert await adapter.reconcile_create(attempt) is None
    adapter.threads['888'] = ThreadView(verified, kind='message')
    adapter.allow_proof = False
    assert await adapter.reconcile_create(attempt) is None
    assert adapter.creates == 0
    fluxer = PhysicalPlatform(ConnectorRef('fluxer-a', 'fluxer', 'https://fluxer.test'),
                               {'700': ChannelRef('fluxer-a', '700', 'text')})
    fluxer.threads['888'] = ThreadView(ThreadRef(fluxer.channels['700'], '888'), kind='message')
    assert await fluxer.reconcile_create(CreateAttempt('no-guarantee', 'fluxer-a', 'reply_thread', MessageRef('fluxer-a', '700', '888'))) is None


@pytest.mark.asyncio
async def test_permissions_are_not_implicitly_granted_by_capability_and_webhook_is_explicit():
    """Positive capability and stale preflight do not bypass final API permission."""
    forum = ChannelRef('discord-primary', '900', 'forum', '1')
    adapter = PhysicalPlatform(ConnectorRef('discord-primary', 'discord'), {'900': forum},
                               {'900': PlatformCapabilities(forum_posts=True)}, forbid_send=True)
    bot = AuthorRef('discord-primary', 'bot-id', 'bot', 'Bridge', True)
    preflight = await adapter.check_permissions(bot, forum, frozenset({'create_forum_post'}))
    assert preflight == {'create_forum_post': 'unknown'}
    with pytest.raises(PlatformOperationError) as denied:
        await adapter.create_forum_thread(forum, ContentDocument('post'), title='topic',
                                          presentation=DeliveryPresentation(),
                                          attempt=CreateAttempt('denied', 'discord-primary', 'forum_thread', forum))
    assert denied.value.reason == 'forbidden'
    assert adapter.creates == 0
    adapter.forbid_send = False
    with pytest.raises(PlatformOperationError) as unsupported:
        await adapter.create_forum_thread(forum, ContentDocument('post'), title='topic',
                                          presentation=DeliveryPresentation('webhook'),
                                          attempt=CreateAttempt('webhook', 'discord-primary', 'forum_thread', forum))
    assert unsupported.value.reason == 'unsupported'
    adapter.capabilities_by_channel['900'] = PlatformCapabilities(forum_posts=True, webhook_send=True)
    receipt = await adapter.create_forum_thread(forum, ContentDocument('post'), title='topic',
                                                presentation=DeliveryPresentation('webhook'),
                                                attempt=CreateAttempt('webhook-ok', 'discord-primary', 'forum_thread', forum))
    assert receipt.delivery_mode == 'webhook' and adapter.creates == 1


@pytest.mark.asyncio
async def test_stage01_metadata_upgrade_does_not_create_a_new_surface_or_adapter_identity(tmp_path):
    """Repository enrichment and registry lookup preserve the same physical key."""
    database = Database(f'sqlite:///{tmp_path / "enrichment.db"}')
    database.migrate()
    before_ref = ChannelRef('discord-primary', '900')
    with database.immediate_session() as session:
        before = PlatformSurfaceRepository.ensure_channel(session, connector_key='discord-primary',
                                                          external_channel_id='900')
        before_id = before.id
    with database.immediate_session() as session:
        after = PlatformSurfaceRepository.ensure_channel(session, connector_key='discord-primary',
                                                         external_channel_id='900', external_guild_id='1', kind='forum')
        assert after.id == before_id
        assert after.kind == 'forum' and after.external_guild_id == '1'
    after_ref = ChannelRef('discord-primary', '900', 'forum', '1')
    assert before_ref == after_ref and hash(before_ref) == hash(after_ref)
    adapter = PhysicalPlatform(ConnectorRef('discord-primary', 'discord'), {'900': after_ref})
    registry = PlatformAdapterRegistry([adapter.connector])
    registry.register(adapter)
    assert registry.get_for_channel(before_ref) is registry.get_for_channel(after_ref)
    assert (await adapter.fetch_channel(before_ref)).channel.kind == 'forum'
    with pytest.raises(PlatformOperationError):
        await adapter.fetch_channel(ChannelRef('discord-primary', '900', 'text'))
    database.close()


def test_registry_rejects_connector_origin_change_and_duplicate_configuration():
    """An instance swap under the same key must not silently move existing copies."""
    original = ConnectorRef('fluxer-a', 'fluxer', 'https://original.test')
    with pytest.raises(ValueError):
        PlatformAdapterRegistry([original, original])
    registry = PlatformAdapterRegistry([original])
    with pytest.raises(ValueError):
        registry.register(PhysicalPlatform(ConnectorRef('fluxer-a', 'fluxer', 'https://replacement.test'), {}))
    with pytest.raises(LookupError):
        registry.get('fluxer-a')


@pytest.mark.asyncio
async def test_unknown_forum_starter_defers_until_fetch_returns_real_message_id():
    """A thread notice without a starter cannot fabricate an AP post identity."""
    forum = ChannelRef('discord-primary', '900', 'forum', '1')
    thread = ThreadRef(forum, '111')
    adapter = PhysicalPlatform(ConnectorRef('discord-primary', 'discord'), {'900': forum})
    adapter.threads['111'] = ThreadView(thread, kind='forum', starter=None)
    sink = EventSink()
    await sink.handle_platform_event(ThreadCreated('discord-primary', thread, 'post', starter=None))
    assert sink.events[0].starter is None
    assert (await adapter.fetch_thread(thread)).starter is None
    assert adapter.creates == 0  # No publish/create based on thread ID alone.
    starter = MessageRef('discord-primary', '111', '222')
    adapter.threads['111'] = ThreadView(thread, kind='forum', starter=starter)
    assert (await adapter.fetch_thread(thread)).starter == starter
    assert starter.external_message_id != thread.external_thread_id
    with pytest.raises(ValueError):
        adapter.threads['111'] = ThreadView(thread, kind='forum', starter=MessageRef('discord-primary', '900', '111'))


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['text', 'announcement'])
async def test_personal_feed_reply_thread_is_root_scoped_in_supported_channels(kind):
    """Plain and announcement roots create/adopt message threads, never forum posts."""
    channel = ChannelRef('discord-primary', '700', kind, '1')
    adapter = PhysicalPlatform(ConnectorRef('discord-primary', 'discord'), {'700': channel},
                               {'700': PlatformCapabilities(message_threads=True, thread_replies=True)})
    root = MessageRef('discord-primary', '700', '888')
    adapter.messages[root] = ContentDocument('root')
    attempt = CreateAttempt(f'reply-{kind}', 'discord-primary', 'reply_thread', root)
    thread = await adapter.create_reply_thread(root, title='Conversation', attempt=attempt)
    assert thread.external_thread_id == root.external_message_id
    assert thread.channel == channel
    assert adapter.creates == 1
    assert (await adapter.fetch_thread(thread)).starter is None  # Root is outside the thread.
    assert await adapter.reconcile_create(attempt) == thread
    assert (await adapter.create_reply_thread(root, title='Conversation', attempt=attempt)) == thread
    assert adapter.creates == 1  # Existing thread is adopted rather than recreated.


@pytest.mark.asyncio
async def test_reply_thread_capability_is_checked_before_any_root_creation():
    """A valid text root cannot bypass an unsupported message-thread feature."""
    root_channel = ChannelRef('discord-primary', '700', 'text')
    root = MessageRef('discord-primary', '700', '888')
    adapter = PhysicalPlatform(ConnectorRef('discord-primary', 'discord'), {'700': root_channel})
    adapter.messages[root] = ContentDocument('root')
    attempt = CreateAttempt('reply-unsupported', 'discord-primary', 'reply_thread', root)
    with pytest.raises(PlatformOperationError) as error:
        await adapter.create_reply_thread(root, title='Conversation', attempt=attempt)
    assert error.value.reason == 'unsupported' and adapter.creates == 0
    adapter.capabilities_by_channel['700'] = PlatformCapabilities(message_threads=True)
    created = await adapter.create_reply_thread(root, title='Conversation', attempt=attempt)
    assert created.external_thread_id == '888' and adapter.creates == 1

@pytest.mark.asyncio
async def test_thread_lookup_and_delivery_reject_wrong_parent_and_keep_actual_thread():
    """An API caller cannot send into forum A's thread through a forged forum B ref."""
    forum_a = ChannelRef('discord-primary', '900', 'forum', '1')
    forum_b = ChannelRef('discord-primary', '901', 'forum', '1')
    adapter = PhysicalPlatform(
        ConnectorRef('discord-primary', 'discord'),
        {'900': forum_a, '901': forum_b},
        {'900': PlatformCapabilities(forum_posts=True, thread_replies=True),
         '901': PlatformCapabilities(forum_posts=True, thread_replies=True)},
    )
    created = await adapter.create_forum_thread(
        forum_a, ContentDocument('root in A'), title='A',
        presentation=DeliveryPresentation(),
        attempt=CreateAttempt('post-a', 'discord-primary', 'forum_thread', forum_a),
    )
    forged = ThreadRef(forum_b, created.thread.external_thread_id)
    with pytest.raises(PlatformOperationError) as fetch_error:
        await adapter.fetch_thread(forged)
    assert fetch_error.value.reason == 'invalid_request'
    count = adapter.creates
    with pytest.raises(PlatformOperationError) as send_error:
        await adapter.send_message(
            forged, ContentDocument('wrong target'), reply_to=None,
            presentation=DeliveryPresentation(),
            attempt=CreateAttempt('bad-target', 'discord-primary', 'message', forged),
        )
    assert send_error.value.reason == 'invalid_request'
    assert adapter.creates == count
    assert len(adapter.messages) == 1
    assert (await adapter.fetch_thread(created.thread)).thread == created.thread

    # A physical message sent through the real parent must still be deliverable.
    sent = await adapter.send_message(
        created.thread, ContentDocument('valid reply'), reply_to=created.starter,
        presentation=DeliveryPresentation(),
        attempt=CreateAttempt('right-target', 'discord-primary', 'message', created.thread),
    )
    assert (await adapter.fetch_message(sent.message)).content.body == 'valid reply'
