"""Behavior scenarios for the Stage 2 local-community surface refactor.

These scenarios pin down the Stage 2 boundary from the implementation plan:
host-forum behavior must keep working, old databases must backfill exactly one
host surface per canonical row, and local-subscriber forums must still stay out
of runtime routing until later stages.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text

from src.discord_event_router import DiscordEventRouter
from src.content_publish_service import ContentPublishService
from src.local_communities.runtime import LocalCommunityRuntime
from src.local_communities.service import LocalCommunityService
from support.db import add_registered_user, build_database
from support.discord import build_starter_message, build_thread, build_thread_message


def _runtime(tmp_path: Path) -> tuple[object, LocalCommunityRuntime]:
    """Build one real local-community runtime with only gateway IO mocked."""
    database = build_database(tmp_path, "local-community-stage2.db")
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


def _create_local_community(database: object, *, forum_channel_id: int = 100) -> object:
    """Create and return one local community bound to the requested forum."""
    LocalCommunityService(
        database=database,
        base_url="https://bridge.example",
        keypair_generator=lambda: ("public-key", "private-key"),
    ).create_local_community(
        discord_guild_id=10,
        discord_forum_channel_id=forum_channel_id,
        slug="hackers",
        name="Hackers",
        description="A local hackerspace forum.",
    )
    return database.get_local_community_by_slug("hackers")


@pytest.mark.asyncio
async def test_host_forum_create_paths_write_exactly_one_host_surface_per_canonical_row(
    tmp_path: Path,
) -> None:
    """Host-forum publish paths should create one canonical row and one host surface.

    System state: one local community exists, one local subscriber row exists,
    and the runtime is still operating in the host-forum-only Stage 2 mode.
    Action: publish one thread starter and one reply through the real
    LocalCommunityRuntime entry points. Assert: canonical rows exist, exactly
    one host surface row exists per canonical row, and no local-subscriber
    surfaces are created yet.
    """
    database, runtime = _runtime(tmp_path)
    community = _create_local_community(database, forum_channel_id=100)
    database.create_local_subscriber(
        local_community_id=community.id,
        discord_guild_id=10,
        discord_channel_id=101,
        initiated_by_discord_user_id="456",
    )
    add_registered_user(database)
    runtime.fedify_gateway.publish_local_community_content.side_effect = [
        SimpleNamespace(
            activity_id="https://bridge.example/users/alice/activities/create/post/1",
            object_id="https://bridge.example/users/alice/post/1",
            community_actor_url="https://bridge.example/communities/hackers",
            delivered_follower_count=0,
            failed_follower_count=0,
        ),
        SimpleNamespace(
            activity_id="https://bridge.example/users/alice/activities/create/comment/1",
            object_id="https://bridge.example/users/alice/comment/1",
            community_actor_url="https://bridge.example/communities/hackers",
            delivered_follower_count=0,
            failed_follower_count=0,
        ),
    ]

    await runtime.handle_discord_thread_create(
        thread=build_thread(),
        starter_message=build_starter_message(),
    )
    await runtime.handle_discord_message(message=build_thread_message())

    canonical_thread = database.get_local_community_thread_by_ap_object_id(
        "https://bridge.example/users/alice/post/1"
    )
    assert canonical_thread is not None
    assert not hasattr(canonical_thread, "discord_thread_id")
    assert not hasattr(canonical_thread, "discord_starter_message_id")

    host_thread_surface = database.get_local_community_thread_surface_by_discord_thread_id(200)
    assert host_thread_surface is not None
    assert host_thread_surface.role == "host"
    assert host_thread_surface.discord_forum_channel_id == 100
    thread_surfaces = database.list_local_community_thread_surfaces(canonical_thread.id)
    assert len(thread_surfaces) == 1

    canonical_message = database.get_local_community_message_by_ap_object_id(
        "https://bridge.example/users/alice/comment/1"
    )
    assert canonical_message is not None
    assert not hasattr(canonical_message, "discord_message_id")
    assert not hasattr(canonical_message, "parent_discord_message_id")

    host_message_surface = database.get_local_community_message_surface_by_discord_message_id(301)
    assert host_message_surface is not None
    assert host_message_surface.role == "host"
    assert host_message_surface.parent_discord_message_id == 300
    message_surfaces = database.list_local_community_message_surfaces(canonical_message.id)
    assert len(message_surfaces) == 1
    assert message_surfaces[0].local_community_thread_surface_id == host_thread_surface.id


def test_stage2_current_schema_migrate_keeps_host_surfaces_idempotent(
    tmp_path: Path,
) -> None:
    """Current-schema migrate keeps canonical rows and host surfaces stable.

    Stage 5 removes the old pre-surface database upgrade guarantee. This Stage
    2 regression check now protects the supported baseline instead: canonical
    rows no longer own Discord ids, host surface rows preserve those ids,
    repeated migrate calls do not duplicate surfaces, and migrate alone does
    not create local-subscriber surfaces.
    """
    migrated = build_database(tmp_path, "local-community-stage2-current-migrate.db")
    community = _create_local_community(migrated, forum_channel_id=100)
    migrated.create_local_subscriber(
        local_community_id=community.id,
        discord_guild_id=10,
        discord_channel_id=101,
        initiated_by_discord_user_id="456",
    )
    canonical_thread = migrated.create_local_community_thread(
        local_community_id=community.id,
        discord_thread_id=200,
        discord_starter_message_id=300,
        ap_activity_id="https://bridge.example/users/alice/activities/create/post/1",
        ap_object_id="https://bridge.example/users/alice/post/1",
        direction="discord_to_ap",
        origin_kind="discord_local",
    )
    canonical_message = migrated.create_local_community_message(
        local_community_thread_id=canonical_thread.id,
        discord_message_id=301,
        ap_activity_id="https://bridge.example/users/alice/activities/create/comment/1",
        ap_object_id="https://bridge.example/users/alice/comment/1",
        parent_ap_object_id="https://bridge.example/users/alice/post/1",
        parent_discord_message_id=300,
        direction="discord_to_ap",
    )

    migrated.migrate()
    migrated.migrate()

    thread_surfaces = migrated.list_local_community_thread_surfaces(canonical_thread.id)
    assert len(thread_surfaces) == 1
    assert thread_surfaces[0].role == "host"
    assert thread_surfaces[0].discord_thread_id == 200
    assert thread_surfaces[0].discord_starter_message_id == 300
    assert thread_surfaces[0].local_subscriber_id is None

    message_surfaces = migrated.list_local_community_message_surfaces(canonical_message.id)
    assert len(message_surfaces) == 1
    assert message_surfaces[0].role == "host"
    assert message_surfaces[0].discord_message_id == 301
    assert message_surfaces[0].parent_discord_message_id == 300
    assert message_surfaces[0].local_subscriber_id is None

    with migrated.engine.connect() as connection_sql:
        thread_columns = {
            row[1]
            for row in connection_sql.execute(text("PRAGMA table_info(local_community_threads)")).fetchall()
        }
        message_columns = {
            row[1]
            for row in connection_sql.execute(text("PRAGMA table_info(local_community_messages)")).fetchall()
        }

    assert "discord_thread_id" not in thread_columns
    assert "discord_starter_message_id" not in thread_columns
    assert "discord_message_id" not in message_columns
    assert "parent_discord_message_id" not in message_columns


@pytest.mark.asyncio
async def test_local_subscriber_forum_is_stage4_runtime_source(
    tmp_path: Path,
) -> None:
    """Active local-subscriber forums route to local-community runtime after Stage 4.

    Stage 2 created the surface model while keeping subscribers out of runtime.
    Stage 4 deliberately changes that routing boundary, so this regression test
    now documents the widened behavior without changing Stage 2 storage checks.
    """
    database = build_database(tmp_path, "local-community-stage2-router.db")
    community = _create_local_community(database, forum_channel_id=100)
    database.create_local_subscriber(
        local_community_id=community.id,
        discord_guild_id=10,
        discord_channel_id=101,
        initiated_by_discord_user_id="456",
    )
    community_runtime = SimpleNamespace(
        handle_discord_thread_create=AsyncMock(return_value="community")
    )
    local_runtime = SimpleNamespace(handle_discord_thread_create=AsyncMock(return_value="local"))
    router = DiscordEventRouter(
        database=database,
        community_runtime=community_runtime,
        local_community_runtime=local_runtime,
    )

    result = await router.handle_thread_create(
        thread=build_thread(thread_id=501, channel_id=101),
        starter_message=build_starter_message(message_id=601),
    )

    assert router.is_local_community_forum(101) is True
    assert result == "local"
    local_runtime.handle_discord_thread_create.assert_awaited_once()
    community_runtime.handle_discord_thread_create.assert_not_awaited()
