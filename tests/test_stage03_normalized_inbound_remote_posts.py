"""Remote ActivityPub posts delivered through typed Discord transport and real SQLite.

Only physical Discord writes are faked; subscription selection, canonical IDs,
receipt persistence and replay decisions run through the actual runtime.
"""
from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import dataclass
from types import ModuleType
import sys

import pytest

from src.activitypub_models import ActivityPubEvent
from src.community_sync.runtime import CommunityRuntime
from src.platform_types import CreatedThread, MessageRef, ThreadRef
from support.db import add_accepted_subscription, build_database
from tests_constants import LEMMY_EXAMPLE_DOMAIN


COMMUNITY = f'https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers'


class RecordingDiscord:
    """Return per-forum physical receipts while exposing attempted operations."""

    def __init__(self, *, fail_forum=None):
        """Record sends and optionally reject a selected physical destination."""
        self.calls = []
        self.fail_forum = fail_forum

    async def create_forum_thread(self, forum, content, *, title, presentation, attempt):
        """Confirm distinct thread and starter IDs without a Discord SDK."""
        self.calls.append((forum, content, title, presentation, attempt))
        if forum.external_channel_id == self.fail_forum:
            raise RuntimeError('Discord down')
        thread = ThreadRef(forum, str(int(forum.external_channel_id) + 1000))
        return CreatedThread(thread, MessageRef('discord-primary', thread.external_thread_id,
                                                 str(int(forum.external_channel_id) + 2000)), 'bot')


def post():
    """Build a legitimate remotely authored top-level ActivityPub post."""
    now = datetime.now(timezone.utc).isoformat()
    return ActivityPubEvent.model_validate({
        'event_type': 'post.created', 'delivery_id': 'https://remote.example/activities/1',
        'occurred_at': now, 'community_actor_id': COMMUNITY,
        'actor_id': f'https://{LEMMY_EXAMPLE_DOMAIN}/u/bob',
        'object': {
            'ap_id': f'https://{LEMMY_EXAMPLE_DOMAIN}/post/777', 'kind': 'post',
            'lemmy_id': 777, 'title': 'Hello world', 'body_markdown': 'Body',
            'url': f'https://{LEMMY_EXAMPLE_DOMAIN}/post/777',
            'published_at': now, 'author_name': 'bob',
        },
    })


@pytest.fixture(autouse=True)
def isolated_result_type(monkeypatch):
    """Avoid importing the absent Discord SDK's command tree for one result type."""
    module = ModuleType('src.activitypub_handlers')
    @dataclass
    class HandlerResult:
        """Mirror the three public fields of the production outcome contract."""
        status: str
        outcome: object
        detail: str
    module.HandlerResult = HandlerResult
    monkeypatch.setitem(sys.modules, 'src.activitypub_handlers', module)


@pytest.mark.asyncio
async def test_inbound_post_uses_one_canonical_group_and_distinct_physical_receipts(tmp_path):
    """One AP post creates two forum copies, then the same AP replay creates none."""
    db = build_database(tmp_path, 'inbound-remote-posts.db')
    for channel in (100, 200):
        add_accepted_subscription(db, channel_id=channel, guild_id=10)
    runtime = CommunityRuntime(database=db, content_publish_service=object())
    transport = RecordingDiscord()
    event = post()

    result = await runtime.handle_normalized_inbound_post(event, adapter=transport)
    assert result.status == 'processed'
    group = db.discord_fanout_groups.get_thread_group_by_ap_object(event.object.ap_id)
    assert group.ap_activity_id == event.delivery_id
    deliveries = db.discord_fanout_groups.get_thread_deliveries(group.id)
    assert {(d.discord_channel_id, d.discord_thread_id, d.discord_starter_message_id)
            for d in deliveries} == {(100, 1100, 2100), (200, 1200, 2200)}
    assert {call[4].key for call in transport.calls} == {
        f'inbound-ap:post:{event.object.ap_id}:discord-primary:100',
        f'inbound-ap:post:{event.object.ap_id}:discord-primary:200',
    }
    assert transport.calls[0][1].body.startswith('**Hello world**')
    assert transport.calls[0][2] == 'Hello world'
    assert (await runtime.handle_normalized_inbound_post(event, adapter=transport)).status == 'skipped'
    assert len(transport.calls) == 2


@pytest.mark.asyncio
async def test_inbound_post_rejects_missing_or_invalid_subscription_guild(tmp_path):
    """A subscribed surface without a proven guild cannot authorize a POST."""
    db = build_database(tmp_path, 'inbound-remote-policy.db')
    add_accepted_subscription(db, channel_id=100, guild_id=10)
    db.remote_subscriptions.create_subscription(
        discord_channel_id=200, discord_guild_id=None,
        lemmy_community_actor_id=COMMUNITY, lemmy_community_name='hackers',
        lemmy_community_id=42, status='accepted',
    )
    transport = RecordingDiscord()
    runtime = CommunityRuntime(database=db, content_publish_service=object())
    await runtime.handle_normalized_inbound_post(post(), adapter=transport)
    assert [c[0].external_channel_id for c in transport.calls] == ['100']


@pytest.mark.asyncio
async def test_inbound_post_no_subscription_never_calls_adapter(tmp_path):
    """A community without an accepted Follow produces no physical copy."""
    db = build_database(tmp_path, 'inbound-remote-empty.db')
    runtime = CommunityRuntime(database=db, content_publish_service=object())
    transport = RecordingDiscord()
    result = await runtime.handle_normalized_inbound_post(post(), adapter=transport)
    assert result.status == 'skipped'
    assert transport.calls == []


@pytest.mark.asyncio
async def test_inbound_post_refuses_wrong_target_receipt_before_mapping(tmp_path):
    """An incorrectly addressed Discord receipt never becomes a canonical copy."""
    db = build_database(tmp_path, 'inbound-bad-receipt.db')
    add_accepted_subscription(db, channel_id=100, guild_id=10)
    runtime = CommunityRuntime(database=db, content_publish_service=object())

    class WrongReceipt(RecordingDiscord):
        """Emulate a misbehaving transport returning a foreign physical forum."""

        async def create_forum_thread(self, forum, content, *, title, presentation, attempt):
            """Return IDs from another forum despite a valid request."""
            other = type(forum)('discord-primary', '900', 'forum', '10')
            thread = ThreadRef(other, '1500')
            return CreatedThread(thread, MessageRef('discord-primary', '1500', '2500'), 'bot')

    from src.platform_types import PlatformOperationError
    with pytest.raises(PlatformOperationError, match='receipt does not match forum'):
        await runtime.handle_normalized_inbound_post(post(), adapter=WrongReceipt())
    group = db.discord_fanout_groups.get_thread_group_by_ap_object(post().object.ap_id)
    assert group is not None
    assert db.discord_fanout_groups.get_thread_deliveries(group.id) == []


@pytest.mark.asyncio
async def test_inbound_post_rechecks_follow_before_each_forum_post(tmp_path):
    """Undo(Follow) during the first send prevents the second forum POST."""
    db = build_database(tmp_path, 'inbound-concurrent-undo.db')
    for channel in (100, 200):
        add_accepted_subscription(db, channel_id=channel, guild_id=10)
    runtime = CommunityRuntime(database=db, content_publish_service=object())

    class UndoDuringSend(RecordingDiscord):
        """Make the second subscription inactive after the first HTTP response."""

        async def create_forum_thread(self, forum, content, *, title, presentation, attempt):
            """Simulate a legitimate concurrent subscription lifecycle change."""
            receipt = await super().create_forum_thread(
                forum, content, title=title, presentation=presentation, attempt=attempt,
            )
            db.remote_subscriptions.delete_subscription(200)
            return receipt

    transport = UndoDuringSend()
    await runtime.handle_normalized_inbound_post(post(), adapter=transport)
    assert [c[0].external_channel_id for c in transport.calls] == ['100']


@pytest.mark.asyncio
async def test_failed_first_remote_post_target_does_not_block_healthy_second(tmp_path):
    """One Discord permission failure leaves only that surface pending, not the whole fanout."""
    db = build_database(tmp_path, 'inbound-post-partial-failure.db')
    try:
        for channel in (100, 200):
            add_accepted_subscription(db, channel_id=channel, guild_id=10)
        runtime = CommunityRuntime(database=db, content_publish_service=object())
        discord = RecordingDiscord(fail_forum='100')
        result = await runtime.handle_normalized_inbound_post(post(), adapter=discord)
        group = db.discord_fanout_groups.get_thread_group_by_ap_object(post().object.ap_id)
        assert result.status == 'processed'
        assert [c[0].external_channel_id for c in discord.calls] == ['100', '200']
        assert {(d.discord_channel_id, d.discord_thread_id) for d in
                db.discord_fanout_groups.get_thread_deliveries(group.id)} == {(200, 1200)}
        # Ordinary replays do not blindly POST again, since an earlier create
        # might have landed without returning a confirmed receipt.
        assert (await runtime.handle_normalized_inbound_post(post(), adapter=discord)).status == 'skipped'
        assert len(discord.calls) == 2
    finally:
        db.close()


@pytest.mark.asyncio
async def test_existing_partial_parent_cannot_repost_missing_remote_target(tmp_path):
    """A missing mapping in a persisted group is not proof of a negative POST."""
    db = build_database(tmp_path, 'inbound-post-recovery.db')
    try:
        for channel in (100, 200):
            add_accepted_subscription(db, channel_id=channel, guild_id=10)
        runtime = CommunityRuntime(database=db, content_publish_service=object())
        first = RecordingDiscord(fail_forum='100')
        await runtime.handle_normalized_inbound_post(post(), adapter=first)
        later = RecordingDiscord()
        outcome = await runtime.handle_normalized_inbound_post(post(), adapter=later, recover_missing=True)
        assert outcome.status == 'skipped'
        assert later.calls == []
        group = db.discord_fanout_groups.get_thread_group_by_ap_object(post().object.ap_id)
        assert {d.discord_channel_id for d in db.discord_fanout_groups.get_thread_deliveries(group.id)} == {200}
    finally:
        db.close()


@pytest.mark.asyncio
async def test_lost_receipt_cannot_repost_missing_forum_on_restart_backfill(tmp_path):
    """A persisted group with an ID-less possible Discord create cannot authorize another POST."""
    from src.platform_types import PlatformOperationError

    db = build_database(tmp_path, 'remote-unknown-restart.sqlite')
    try:
        for forum_id in (100, 200):
            add_accepted_subscription(db, channel_id=forum_id, guild_id=10)
        runtime = CommunityRuntime(database=db, content_publish_service=object())
        event = post()
        physical = []

        class LostResponse(RecordingDiscord):
            """Discord creates an actual thread in 100, then the HTTP response vanishes."""

            async def create_forum_thread(self, forum, content, *, title, presentation, attempt):
                """Record physical effects separately from SQLite's confirmed receipts."""
                receipt = await super().create_forum_thread(
                    forum, content, title=title, presentation=presentation, attempt=attempt,
                )
                physical.append(forum.external_channel_id)
                if forum.external_channel_id == '100':
                    raise PlatformOperationError('unknown_delivery', 'HTTP response lost')
                return receipt

        await runtime.handle_normalized_inbound_post(event, adapter=LostResponse())
        group = db.discord_fanout_groups.get_thread_group_by_ap_object(event.object.ap_id)
        assert {d.discord_channel_id for d in db.discord_fanout_groups.get_thread_deliveries(group.id)} == {200}
        assert physical == ['100', '200']
        # A fresh adapter/process has forgotten its in-process attempt receipts.
        # An AP object re-fetch cannot prove the first physical thread was absent.
        recovered = RecordingDiscord()
        outcome = await runtime.handle_normalized_inbound_post(
            event, adapter=recovered, recover_missing=True,
        )
        assert outcome.status == 'skipped'
        assert recovered.calls == []
        assert physical.count('100') == 1
    finally:
        db.close()
