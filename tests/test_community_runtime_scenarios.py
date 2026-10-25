"""Scenario tests for CommunityRuntime Phase 0 routing boundaries.

Each test verifies that CommunityRuntime preserves the observable effects of
the existing bridge logic when called through the new entry points. All four
tests run through real DB, real CommunityRuntime, and real handler logic.
Only outer boundaries are mocked: FedifyGatewayClient and Discord SDK calls.
"""

from __future__ import annotations
from support.runtime import build_test_policy_service

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.fedify_gateway_client import PublishContentResult
from support.activitypub import build_comment_created_event, build_post_created_event
from support.db import add_accepted_subscription, add_registered_user, build_database
from support.discord import build_starter_message, build_thread, build_thread_message
from support.runtime import build_community_runtime
from tests_constants import BRIDGE_HOST_DOMAIN, LEMMY_EXAMPLE_DOMAIN


@pytest.mark.asyncio
async def test_community_runtime_thread_create_publishes_and_persists(tmp_path):
    """One verified forum starter persists exactly one canonical AP source graph."""
    from test_normalized_community_runtime import starter, world

    database, runtime, gateway, adapter = world(tmp_path, forum_ids=(100,))
    try:
        event, view = starter()
        result = await runtime.handle_normalized_thread_create(event, view, adapter=adapter)
        group = database.discord_fanout_groups.get_thread_group_by_source_thread(200)
        mapping = database.message_mappings.get_by_source_ref(event.starter)
        published = database.activitypub_objects.get_published_activity_object_by_object_id(
            result.object_id)
        assert result.status == 'published' and group is not None
        assert group.ap_object_id == result.object_id
        assert mapping is not None and mapping.activity_id == result.activity_id
        assert published is not None and published.kind == 'post'
        assert published.in_reply_to_object_id is None
        assert len(gateway.requests) == 1 and adapter.posts == []
    finally:
        database.close()


@pytest.mark.asyncio
async def test_community_runtime_thread_message_publishes_as_comment(tmp_path):
    """A verified reply publishes one Note with the existing canonical post parent."""
    from test_normalized_community_runtime import reply, starter, world

    database, runtime, gateway, adapter = world(tmp_path, forum_ids=(100,))
    try:
        event, view = starter()
        post = await runtime.handle_normalized_thread_create(event, view, adapter=adapter)
        comment = reply(message='301', parent='300')
        result = await runtime.handle_normalized_message_create(comment, adapter=adapter)
        group = database.discord_fanout_groups.get_message_group_by_source_message(301)
        mapping = database.message_mappings.get_by_source_ref(comment.message)
        published = database.activitypub_objects.get_published_activity_object_by_object_id(
            result.object_id)
        assert result.status == 'published' and group is not None
        assert group.ap_object_id == result.object_id
        assert mapping is not None and mapping.object_id == result.object_id
        assert published is not None and published.kind == 'comment'
        assert published.in_reply_to_object_id == post.object_id
        assert len(gateway.requests) == 2 and adapter.replies == []
    finally:
        database.close()


@pytest.fixture(autouse=True)
def result_without_discord_commands(monkeypatch):
    """Use the stable inbound result shape without loading Discord command SDK."""
    from dataclasses import dataclass
    from types import ModuleType
    import sys

    module = ModuleType('src.activitypub_handlers')

    @dataclass
    class HandlerResult:
        """Preserve the actual result fields used by inbound scenario assertions."""
        status: str
        outcome: object
        detail: str

    module.HandlerResult = HandlerResult
    monkeypatch.setitem(sys.modules, 'src.activitypub_handlers', module)


@pytest.mark.asyncio
async def test_community_runtime_inbound_post_creates_discord_thread(tmp_path):
    """A remote AP post persists one canonical group and one typed physical receipt."""
    from test_normalized_inbound_remote_posts import RecordingDiscord

    database = build_database(tmp_path, 'community-runtime.db')
    try:
        add_accepted_subscription(database, guild_id=10)
        adapter = RecordingDiscord()
        runtime = build_community_runtime(database, fedify_gateway=AsyncMock())
        event = build_post_created_event(object_id=f'https://{LEMMY_EXAMPLE_DOMAIN}/post/99')
        result = await runtime.handle_normalized_inbound_post(event, adapter=adapter)
        group = database.discord_fanout_groups.get_thread_group_by_ap_object(event.object.ap_id)
        assert result.status == 'processed' and group is not None
        assert len(database.discord_fanout_groups.get_thread_deliveries(group.id)) == 1
        assert len(adapter.calls) == 1
    finally:
        database.close()


@pytest.mark.asyncio
async def test_community_runtime_inbound_comment_creates_discord_message(tmp_path):
    """An AP comment reuses the exact confirmed thread, not the parent forum ID."""
    from test_normalized_inbound_remote_posts import RecordingDiscord
    from test_normalized_inbound_remote_comments import DiscordMessages

    database = build_database(tmp_path, 'community-runtime.db')
    try:
        add_accepted_subscription(database, guild_id=10)
        runtime = build_community_runtime(database, fedify_gateway=AsyncMock())
        parent = build_post_created_event(object_id=f'https://{LEMMY_EXAMPLE_DOMAIN}/post/99')
        post_adapter = RecordingDiscord()
        assert (await runtime.handle_normalized_inbound_post(parent, adapter=post_adapter)).status == 'processed'
        comment = build_comment_created_event(
            object_id=f'https://{LEMMY_EXAMPLE_DOMAIN}/comment/55',
            post_ap_id=parent.object.ap_id,
        )
        adapter = DiscordMessages()
        result = await runtime.handle_normalized_inbound_comment(comment, adapter=adapter)
        group = database.discord_fanout_groups.get_message_group_by_ap_object(comment.object.ap_id)
        assert result.status == 'processed' and group is not None
        assert group.ap_object_id == comment.object.ap_id
        assert len(adapter.calls) == 1
        assert adapter.calls[0][0].external_thread_id == '1100'
    finally:
        database.close()
