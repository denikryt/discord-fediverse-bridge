"""Stage 4 scenarios for local-subscriber-originated local-community creates."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from src.discord_event_router import DiscordEventRouter
from src.content_publish_service import ContentPublishService
from src.fedify_gateway_client import PublishLocalCommunityContentResult
from src.local_communities.runtime import LocalCommunityRuntime
from src.local_communities.service import LocalCommunityService
from src.models import LocalCommunityMessage, LocalCommunityMessageSurface, LocalCommunityThread, LocalCommunityThreadSurface
from support.db import add_accepted_subscription, add_registered_user, build_database
from support.discord import (
    build_bot,
    build_forum_channel_object_result,
    build_send_thread,
    build_starter_message,
    build_thread,
    build_thread_message,
)


def _runtime(tmp_path: Path) -> tuple[object, LocalCommunityRuntime]:
    """Build a Stage 4 runtime with real persistence and fake outer services."""
    database = build_database(tmp_path, "stage4-local-subscriber-origin.db")
    gateway = AsyncMock()
    publish_service = ContentPublishService(database=database, fedify_gateway=gateway, bridge_prefix="[bridge]")
    runtime = LocalCommunityRuntime(
        database=database,
        fedify_gateway=gateway,
        content_publish_service=publish_service,
        bridge_prefix="[bridge]",
    )
    return database, runtime


def _local_community(database: object) -> object:
    """Seed one bridge-owned local community anchored at host forum 100."""
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
    return database.local_communities.get_local_community_by_slug("hackers")


def _add_local_subscribers(database: object, local_community: object) -> tuple[object, object]:
    """Create the source and sibling local subscriber rows used by tests."""
    source = database.local_subscribers.create_local_subscriber(
        local_community_id=local_community.id,
        discord_guild_id=10,
        discord_channel_id=200,
        initiated_by_discord_user_id="999",
        status="active",
    )
    sibling = database.local_subscribers.create_local_subscriber(
        local_community_id=local_community.id,
        discord_guild_id=10,
        discord_channel_id=300,
        initiated_by_discord_user_id="999",
        status="active",
    )
    return source, sibling


def _publish_results(*pairs: tuple[str, str]) -> list[PublishLocalCommunityContentResult]:
    """Build gateway publish results for deterministic AP ids in scenarios."""
    return [PublishLocalCommunityContentResult(a, o, "https://bridge.example/communities/hackers", 0, 0) for a, o in pairs]


def _canonical_thread(database: object, local_community: object, *, activity: str = "a-post", obj: str = "o-post") -> object:
    """Create one canonical Stage 4 thread row without a host surface."""
    return database.local_community_content.create_local_community_thread_canonical(
        local_community_id=local_community.id,
        ap_activity_id=activity,
        ap_object_id=obj,
        direction="discord_to_ap",
        origin_kind="discord_local_subscriber",
    )


def _thread_surface(database: object, thread_row: object, forum: int, thread: int, starter: int, role: str, subscriber_id: int | None) -> object:
    """Create one explicit thread surface using the keyword-only repository API."""
    return database.local_community_surfaces.create_local_community_thread_surface(
        local_community_thread_id=thread_row.id,
        discord_forum_channel_id=forum,
        discord_thread_id=thread,
        discord_starter_message_id=starter,
        role=role,
        local_subscriber_id=subscriber_id,
    )


def _canonical_message(database: object, thread_row: object, *, activity: str, obj: str, parent: str) -> object:
    """Create one canonical Stage 4 message row without a host surface."""
    return database.local_community_content.create_local_community_message_canonical(
        local_community_thread_id=thread_row.id,
        ap_activity_id=activity,
        ap_object_id=obj,
        parent_ap_object_id=parent,
        direction="discord_to_ap",
    )


def _message_surface(database: object, message_row: object, thread_surface: object, forum: int, message: int, parent: int, role: str, subscriber_id: int | None) -> object:
    """Create one explicit message surface using the keyword-only repository API."""
    return database.local_community_surfaces.create_local_community_message_surface(
        local_community_message_id=message_row.id,
        local_community_thread_surface_id=thread_surface.id,
        discord_forum_channel_id=forum,
        discord_message_id=message,
        parent_discord_message_id=parent,
        role=role,
        local_subscriber_id=subscriber_id,
    )


@pytest.mark.asyncio
async def test_local_subscriber_thread_create_creates_source_host_and_sibling_surfaces(tmp_path: Path) -> None:
    """A local subscriber forum thread should become one canonical community post."""
    database, runtime = _runtime(tmp_path)
    local_community = _local_community(database)
    source, sibling = _add_local_subscribers(database, local_community)
    add_registered_user(database)
    runtime.bot = build_bot(
        forum_channels={
            100: build_forum_channel_object_result(channel_id=100, thread_id=1100, starter_message_id=1200),
            300: build_forum_channel_object_result(channel_id=300, thread_id=3300, starter_message_id=3400),
        }
    )
    runtime.fedify_gateway.publish_local_community_content.return_value = _publish_results(("a-post", "o-post"))[0]

    result = await runtime.handle_discord_thread_create(
        thread=build_thread(thread_id=2200, channel_id=200, name="Subscriber topic"),
        starter_message=build_starter_message(message_id=2300, content="subscriber body"),
    )

    thread_row = database.local_community_content.get_local_community_thread_by_ap_object_id("o-post")
    surfaces = database.local_community_surfaces.list_local_community_thread_surfaces(thread_row.id)
    assert result.status == "published"
    assert [(s.discord_forum_channel_id, s.role, s.local_subscriber_id) for s in surfaces] == [
        (200, "local_subscriber", source.id),
        (100, "host", None),
        (300, "local_subscriber", sibling.id),
    ]
    assert runtime.fedify_gateway.publish_local_community_content.await_count == 1


@pytest.mark.asyncio
async def test_duplicate_local_subscriber_thread_retries_missing_targets_without_republish(tmp_path: Path) -> None:
    """Duplicate source processing should only create missing target surfaces."""
    database, runtime = _runtime(tmp_path)
    local_community = _local_community(database)
    source, sibling = _add_local_subscribers(database, local_community)
    add_registered_user(database)
    thread_row = database.local_community_content.create_local_community_thread_canonical(
        local_community_id=local_community.id,
        ap_activity_id="a-post",
        ap_object_id="o-post",
        direction="discord_to_ap",
        origin_kind="discord_local_subscriber",
    )
    database.local_community_surfaces.create_local_community_thread_surface(
        local_community_thread_id=thread_row.id,
        discord_forum_channel_id=200,
        discord_thread_id=2200,
        discord_starter_message_id=2300,
        role="local_subscriber",
        local_subscriber_id=source.id,
    )
    database.local_community_surfaces.create_local_community_thread_surface(
        local_community_thread_id=thread_row.id,
        discord_forum_channel_id=300,
        discord_thread_id=3300,
        discord_starter_message_id=3400,
        role="local_subscriber",
        local_subscriber_id=sibling.id,
    )
    runtime.bot = build_bot(forum_channels={100: build_forum_channel_object_result(channel_id=100, thread_id=1100, starter_message_id=1200)})

    await runtime.handle_discord_thread_create(
        thread=build_thread(thread_id=2200, channel_id=200, name="Subscriber topic"),
        starter_message=build_starter_message(message_id=2300),
    )

    with database.session() as session:
        assert session.scalar(select(func.count()).select_from(LocalCommunityThread)) == 1
        assert session.scalar(select(func.count()).select_from(LocalCommunityThreadSurface)) == 3
    assert database.local_community_surfaces.get_local_community_thread_surface(local_community_thread_id=thread_row.id, discord_forum_channel_id=100) is not None
    runtime.fedify_gateway.publish_local_community_content.assert_not_awaited()


@pytest.mark.asyncio
async def test_local_subscriber_root_reply_fans_out_with_target_local_starter_parents(tmp_path: Path) -> None:
    """Root replies from a subscriber should use each target thread's starter id."""
    database, runtime = _runtime(tmp_path)
    local_community = _local_community(database)
    source, sibling = _add_local_subscribers(database, local_community)
    add_registered_user(database)
    thread_row = database.local_community_content.create_local_community_thread_canonical(
        local_community_id=local_community.id,
        ap_activity_id="a-post",
        ap_object_id="o-post",
        direction="discord_to_ap",
        origin_kind="discord_local_subscriber",
    )
    source_surface = _thread_surface(database, thread_row, 200, 2200, 2300, "local_subscriber", source.id)
    _thread_surface(database, thread_row, 100, 1100, 1200, "host", None)
    _thread_surface(database, thread_row, 300, 3300, 3400, "local_subscriber", sibling.id)
    runtime.bot = build_bot(
        threads={1000: build_send_thread(thread_id=1000, sent_message_id=0), 1100: build_send_thread(thread_id=1100, sent_message_id=1300), 3300: build_send_thread(thread_id=3300, sent_message_id=3500)}
    )
    runtime.fedify_gateway.publish_local_community_content.return_value = _publish_results(("a-comment", "o-comment"))[0]

    result = await runtime.handle_discord_message(message=build_thread_message(message_id=2400, thread_id=2200, channel_id=200))

    message_row = database.local_community_content.get_local_community_message_by_ap_object_id("o-comment")
    source_message_surface = database.local_community_surfaces.get_local_community_message_surface(local_community_message_id=message_row.id, local_community_thread_surface_id=source_surface.id)
    host_surface = database.local_community_surfaces.get_local_community_message_surface_by_discord_message_id(1300)
    sibling_surface = database.local_community_surfaces.get_local_community_message_surface_by_discord_message_id(3500)
    assert result.status == "published"
    assert source_message_surface.discord_message_id == 2400
    assert source_message_surface.parent_discord_message_id == 2300
    assert host_surface.parent_discord_message_id == 1200
    assert sibling_surface.parent_discord_message_id == 3400


@pytest.mark.asyncio
async def test_local_subscriber_nested_reply_maps_parent_surface_per_target(tmp_path: Path) -> None:
    """Nested replies must target corresponding parent messages per Discord surface."""
    database, runtime = _runtime(tmp_path)
    local_community = _local_community(database)
    source, sibling = _add_local_subscribers(database, local_community)
    add_registered_user(database)
    thread_row = _canonical_thread(database, local_community)
    source_thread = _thread_surface(database, thread_row, 200, 2200, 2300, "local_subscriber", source.id)
    host_thread = _thread_surface(database, thread_row, 100, 1100, 1200, "host", None)
    sibling_thread = _thread_surface(database, thread_row, 300, 3300, 3400, "local_subscriber", sibling.id)
    parent_row = _canonical_message(database, thread_row, activity="a-parent", obj="o-parent", parent="o-post")
    _message_surface(database, parent_row, source_thread, 200, 2400, 2300, "local_subscriber", source.id)
    _message_surface(database, parent_row, host_thread, 100, 1300, 1200, "host", None)
    _message_surface(database, parent_row, sibling_thread, 300, 3500, 3400, "local_subscriber", sibling.id)
    runtime.bot = build_bot(threads={1100: build_send_thread(thread_id=1100, sent_message_id=1301), 3300: build_send_thread(thread_id=3300, sent_message_id=3501)})
    runtime.fedify_gateway.publish_local_community_content.return_value = _publish_results(("a-child", "o-child"))[0]

    await runtime.handle_discord_message(message=build_thread_message(message_id=2401, thread_id=2200, channel_id=200, reference_message_id=2400))

    assert database.local_community_surfaces.get_local_community_message_surface_by_discord_message_id(1301).parent_discord_message_id == 1300
    assert database.local_community_surfaces.get_local_community_message_surface_by_discord_message_id(3501).parent_discord_message_id == 3500
    assert runtime.fedify_gateway.publish_local_community_content.await_args.args[0].in_reply_to_object_id == "o-parent"


@pytest.mark.asyncio
async def test_duplicate_local_subscriber_reply_retries_missing_target_surface_only(tmp_path: Path) -> None:
    """Duplicate subscriber replies should not republish or duplicate source surfaces."""
    database, runtime = _runtime(tmp_path)
    local_community = _local_community(database)
    source, sibling = _add_local_subscribers(database, local_community)
    thread_row = _canonical_thread(database, local_community)
    source_thread = _thread_surface(database, thread_row, 200, 2200, 2300, "local_subscriber", source.id)
    host_thread = _thread_surface(database, thread_row, 100, 1100, 1200, "host", None)
    _thread_surface(database, thread_row, 300, 3300, 3400, "local_subscriber", sibling.id)
    message_row = _canonical_message(database, thread_row, activity="a-comment", obj="o-comment", parent="o-post")
    _message_surface(database, message_row, source_thread, 200, 2400, 2300, "local_subscriber", source.id)
    _message_surface(database, message_row, host_thread, 100, 1300, 1200, "host", None)
    runtime.bot = build_bot(threads={3300: build_send_thread(thread_id=3300, sent_message_id=3500)})

    await runtime.handle_discord_message(message=build_thread_message(message_id=2400, thread_id=2200, channel_id=200))

    with database.session() as session:
        assert session.scalar(select(func.count()).select_from(LocalCommunityMessage)) == 1
        assert session.scalar(select(func.count()).select_from(LocalCommunityMessageSurface)) == 3
    runtime.fedify_gateway.publish_local_community_content.assert_not_awaited()


@pytest.mark.asyncio
async def test_inactive_local_subscriber_is_not_routed_as_source(tmp_path: Path) -> None:
    """Inactive local subscribers must not become local-community source forums."""
    database, local_runtime = _runtime(tmp_path)
    local_community = _local_community(database)
    database.local_subscribers.create_local_subscriber(local_community_id=local_community.id, discord_guild_id=10, discord_channel_id=200, initiated_by_discord_user_id="999", status="inactive")
    remote_runtime = AsyncMock()
    router = DiscordEventRouter(database=database, community_runtime=remote_runtime, local_community_runtime=local_runtime)
    local_runtime.handle_discord_thread_create = AsyncMock()

    await router.handle_thread_create(thread=build_thread(thread_id=2200, channel_id=200), starter_message=build_starter_message(message_id=2300))

    local_runtime.handle_discord_thread_create.assert_not_awaited()
    remote_runtime.handle_discord_thread_create.assert_awaited_once()


@pytest.mark.asyncio
async def test_remote_subscription_forum_with_bad_local_subscriber_row_stays_remote(tmp_path: Path) -> None:
    """Mixed-state forums should not silently become local-subscriber sources."""
    database, local_runtime = _runtime(tmp_path)
    local_community = _local_community(database)
    database.local_subscribers.create_local_subscriber(
        local_community_id=local_community.id,
        discord_guild_id=10,
        discord_channel_id=200,
        initiated_by_discord_user_id="999",
        status="active",
    )
    add_accepted_subscription(database, channel_id=200)
    remote_runtime = AsyncMock()
    router = DiscordEventRouter(database=database, community_runtime=remote_runtime, local_community_runtime=local_runtime)
    local_runtime.handle_discord_thread_create = AsyncMock()

    await router.handle_thread_create(
        thread=build_thread(thread_id=2200, channel_id=200),
        starter_message=build_starter_message(message_id=2300),
    )

    local_runtime.handle_discord_thread_create.assert_not_awaited()
    remote_runtime.handle_discord_thread_create.assert_awaited_once()


@pytest.mark.asyncio
async def test_local_subscriber_source_edit_delete_is_stage5_authoritative(tmp_path: Path) -> None:
    """Stage 5 supersedes Stage 4 deferral for active subscriber surfaces."""
    database, runtime = _runtime(tmp_path)
    local_community = _local_community(database)
    source, _ = _add_local_subscribers(database, local_community)
    add_registered_user(database)
    thread_row = _canonical_thread(database, local_community)
    _thread_surface(database, thread_row, 200, 2200, 2300, "local_subscriber", source.id)
    database.activitypub_objects.create_published_activity_object(actor_username="alice", actor_url="https://bridge.example/actors/alice", community_actor_url=local_community.actor_url, activity_id="a-post", object_id="o-post", kind="post", title="Title", body_markdown="Body", in_reply_to_object_id=None, discord_channel_id=200, discord_message_id=2300)

    await runtime.handle_discord_message_edit(message_id=2300, new_content="edited", runtime=SimpleNamespace(fedify_gateway=runtime.fedify_gateway))
    await runtime.handle_discord_message_delete(message_id=2300, runtime=SimpleNamespace(fedify_gateway=runtime.fedify_gateway))

    runtime.fedify_gateway.update_content.assert_awaited_once()
    runtime.fedify_gateway.delete_content.assert_awaited_once()
