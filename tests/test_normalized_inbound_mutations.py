"""Inbound AP mutation scenarios through the actual canonical Community runtime.

Real SQLite owns canonical groups and physical deliveries. Only Discord I/O and
its SDK-dependent handler-result import are replaced at the outer boundary.
"""
from __future__ import annotations

from dataclasses import dataclass
from types import ModuleType, SimpleNamespace
import sys

import pytest

# The legacy module imports discord.py for type-only paths; no physical SDK is used here.
import discord  # Load the installed SDK; never leave a global placeholder for other suites.
from src.community_sync.runtime import CommunityRuntime
from src.platform_types import ContentDocument, MessageRef, MessageView, PlatformOperationError
from support.db import add_accepted_subscription, build_database
from tests_constants import LEMMY_EXAMPLE_DOMAIN

COMMUNITY = f'https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers'
POST = f'https://{LEMMY_EXAMPLE_DOMAIN}/post/mutation'
COMMENT = f'https://{LEMMY_EXAMPLE_DOMAIN}/comment/mutation'


@pytest.fixture(autouse=True)
def result_without_discord_sdk(monkeypatch):
    """Keep the SDK at the import boundary, not in domain/SQLite tests."""
    module = ModuleType('src.activitypub_handlers')

    @dataclass
    class HandlerResult:
        """Expose the same status, outcome and detail fields as production."""
        status: str
        outcome: object
        detail: str

    module.HandlerResult = HandlerResult
    monkeypatch.setitem(sys.modules, 'src.activitypub_handlers', module)


class PhysicalEdits:
    """Model known physical Discord messages with mutable content and failures."""

    def __init__(self, *, failed_container=None):
        """Create two forum starter/message copies with independent bodies."""
        self.contents = {
            ('1100', '2100'): 'old starter A', ('1200', '2200'): 'old starter B',
            ('1100', '3100'): '`bob`\n\nold A', ('1200', '3200'): '`bob`\n\nold B',
            ('1300', '3300'): 'source authored',
        }
        self.failed_container = failed_container
        self.fetched = []
        self.edited = []

    async def fetch_message(self, ref):
        """Read only exact, recognized scoped Discord message identities."""
        self.fetched.append(ref)
        if ref.connector_key != 'discord-primary' or (ref.external_container_id, ref.external_message_id) not in self.contents:
            raise PlatformOperationError('not_found', 'unknown message')
        if ref.external_container_id == self.failed_container:
            raise PlatformOperationError('unavailable', 'temporary Discord failure')
        return MessageView(ref, content=ContentDocument(self.contents[(ref.external_container_id, ref.external_message_id)]))

    async def edit_message(self, ref, content):
        """Record physical edits after optional transport failure."""
        if ref.external_container_id == self.failed_container:
            raise PlatformOperationError('unavailable', 'temporary Discord failure')
        assert ref.connector_key == 'discord-primary'
        self.edited.append((ref, content.body))
        self.contents[(ref.external_container_id, ref.external_message_id)] = content.body


def world(tmp_path):
    """Create canonical post/comment groups across two accepted Discord forums."""
    db = build_database(tmp_path, 'inbound-mutations.sqlite3')
    for channel in (100, 200, 300):
        add_accepted_subscription(db, channel_id=channel, guild_id=10)
    thread_group = db.discord_fanout_groups.create_thread_group(
        community_actor_id=COMMUNITY, source_channel_id=None,
        source_thread_id=None, source_starter_message_id=None,
        ap_activity_id='https://remote.example/create/post', ap_object_id=POST,
    )
    for channel, thread, starter in ((100, 1100, 2100), (200, 1200, 2200), (300, 1300, 2300)):
        db.discord_fanout_groups.add_thread_delivery(
            thread_group_id=thread_group.id, discord_channel_id=channel,
            discord_thread_id=thread, discord_starter_message_id=starter, role='inbound',
        )
    message_group = db.discord_fanout_groups.create_message_group(
        community_actor_id=COMMUNITY, thread_group_id=thread_group.id,
        source_channel_id=None, source_thread_id=None, source_message_id=None,
        ap_activity_id='https://remote.example/create/comment', ap_object_id=COMMENT,
    )
    for channel, thread, message, role in (
        (100, 1100, 3100, 'inbound'), (200, 1200, 3200, 'mirror'),
        (300, 1300, 3300, 'source'),
    ):
        db.discord_fanout_groups.add_message_delivery(
            message_group_id=message_group.id, discord_channel_id=channel,
            discord_thread_id=thread, discord_message_id=message, role=role,
        )
    runtime = CommunityRuntime(database=db, content_publish_service=object())
    physical = PhysicalEdits()
    # Legacy bot is deliberately unusable: production must use adapter operations.
    context = SimpleNamespace(discord_adapter=physical, bot=object())
    return runtime, physical, context


def event(ap_id, body='New body'):
    """Supply the original AP object identity and content to the runtime."""
    return SimpleNamespace(object=SimpleNamespace(ap_id=ap_id, body_markdown=body))


@pytest.mark.asyncio
async def test_inbound_post_update_uses_scoped_adaptor_edits(tmp_path):
    """One AP post Update edits two starter messages, not the containing threads."""
    runtime, adapter, context = world(tmp_path)
    result = await runtime.handle_inbound_post_update(event(POST), context)
    assert result.status == 'processed'
    assert {(r.external_container_id, r.external_message_id) for r, _ in adapter.edited} == {
        ('1100', '2100'), ('1200', '2200'), ('1300', '2300'),
    }
    assert all(body == 'New body' for _, body in adapter.edited)


@pytest.mark.asyncio
async def test_inbound_comment_update_preserves_header_without_touching_source(tmp_path):
    """Update retains each bot-owned attribution line and never edits source."""
    runtime, adapter, context = world(tmp_path)
    result = await runtime.handle_inbound_comment_update(event(COMMENT), context)
    assert result.status == 'processed'
    assert {(r.external_container_id, r.external_message_id) for r, _ in adapter.edited} == {
        ('1100', '3100'), ('1200', '3200'),
    }
    assert all(body == '`bob`\n\nNew body' for _, body in adapter.edited)
    assert adapter.contents[('1300', '3300')] == 'source authored'


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['post', 'comment'])
async def test_delete_marks_mirrors_without_physically_deleting(kind, tmp_path):
    """AP Delete edits physical body to a tombstone; no thread/message removal."""
    runtime, adapter, context = world(tmp_path)
    result = (await runtime.handle_inbound_post_delete(event(POST), context)
              if kind == 'post' else await runtime.handle_inbound_comment_delete(event(COMMENT), context))
    assert result.status == 'processed'
    assert len(adapter.edited) == (3 if kind == 'post' else 2)
    assert all(body == '*deleted by creator*' for _, body in adapter.edited)


@pytest.mark.asyncio
async def test_one_failed_discord_target_does_not_block_healthy_target(tmp_path):
    """Transient error at one target does not suppress another physical edit."""
    runtime, adapter, context = world(tmp_path)
    adapter.failed_container = '1100'
    result = await runtime.handle_inbound_post_update(event(POST), context)
    assert result.status == 'processed'
    assert [(r.external_container_id, r.external_message_id) for r, _ in adapter.edited] == [('1200', '2200'), ('1300', '2300')]


@pytest.mark.asyncio
async def test_unmapped_mutation_does_not_reach_platform(tmp_path):
    """Unknown canonical AP objects cannot trigger physical edits by guessed IDs."""
    runtime, adapter, context = world(tmp_path)
    result = await runtime.handle_inbound_comment_update(event('https://other.example/missing'), context)
    assert result.status == 'skipped'
    assert adapter.fetched == []
    assert adapter.edited == []

@pytest.mark.asyncio
async def test_unverified_fetched_comment_does_not_erase_author_header(tmp_path):
    """A wrong physical message read cannot cause a destructive body overwrite."""
    runtime, adapter, context = world(tmp_path)
    original = adapter.fetch_message

    async def wrong_receipt(ref):
        """Simulate a transport returning a message from an unrelated thread."""
        if ref.external_container_id == '1100':
            return MessageView(MessageRef('discord-primary', '9999', '3100'),
                               content=ContentDocument('`other`\n\nforeign'))
        return await original(ref)

    adapter.fetch_message = wrong_receipt
    result = await runtime.handle_inbound_comment_update(event(COMMENT), context)
    assert result.status == 'processed'
    assert adapter.edited == [(MessageRef('discord-primary', '1200', '3200'),
                               '`bob`\n\nNew body')]
    assert adapter.contents[('1100', '3100')] == '`bob`\n\nold A'
