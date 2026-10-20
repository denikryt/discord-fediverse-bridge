"""Mapped remote ActivityPub comments delivered through typed Discord API.

Use file-backed SQLite canonical group/delivery rows and fake only the outer
Discord transport and the import-time SDK-dependent handler result type.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from types import ModuleType
import sys

import pytest

from src.activitypub_models import ActivityPubEvent
from src.community_sync.runtime import CommunityRuntime
from src.platform_types import CreatedMessage, MessageRef
from support.db import add_accepted_subscription, build_database
from tests_constants import LEMMY_EXAMPLE_DOMAIN


COMMUNITY = f'https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers'
POST = f'https://{LEMMY_EXAMPLE_DOMAIN}/post/777'


@pytest.fixture(autouse=True)
def handler_result_without_sdk(monkeypatch):
    """Load only the public result structure; Discord command SDK is absent."""
    module = ModuleType('src.activitypub_handlers')
    @dataclass
    class HandlerResult:
        """Expose the production handler's stable status/outcome/detail fields."""
        status: str
        outcome: object
        detail: str
    module.HandlerResult = HandlerResult
    monkeypatch.setitem(sys.modules, 'src.activitypub_handlers', module)


def comment(*, object_id='comment/1', parent=POST):
    """Build one remotely authored AP comment with a known canonical parent."""
    now = datetime.now(timezone.utc).isoformat()
    return ActivityPubEvent.model_validate({
        'event_type': 'comment.created', 'delivery_id': 'https://remote.example/create/comment1',
        'occurred_at': now, 'community_actor_id': COMMUNITY,
        'actor_id': f'https://{LEMMY_EXAMPLE_DOMAIN}/u/bob',
        'object': {
            'ap_id': f'https://{LEMMY_EXAMPLE_DOMAIN}/{object_id}', 'kind': 'comment',
            'lemmy_id': 21, 'post_ap_id': POST, 'post_lemmy_id': 777, 'parent_ap_id': parent,
            'title': None, 'body_markdown': 'Reply body',
            'url': f'https://{LEMMY_EXAMPLE_DOMAIN}/{object_id}',
            'published_at': now, 'author_name': 'bob',
        },
    })


def world(tmp_path):
    """Seed one canonical inbound post with two exact forum/thread surfaces."""
    db = build_database(tmp_path, 'inbound-ap-comments.sqlite3')
    for channel in (100, 200):
        add_accepted_subscription(db, channel_id=channel, guild_id=10)
    group = db.discord_fanout_groups.create_thread_group(
        community_actor_id=COMMUNITY,
        source_channel_id=None, source_thread_id=None, source_starter_message_id=None,
        ap_activity_id='https://remote.example/create/post', ap_object_id=POST,
    )
    for forum, thread, starter in ((100, 1100, 2100), (200, 1200, 2200)):
        db.discord_fanout_groups.add_thread_delivery(
            thread_group_id=group.id, discord_channel_id=forum,
            discord_thread_id=thread, discord_starter_message_id=starter, role='inbound',
        )
    return db, group, CommunityRuntime(database=db, content_publish_service=object())


class DiscordMessages:
    """Record each physical send and return the ID actually created there."""

    def __init__(self):
        """Begin with zero physically created Discord messages."""
        self.calls = []

    async def send_message(self, destination, content, *, reply_to, presentation, attempt):
        """Verify parent selection by exposing a real per-container receipt."""
        self.calls.append((destination, content, reply_to, presentation, attempt))
        return CreatedMessage(MessageRef('discord-primary', destination.external_thread_id,
                                         str(5000 + len(self.calls))), 'bot')


@pytest.mark.asyncio
async def test_mapped_inbound_comment_uses_distinct_thread_receipts_and_replay(tmp_path):
    """A post comment creates two replies then replay never repeats the POST."""
    db, thread_group, runtime = world(tmp_path)
    discord = DiscordMessages()
    event = comment()
    result = await runtime.handle_normalized_inbound_comment(event, adapter=discord)
    assert result.status == 'processed'
    mapped = db.discord_fanout_groups.get_message_group_by_ap_object(event.object.ap_id)
    assert mapped.thread_group_id == thread_group.id
    assert [call[0].external_thread_id for call in discord.calls] == ['1100','1200']
    assert all(call[2] is None for call in discord.calls)
    assert {(call[4].key) for call in discord.calls} == {
        f'inbound-ap:comment:{event.object.ap_id}:discord-primary:100:1100',
        f'inbound-ap:comment:{event.object.ap_id}:discord-primary:200:1200',
    }
    assert (await runtime.handle_normalized_inbound_comment(event, adapter=discord)).status == 'skipped'
    assert len(discord.calls) == 2


@pytest.mark.asyncio
async def test_nested_inbound_comment_uses_target_specific_parent_ids(tmp_path):
    """The same canonical reply parent has different physical IDs per forum."""
    db, group, runtime = world(tmp_path)
    parent_group = db.discord_fanout_groups.create_message_group(
        community_actor_id=COMMUNITY, thread_group_id=group.id,
        source_channel_id=None, source_thread_id=None, source_message_id=None,
        ap_activity_id='https://remote.example/create/comment0',
        ap_object_id='https://remote.example/comment/0',
    )
    for channel, thread, msg in ((100, 1100, 8001), (200, 1200, 8002)):
        db.discord_fanout_groups.add_message_delivery(
            message_group_id=parent_group.id, discord_channel_id=channel,
            discord_thread_id=thread, discord_message_id=msg, role='inbound',
            parent_discord_message_id=None,
        )
    discord = DiscordMessages()
    result = await runtime.handle_normalized_inbound_comment(
        comment(parent='https://remote.example/comment/0'), adapter=discord,
    )
    assert result.status == 'processed'
    assert [(c[2].external_container_id, c[2].external_message_id)
            for c in discord.calls] == [('1100', '8001'), ('1200', '8002')]


@pytest.mark.asyncio
async def test_parent_from_foreign_canonical_thread_is_not_referenced(tmp_path):
    """A stored parent from a different canonical post cannot be linked."""
    db, group, runtime = world(tmp_path)
    foreign = db.discord_fanout_groups.create_thread_group(
        community_actor_id=COMMUNITY,
        source_channel_id=None, source_thread_id=None, source_starter_message_id=None,
        ap_activity_id='https://remote.example/other-post', ap_object_id='https://remote.example/post/other',
    )
    db.discord_fanout_groups.create_message_group(
        community_actor_id=COMMUNITY, thread_group_id=foreign.id,
        source_channel_id=None, source_thread_id=None, source_message_id=None,
        ap_activity_id='https://remote.example/create/foreign-comment',
        ap_object_id='https://remote.example/foreign-comment',
    )
    discord = DiscordMessages()
    result = await runtime.handle_normalized_inbound_comment(
        comment(parent='https://remote.example/foreign-comment'), adapter=discord,
    )
    assert result.status == 'skipped'
    assert discord.calls == []


@pytest.mark.asyncio
async def test_unmapped_post_requires_backfill_not_blind_discord_send(tmp_path):
    """An unrecognized post cannot become an arbitrary destination thread."""
    db = build_database(tmp_path, 'inbound-comment-missing-post.sqlite3')
    add_accepted_subscription(db, channel_id=100, guild_id=10)
    runtime = CommunityRuntime(database=db, content_publish_service=object())
    discord = DiscordMessages()
    result = await runtime.handle_normalized_inbound_comment(comment(), adapter=discord)
    assert result.status == 'deferred'
    assert discord.calls == []


@pytest.mark.asyncio
async def test_comment_receipt_from_wrong_thread_never_persists(tmp_path):
    """An adapter returning another thread must not forge a successful copy."""
    db, group, runtime = world(tmp_path)

    class WrongThread(DiscordMessages):
        """Simulate a transport violating the physical destination contract."""

        async def send_message(self, destination, content, *, reply_to, presentation, attempt):
            """Return an untrusted confirmed-looking receipt from another thread."""
            self.calls.append((destination, content, reply_to, presentation, attempt))
            return CreatedMessage(MessageRef('discord-primary', '9000', '9999'), 'bot')

    from src.platform_types import PlatformOperationError
    with pytest.raises(PlatformOperationError, match='receipt thread mismatch'):
        await runtime.handle_normalized_inbound_comment(comment(), adapter=WrongThread())
    message = db.discord_fanout_groups.get_message_group_by_ap_object(comment().object.ap_id)
    assert message is not None
    assert db.discord_fanout_groups.get_message_deliveries(message.id) == []


@pytest.mark.asyncio
async def test_comment_after_undo_follow_skips_target(tmp_path):
    """Revoked subscriber forum receives no new physical comment delivery."""
    db, group, runtime = world(tmp_path)
    db.remote_subscriptions.delete_subscription(200)
    discord = DiscordMessages()
    result = await runtime.handle_normalized_inbound_comment(comment(), adapter=discord)
    assert result.status == 'processed'
    assert [call[0].channel.external_channel_id for call in discord.calls] == ['100']
