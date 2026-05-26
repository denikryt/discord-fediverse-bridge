"""Stage 3 scenarios for one-way local-subscriber Discord fanout."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from src.activitypub_models import ActivityPubEvent
from src.discord_event_router import DiscordEventRouter
from src.discord_publish_service import ContentPublishService
from src.fedify_gateway_client import PublishLocalCommunityContentResult
from src.local_communities.runtime import LocalCommunityRuntime
from src.local_communities.service import LocalCommunityService
from src.models import LocalCommunityMessage, LocalCommunityMessageSurface, LocalCommunityThread, LocalCommunityThreadSurface
from support.db import add_registered_user, build_database
from support.discord import (
    build_bot,
    build_forum_channel_object_result,
    build_forum_channel_tuple_result,
    build_send_thread,
    build_starter_message,
    build_thread,
    build_thread_message,
)


def _runtime(tmp_path: Path) -> tuple[object, LocalCommunityRuntime]:
    """Build a local-community runtime with real persistence and fake gateways."""
    database = build_database(tmp_path, "stage3-local-subscribers.db")
    gateway = AsyncMock()
    publish_service = ContentPublishService(
        database=database,
        fedify_gateway=gateway,
        bridge_prefix="[bridge]",
    )
    runtime = LocalCommunityRuntime(
        database=database,
        fedify_gateway=gateway,
        content_publish_service=publish_service,
        bridge_prefix="[bridge]",
    )
    return database, runtime


def _local_community(database: object) -> object:
    """Seed one bridge-owned local community and return its row."""
    LocalCommunityService(
        database=database,
        base_url="https://bridge.example",
        keypair_generator=lambda: ("public-key", "private-key"),
    ).create_local_community(
        discord_guild_id=10,
        discord_forum_channel_id=100,
        slug="hackers",
        name="Hackers",
        description="A local hackerspace forum.",
    )
    return database.get_local_community_by_slug("hackers")


def _add_local_subscribers(database: object, local_community: object) -> None:
    """Create two active local subscriber forums for Stage 3 fanout tests."""
    database.create_local_subscriber(
        local_community_id=local_community.id,
        discord_guild_id=10,
        discord_channel_id=200,
        initiated_by_discord_user_id="999",
        status="active",
    )
    database.create_local_subscriber(
        local_community_id=local_community.id,
        discord_guild_id=10,
        discord_channel_id=300,
        initiated_by_discord_user_id="999",
        status="active",
    )


def _post_event(*, suffix: str = "1", actor_id: str = "https://lemmy.example/u/bob") -> ActivityPubEvent:
    """Build one inbound remote post event targeting the local community."""
    return ActivityPubEvent.model_validate(
        {
            "event_type": "post.created",
            "delivery_id": f"https://lemmy.example/activities/create/post/{suffix}",
            "source_activity_json": {
                "type": "Create",
                "id": f"https://lemmy.example/activities/create/post/{suffix}",
                "actor": actor_id,
                "object": {"type": "Page", "id": f"https://lemmy.example/post/{suffix}"},
            },
            "source_activity_id": f"https://lemmy.example/activities/create/post/{suffix}",
            "source_announce_id": None,
            "occurred_at": "2026-05-19T10:00:00Z",
            "community_actor_id": "https://bridge.example/communities/hackers",
            "actor_id": actor_id,
            "object": {
                "ap_id": f"https://lemmy.example/post/{suffix}",
                "kind": "post",
                "lemmy_id": 1,
                "post_ap_id": None,
                "post_lemmy_id": None,
                "parent_ap_id": None,
                "title": "Remote topic",
                "body_markdown": "hello from lemmy",
                "url": f"https://lemmy.example/post/{suffix}",
                "published_at": "2026-05-19T10:00:00Z",
                "author_name": "bob",
            },
        }
    )


def _comment_event(*, suffix: str = "1", parent_ap_id: str = "https://lemmy.example/post/1") -> ActivityPubEvent:
    """Build one inbound remote comment event targeting the local community."""
    return ActivityPubEvent.model_validate(
        {
            "event_type": "comment.created",
            "delivery_id": f"https://lemmy.example/activities/create/comment/{suffix}",
            "source_activity_json": {
                "type": "Create",
                "id": f"https://lemmy.example/activities/create/comment/{suffix}",
                "actor": "https://lemmy.example/u/bob",
                "object": {"type": "Note", "id": f"https://lemmy.example/comment/{suffix}"},
            },
            "source_activity_id": f"https://lemmy.example/activities/create/comment/{suffix}",
            "source_announce_id": None,
            "occurred_at": "2026-05-19T10:05:00Z",
            "community_actor_id": "https://bridge.example/communities/hackers",
            "actor_id": "https://lemmy.example/u/bob",
            "object": {
                "ap_id": f"https://lemmy.example/comment/{suffix}",
                "kind": "comment",
                "lemmy_id": 2,
                "post_ap_id": "https://lemmy.example/post/1",
                "post_lemmy_id": 1,
                "parent_ap_id": parent_ap_id,
                "title": None,
                "body_markdown": "hello comment",
                "url": f"https://lemmy.example/comment/{suffix}",
                "published_at": "2026-05-19T10:05:00Z",
                "author_name": "bob",
            },
        }
    )


@pytest.mark.asyncio
async def test_host_thread_create_fans_out_to_local_subscriber_thread_surfaces(tmp_path: Path) -> None:
    """Host forum posts should create one subscriber thread surface per active local subscriber."""
    database, runtime = _runtime(tmp_path)
    local_community = _local_community(database)
    _add_local_subscribers(database, local_community)
    add_registered_user(database)
    runtime.bot = build_bot(
        forum_channels={
            200: build_forum_channel_object_result(channel_id=200, thread_id=2200, starter_message_id=2300),
            300: build_forum_channel_object_result(channel_id=300, thread_id=3300, starter_message_id=3400),
        }
    )
    runtime.fedify_gateway.publish_local_community_content.return_value = PublishLocalCommunityContentResult(
        activity_id="https://bridge.example/users/alice/activities/create/post/1",
        object_id="https://bridge.example/users/alice/post/1",
        community_actor_url=local_community.actor_url,
        delivered_follower_count=0,
        failed_follower_count=0,
    )

    await runtime.handle_discord_thread_create(
        thread=build_thread(thread_id=1100, channel_id=100, name="Host topic"),
        starter_message=build_starter_message(message_id=1200, content="host body"),
    )

    surfaces = database.list_local_community_thread_surfaces(
        database.get_local_community_thread_by_ap_object_id("https://bridge.example/users/alice/post/1").id
    )
    assert [(s.discord_forum_channel_id, s.role) for s in surfaces] == [
        (100, "host"),
        (200, "local_subscriber"),
        (300, "local_subscriber"),
    ]
    assert all(s.local_subscriber_id is not None for s in surfaces if s.role == "local_subscriber")


@pytest.mark.asyncio
async def test_host_root_and_nested_replies_use_surface_local_parent_ids(tmp_path: Path) -> None:
    """Subscriber replies should target starter or parent message ids on each target surface."""
    database, runtime = _runtime(tmp_path)
    local_community = _local_community(database)
    _add_local_subscribers(database, local_community)
    add_registered_user(database)
    thread = build_thread(thread_id=1100, channel_id=100, name="Host topic")
    subscriber_thread_200 = build_send_thread(thread_id=2200, sent_message_id=2400)
    subscriber_thread_300 = build_send_thread(thread_id=3300, sent_message_id=3500)
    runtime.bot = build_bot(
        forum_channels={
            200: build_forum_channel_object_result(channel_id=200, thread_id=2200, starter_message_id=2300),
            300: build_forum_channel_object_result(channel_id=300, thread_id=3300, starter_message_id=3400),
        },
        threads={
            2200: subscriber_thread_200,
            3300: subscriber_thread_300,
        },
    )
    runtime.fedify_gateway.publish_local_community_content.side_effect = [
        PublishLocalCommunityContentResult("a-post", "o-post", local_community.actor_url, 0, 0),
        PublishLocalCommunityContentResult("a-comment-1", "o-comment-1", local_community.actor_url, 0, 0),
        PublishLocalCommunityContentResult("a-comment-2", "o-comment-2", local_community.actor_url, 0, 0),
    ]
    await runtime.handle_discord_thread_create(thread=thread, starter_message=build_starter_message(message_id=1200))
    await runtime.handle_discord_message(message=build_thread_message(message_id=1300, thread_id=1100, channel_id=100))
    subscriber_thread_200.send.return_value = SimpleNamespace(id=2401)
    subscriber_thread_300.send.return_value = SimpleNamespace(id=3501)

    await runtime.handle_discord_message(
        message=build_thread_message(message_id=1301, thread_id=1100, channel_id=100, reference_message_id=1300)
    )

    first_200 = database.get_local_community_message_surface_by_discord_message_id(2400)
    nested_200 = database.get_local_community_message_surface_by_discord_message_id(2401)
    first_300 = database.get_local_community_message_surface_by_discord_message_id(3500)
    nested_300 = database.get_local_community_message_surface_by_discord_message_id(3501)
    assert first_200.parent_discord_message_id == 2300
    assert first_300.parent_discord_message_id == 3400
    assert nested_200.parent_discord_message_id == 2400
    assert nested_300.parent_discord_message_id == 3500


@pytest.mark.asyncio
async def test_inbound_remote_post_fans_out_locally_and_still_relays_remotely(tmp_path: Path) -> None:
    """Remote-origin posts should create subscriber surfaces without blocking remote relay."""
    database, runtime = _runtime(tmp_path)
    local_community = _local_community(database)
    _add_local_subscribers(database, local_community)
    for name in ["bob", "alice"]:
        database.create_local_community_follower(
            local_community_id=local_community.id,
            remote_actor_id=f"https://lemmy.example/u/{name}",
            remote_inbox_url=f"https://lemmy.example/u/{name}/inbox",
            follow_activity_id=f"https://lemmy.example/follow/{name}",
        )
    runtime.bot = build_bot(
        forum_channels={
            100: build_forum_channel_tuple_result(channel_id=100, thread_id=1100, starter_message_id=1200),
            200: build_forum_channel_object_result(channel_id=200, thread_id=2200, starter_message_id=2300),
            300: build_forum_channel_object_result(channel_id=300, thread_id=3300, starter_message_id=3400),
        }
    )

    result = await runtime.handle_inbound_post(_post_event(), SimpleNamespace())
    thread_row = database.get_local_community_thread_by_ap_object_id("https://lemmy.example/post/1")

    assert result.status == "processed"
    assert len(database.list_local_community_thread_surfaces(thread_row.id)) == 3
    assert runtime.fedify_gateway.send_local_community_relay.await_count == 1


@pytest.mark.asyncio
async def test_inbound_remote_comment_creates_local_subscriber_message_surfaces(tmp_path: Path) -> None:
    """Remote-origin comments should create message surfaces under each subscriber thread surface."""
    database, runtime = _runtime(tmp_path)
    local_community = _local_community(database)
    _add_local_subscribers(database, local_community)
    database.create_local_community_follower(
        local_community_id=local_community.id,
        remote_actor_id="https://lemmy.example/u/bob",
        remote_inbox_url="https://lemmy.example/u/bob/inbox",
        follow_activity_id="https://lemmy.example/follow/bob",
    )
    thread_row = database.create_local_community_thread(
        local_community_id=local_community.id,
        discord_thread_id=1100,
        discord_starter_message_id=1200,
        ap_activity_id="a-post",
        ap_object_id="https://lemmy.example/post/1",
        direction="ap_to_discord",
        origin_kind="remote_follower",
    )
    subscriber_200 = database.get_local_subscriber_by_channel(200)
    subscriber_300 = database.get_local_subscriber_by_channel(300)
    database.create_local_community_thread_surface(
        local_community_thread_id=thread_row.id,
        discord_forum_channel_id=200,
        discord_thread_id=2200,
        discord_starter_message_id=2300,
        role="local_subscriber",
        local_subscriber_id=subscriber_200.id,
    )
    database.create_local_community_thread_surface(
        local_community_thread_id=thread_row.id,
        discord_forum_channel_id=300,
        discord_thread_id=3300,
        discord_starter_message_id=3400,
        role="local_subscriber",
        local_subscriber_id=subscriber_300.id,
    )
    runtime.bot = build_bot(
        threads={
            1100: build_send_thread(thread_id=1100, sent_message_id=1300),
            2200: build_send_thread(thread_id=2200, sent_message_id=2400),
            3300: build_send_thread(thread_id=3300, sent_message_id=3500),
        }
    )

    result = await runtime.handle_inbound_comment(_comment_event(), SimpleNamespace())
    message_row = database.get_local_community_message_by_ap_object_id("https://lemmy.example/comment/1")

    assert result.status == "processed"
    surfaces = database.list_local_community_message_surfaces(message_row.id)
    assert [(surface.discord_forum_channel_id, surface.role) for surface in surfaces] == [
        (100, "host"),
        (200, "local_subscriber"),
        (300, "local_subscriber"),
    ]
    assert database.get_local_community_message_surface_by_discord_message_id(2400).parent_discord_message_id == 2300
    assert database.get_local_community_message_surface_by_discord_message_id(3500).parent_discord_message_id == 3400


@pytest.mark.asyncio
async def test_partial_local_fanout_failure_allows_healthy_subscriber_surface(tmp_path: Path) -> None:
    """One broken local subscriber forum must not block other local subscriber targets."""
    database, runtime = _runtime(tmp_path)
    local_community = _local_community(database)
    _add_local_subscribers(database, local_community)
    add_registered_user(database)
    failing_forum = build_forum_channel_object_result(channel_id=200, thread_id=2200, starter_message_id=2300)
    failing_forum.create_thread.side_effect = RuntimeError("forum gone")
    runtime.bot = build_bot(
        forum_channels={
            200: failing_forum,
            300: build_forum_channel_object_result(channel_id=300, thread_id=3300, starter_message_id=3400),
        }
    )
    runtime.fedify_gateway.publish_local_community_content.return_value = PublishLocalCommunityContentResult(
        "a-post", "o-post", local_community.actor_url, 0, 0
    )

    await runtime.handle_discord_thread_create(thread=build_thread(thread_id=1100, channel_id=100), starter_message=build_starter_message(message_id=1200))
    thread_row = database.get_local_community_thread_by_ap_object_id("o-post")

    assert database.get_local_community_thread_surface(local_community_thread_id=thread_row.id, discord_forum_channel_id=200) is None
    assert database.get_local_community_thread_surface(local_community_thread_id=thread_row.id, discord_forum_channel_id=300) is not None


@pytest.mark.asyncio
async def test_duplicate_source_processing_retries_missing_surfaces_only(tmp_path: Path) -> None:
    """Duplicate source processing should fill missing surfaces without new canonical rows."""
    database, runtime = _runtime(tmp_path)
    local_community = _local_community(database)
    _add_local_subscribers(database, local_community)
    add_registered_user(database)
    failing_forum = build_forum_channel_object_result(channel_id=200, thread_id=2200, starter_message_id=2300)
    failing_forum.create_thread.side_effect = RuntimeError("first attempt fails")
    healthy_200 = build_forum_channel_object_result(channel_id=200, thread_id=2200, starter_message_id=2300)
    runtime.bot = build_bot(
        forum_channels={
            200: failing_forum,
            300: build_forum_channel_object_result(channel_id=300, thread_id=3300, starter_message_id=3400),
        }
    )
    runtime.fedify_gateway.publish_local_community_content.return_value = PublishLocalCommunityContentResult(
        "a-post", "o-post", local_community.actor_url, 0, 0
    )
    thread = build_thread(thread_id=1100, channel_id=100)
    starter = build_starter_message(message_id=1200)
    await runtime.handle_discord_thread_create(thread=thread, starter_message=starter)
    runtime.bot = build_bot(forum_channels={200: healthy_200, 300: runtime.bot.fetch_forum_channel})
    runtime.bot = build_bot(
        forum_channels={
            200: healthy_200,
            300: build_forum_channel_object_result(channel_id=300, thread_id=3301, starter_message_id=3401),
        }
    )

    await runtime.handle_discord_thread_create(thread=thread, starter_message=starter)

    with database.session() as session:
        assert session.scalar(select(func.count()).select_from(LocalCommunityThread)) == 1
        assert session.scalar(select(func.count()).select_from(LocalCommunityThreadSurface)) == 3
    thread_row = database.get_local_community_thread_by_ap_object_id("o-post")
    assert database.get_local_community_thread_surface(local_community_thread_id=thread_row.id, discord_forum_channel_id=200) is not None


@pytest.mark.asyncio
async def test_local_subscriber_forum_creates_are_not_local_community_sources(tmp_path: Path) -> None:
    """Stage 3 must not route subscriber forum creates into LocalCommunityRuntime."""
    database, local_runtime = _runtime(tmp_path)
    local_community = _local_community(database)
    database.create_local_subscriber(local_community_id=local_community.id, discord_guild_id=10, discord_channel_id=200, initiated_by_discord_user_id="999")
    remote_runtime = AsyncMock()
    router = DiscordEventRouter(database=database, community_runtime=remote_runtime, local_community_runtime=local_runtime)
    local_runtime.handle_discord_thread_create = AsyncMock()

    await router.handle_thread_create(thread=build_thread(thread_id=2200, channel_id=200), starter_message=build_starter_message())

    local_runtime.handle_discord_thread_create.assert_not_awaited()
    remote_runtime.handle_discord_thread_create.assert_awaited_once()

    local_runtime.handle_discord_message = AsyncMock()
    await router.handle_message(message=build_thread_message(message_id=2400, thread_id=2200, channel_id=200))
    local_runtime.handle_discord_message.assert_not_awaited()
    remote_runtime.handle_discord_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_local_subscriber_mirror_edit_delete_is_contained(tmp_path: Path) -> None:
    """Stage 3 mirrored subscriber surfaces must not publish AP edit/delete."""
    database, runtime = _runtime(tmp_path)
    local_community = _local_community(database)
    thread_row = database.create_local_community_thread(
        local_community_id=local_community.id,
        discord_thread_id=1100,
        discord_starter_message_id=1200,
        ap_activity_id="a-post",
        ap_object_id="o-post",
        direction="discord_to_ap",
        origin_kind="discord_local",
    )
    subscriber = database.create_local_subscriber(local_community_id=local_community.id, discord_guild_id=10, discord_channel_id=200, initiated_by_discord_user_id="999")
    database.create_local_community_thread_surface(
        local_community_thread_id=thread_row.id,
        discord_forum_channel_id=200,
        discord_thread_id=2200,
        discord_starter_message_id=2300,
        role="local_subscriber",
        local_subscriber_id=subscriber.id,
    )
    database.create_published_activity_object(
        actor_username="alice",
        actor_url="https://bridge.example/actors/alice",
        community_actor_url=local_community.actor_url,
        activity_id="a-post",
        object_id="o-post",
        kind="post",
        title="Title",
        body_markdown="Body",
        in_reply_to_object_id=None,
        discord_channel_id=2200,
        discord_message_id=2300,
    )

    await runtime.handle_discord_message_edit(message_id=2300, new_content="edited", runtime=SimpleNamespace(fedify_gateway=runtime.fedify_gateway))
    await runtime.handle_discord_message_delete(message_id=2300, runtime=SimpleNamespace(fedify_gateway=runtime.fedify_gateway))

    runtime.fedify_gateway.update_content.assert_not_awaited()
    runtime.fedify_gateway.delete_content.assert_not_awaited()
