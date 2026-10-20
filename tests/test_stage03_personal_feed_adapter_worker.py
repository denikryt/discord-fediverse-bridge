"""Personal-feed Discord rendering is observable through the typed physical adapter."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from src.models import User
from src.platform_types import (ChannelRef, ChannelView, ConnectorRef, ContentDocument,
                                CreatedMessage, MessageRef, PlatformOperationError,
                                ThreadRef, ThreadView)
from src.personal_feed_discord_delivery_worker import PersonalFeedDiscordDeliveryWorker
from src.personal_feeds import PersonalFeedService
from test_stage03_personal_feed_delivery_state import _world, _state, _update, NOW, OBJECT, ACTOR


class RecordingAdapter:
    """Stateful SDK substitute preserving typed physical receipts and side effects."""

    connector = ConnectorRef('discord-primary', 'discord')

    def __init__(self):
        """Initialize one physical personal root and no thread or replies."""
        self.thread_exists = False
        self.sent = []
        self.edits = []
        self.deletes = []
        self.threads = []
        self.before_channel_fetch = None
        self.send_failure = None
        self.kind = 'text'

    async def fetch_channel(self, ref):
        """Resolve only the physically observed personal-feed channel."""
        if self.before_channel_fetch is not None:
            self.before_channel_fetch()
        assert (ref.connector_key, ref.external_channel_id) == ('discord-primary', '10')
        return ChannelView(ChannelRef(self.connector.key, '10', self.kind, '1'), 'root')

    async def fetch_thread(self, ref):
        """Distinguish an existing root-anchored thread from an absent one."""
        assert ref.external_thread_id == '100'
        if not self.thread_exists:
            raise PlatformOperationError('not_found', 'thread not yet created')
        return ThreadView(ref, 'replies', kind='message')

    async def create_reply_thread(self, root, *, title, attempt):
        """Create one thread whose actual ID equals its root message ID."""
        assert root == MessageRef(self.connector.key, '10', '100')
        assert title
        self.threads.append((root, attempt, title))
        self.thread_exists = True
        return ThreadRef(ChannelRef(self.connector.key, '10', self.kind, '1'), '100')

    async def send_message(self, destination, content, *, reply_to, presentation, attempt):
        """Send one bot-owned physical message and return its verified receipt."""
        assert isinstance(destination, ThreadRef)
        assert destination.external_thread_id == '100'
        assert isinstance(content, ContentDocument)
        assert presentation.mode == 'bot'
        self.sent.append((content.body, reply_to, attempt))
        if self.send_failure is not None:
            raise self.send_failure
        return CreatedMessage(MessageRef(self.connector.key, '100', '500'), 'bot')

    async def edit_message(self, ref, content):
        """Update the exact previously acknowledged message, never another Create."""
        self.edits.append((ref, content.body))

    async def delete_message(self, ref):
        """Remove only the acknowledged Discord message."""
        self.deletes.append(ref)

    async def fetch_message(self, ref):
        """Confirm an existing Discord reply still belongs to the root thread."""
        from src.platform_types import MessageView
        assert ref.external_container_id == '100'
        return MessageView(ref, content=ContentDocument('old'))


def world(tmp_path):
    """Create a real active binding and a due inbound remote comment."""
    db, repo, delivery_id = _world(tmp_path)
    with db.session() as session:
        session.add(User(
            discord_user_id='10', activitypub_username='alice',
            actor_url='https://bridge.example/actors/users/alice',
            inbox_url='https://bridge.example/actors/users/alice/inbox',
            outbox_url='https://bridge.example/actors/users/alice/outbox',
            followers_url='https://bridge.example/actors/users/alice/followers',
            public_key_pem='public', private_key_pem='private'))
    service = PersonalFeedService(db)
    service.bind_or_move('10', 1, 10)
    return db, repo, delivery_id, service


@pytest.mark.asyncio
async def test_retained_remote_comment_creates_thread_then_one_typed_message(tmp_path):
    """AP Create sends one reply through adapter and records the physical ID."""
    db, repo, delivery_id, service = world(tmp_path)
    adapter = RecordingAdapter()
    worker = PersonalFeedDiscordDeliveryWorker(database=db, adapter=adapter, personal_feed_service=service)
    claim, = repo.claim_due()
    await worker._deliver(claim)
    assert _state(db, delivery_id).state == 'delivered'
    assert _state(db, delivery_id).discord_message_id == 500
    assert len(adapter.threads) == len(adapter.sent) == 1
    assert adapter.sent[0][0] == '**Bob:** v1'
    assert adapter.sent[0][2].key == f'personal-feed-comment:{OBJECT}'
    db.close()


@pytest.mark.asyncio
async def test_later_update_and_delete_reuse_exact_message_id(tmp_path):
    """AP Update and Delete mutate confirmed Discord receipt without new POST."""
    db, repo, delivery_id, service = world(tmp_path)
    adapter = RecordingAdapter()
    worker = PersonalFeedDiscordDeliveryWorker(database=db, adapter=adapter, personal_feed_service=service)
    await worker._deliver(repo.claim_due()[0])
    assert _update(repo, 'v2', 1).status == 'accepted'
    await worker._deliver(repo.claim_due()[0])
    assert adapter.edits == [(MessageRef('discord-primary', '100', '500'), '**Bob:** v2')]
    assert repo.admit_delete(object_id=OBJECT, activity_actor_id=ACTOR).status == 'accepted'
    await worker._deliver(repo.claim_due()[0])
    assert adapter.deletes == [MessageRef('discord-primary', '100', '500')]
    assert len(adapter.sent) == 1
    assert _state(db, delivery_id).state == 'delivered'
    db.close()


@pytest.mark.asyncio
async def test_move_during_channel_fetch_prevents_first_discord_post(tmp_path):
    """Revoked active personal-feed channel is checked after physical lookup."""
    db, repo, delivery_id, service = world(tmp_path)
    adapter = RecordingAdapter()
    adapter.before_channel_fetch = lambda: service.bind_or_move('10', 1, 11)
    worker = PersonalFeedDiscordDeliveryWorker(database=db, adapter=adapter, personal_feed_service=service)
    await worker._deliver(repo.claim_due()[0])
    assert adapter.sent == []
    assert _state(db, delivery_id).state == 'suppressed'
    db.close()


@pytest.mark.asyncio
async def test_ambiguous_first_discord_post_is_parked_and_never_replayed(tmp_path):
    """A lost response leaves canonical Updates pending without a second POST."""
    db, repo, delivery_id, service = world(tmp_path)
    adapter = RecordingAdapter()
    adapter.send_failure = PlatformOperationError('unknown_delivery', 'timeout')
    worker = PersonalFeedDiscordDeliveryWorker(database=db, adapter=adapter, personal_feed_service=service)
    await worker._deliver(repo.claim_due()[0])
    assert _state(db, delivery_id).state == 'failed'
    assert _state(db, delivery_id).next_attempt_at is None
    assert _update(repo, 'v2', 1).status == 'accepted'
    assert repo.claim_due() == []
    assert len(adapter.sent) == 1
    db.close()


@pytest.mark.asyncio
async def test_forum_destination_cannot_become_personal_feed_root(tmp_path):
    """Adapter-observed forum kind is rejected rather than creating a root thread."""
    db, repo, delivery_id, service = world(tmp_path)
    adapter = RecordingAdapter()
    adapter.kind = 'forum'
    worker = PersonalFeedDiscordDeliveryWorker(database=db, adapter=adapter, personal_feed_service=service)
    await worker._deliver(repo.claim_due()[0])
    assert not adapter.threads and not adapter.sent
    assert _state(db, delivery_id).state == 'failed'
    db.close()
