"""Behavior scenarios for local-community edit and delete propagation.

These scenarios exercise the local-community mode through its runtime and
handler entry points. They pin down the parity work required to bring
`src/local_communities` closer to the established `src/community_sync`
contracts for outbound Discord edits/deletes and inbound AP updates/deletes.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.activitypub_handlers import dispatch_activitypub_event
from src.activitypub_models import ActivityPubEvent
from src.content_publish_service import ContentPublishService
from src.local_communities.runtime import LocalCommunityRuntime
from src.local_communities.service import LocalCommunityService
from support.db import add_registered_user, build_database
from support.discord import build_bot
from support.runtime import build_runtime_namespace


def _runtime(tmp_path: Path) -> tuple[object, LocalCommunityRuntime]:
    """Build one local-community runtime with a fake gateway boundary."""
    database = build_database(tmp_path, "local-community-edit-delete.db")
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
    """Seed one local community and return its persisted row."""
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
        created_by_discord_user_id="123",
    )
    return database.local_communities.get_local_community_by_slug("hackers")


def _local_post_event(*, event_type: str) -> ActivityPubEvent:
    """Build one normalized local-community post update/delete event."""
    return ActivityPubEvent.model_validate(
        {
            "event_type": event_type,
            "delivery_id": f"https://lemmy.example/activities/{event_type}/post/1",
            "occurred_at": "2026-05-19T10:00:00Z",
            "community_actor_id": "https://bridge.example/communities/hackers",
            "actor_id": "https://lemmy.example/u/bob",
            "object": {
                "ap_id": "https://lemmy.example/post/1",
                "kind": "post",
                "lemmy_id": 1,
                "post_ap_id": None,
                "post_lemmy_id": None,
                "parent_ap_id": None,
                "title": "Remote topic",
                "body_markdown": "updated remote post body",
                "url": "https://lemmy.example/post/1",
                "published_at": "2026-05-19T10:00:00Z",
                "author_name": "bob",
            },
        }
    )


def _local_comment_event(*, event_type: str) -> ActivityPubEvent:
    """Build one normalized local-community comment update/delete event."""
    return ActivityPubEvent.model_validate(
        {
            "event_type": event_type,
            "delivery_id": f"https://lemmy.example/activities/{event_type}/comment/1",
            "occurred_at": "2026-05-19T10:05:00Z",
            "community_actor_id": "https://bridge.example/communities/hackers",
            "actor_id": "https://lemmy.example/u/bob",
            "object": {
                "ap_id": "https://lemmy.example/comment/1",
                "kind": "comment",
                "lemmy_id": 2,
                "post_ap_id": "https://lemmy.example/post/1",
                "post_lemmy_id": 1,
                "parent_ap_id": "https://lemmy.example/post/1",
                "title": None,
                "body_markdown": "updated remote comment body",
                "url": "https://lemmy.example/comment/1",
                "published_at": "2026-05-19T10:05:00Z",
                "author_name": "bob",
            },
        }
    )


@pytest.mark.asyncio
async def test_local_community_thread_starter_edit_updates_ap_post(tmp_path: Path) -> None:
    """Editing a Discord-backed local-community post should send one AP Update."""
    database, local_runtime = _runtime(tmp_path)
    local_community = _local_community(database)
    add_registered_user(database)
    database.message_mappings.create_message_mapping(
        source_platform="discord",
        source_id="300",
        activity_id="https://bridge.example/users/alice/activities/create/post/1",
        object_id="https://bridge.example/users/alice/post/1",
        actor_url="https://bridge.example/users/alice",
        community_actor_url=local_community.actor_url,
        discord_channel_id=100,
        discord_message_id=300,
    )
    database.activitypub_objects.create_published_activity_object(
        actor_username="alice",
        actor_url="https://bridge.example/users/alice",
        community_actor_url=local_community.actor_url,
        activity_id="https://bridge.example/users/alice/activities/create/post/1",
        object_id="https://bridge.example/users/alice/post/1",
        kind="post",
        title="Thread title",
        body_markdown="old body",
        in_reply_to_object_id=None,
        discord_channel_id=100,
        discord_message_id=300,
    )
    database.local_community_content.create_local_community_thread(
        local_community_id=local_community.id,
        discord_thread_id=200,
        discord_starter_message_id=300,
        ap_activity_id="https://bridge.example/users/alice/activities/create/post/1",
        ap_object_id="https://bridge.example/users/alice/post/1",
        direction="discord_to_ap",
        origin_kind="discord_local",
    )

    await local_runtime.handle_discord_message_edit(
        message_id=300,
        new_content="edited starter body",
        author_display_name="Alice",
        runtime=build_runtime_namespace(fedify_gateway=local_runtime.fedify_gateway),
    )

    local_runtime.fedify_gateway.update_content.assert_awaited_once()
    request = local_runtime.fedify_gateway.update_content.await_args.args[0]
    assert request.ap_object_id == "https://bridge.example/users/alice/post/1"
    assert request.kind == "post"
    assert request.community_actor_url == local_community.actor_url


@pytest.mark.asyncio
async def test_local_community_comment_delete_sends_ap_delete(tmp_path: Path) -> None:
    """Deleting a Discord-backed local-community comment should send one AP Delete."""
    database, local_runtime = _runtime(tmp_path)
    local_community = _local_community(database)
    add_registered_user(database)
    thread_row = database.local_community_content.create_local_community_thread(
        local_community_id=local_community.id,
        discord_thread_id=200,
        discord_starter_message_id=300,
        ap_activity_id="https://bridge.example/users/alice/activities/create/post/1",
        ap_object_id="https://bridge.example/users/alice/post/1",
        direction="discord_to_ap",
        origin_kind="discord_local",
    )
    database.message_mappings.create_message_mapping(
        source_platform="discord",
        source_id="301",
        activity_id="https://bridge.example/users/alice/activities/create/comment/1",
        object_id="https://bridge.example/users/alice/comment/1",
        actor_url="https://bridge.example/users/alice",
        community_actor_url=local_community.actor_url,
        discord_channel_id=100,
        discord_message_id=301,
    )
    database.activitypub_objects.create_published_activity_object(
        actor_username="alice",
        actor_url="https://bridge.example/users/alice",
        community_actor_url=local_community.actor_url,
        activity_id="https://bridge.example/users/alice/activities/create/comment/1",
        object_id="https://bridge.example/users/alice/comment/1",
        kind="comment",
        title=None,
        body_markdown="old comment",
        in_reply_to_object_id="https://bridge.example/users/alice/post/1",
        discord_channel_id=100,
        discord_message_id=301,
    )
    database.local_community_content.create_local_community_message(
        local_community_thread_id=thread_row.id,
        discord_message_id=301,
        ap_activity_id="https://bridge.example/users/alice/activities/create/comment/1",
        ap_object_id="https://bridge.example/users/alice/comment/1",
        parent_ap_object_id="https://bridge.example/users/alice/post/1",
        parent_discord_message_id=300,
        direction="discord_to_ap",
    )

    await local_runtime.handle_discord_message_delete(
        message_id=301,
        runtime=build_runtime_namespace(fedify_gateway=local_runtime.fedify_gateway),
    )

    local_runtime.fedify_gateway.delete_content.assert_awaited_once()
    request = local_runtime.fedify_gateway.delete_content.await_args.args[0]
    assert request.ap_object_id == "https://bridge.example/users/alice/comment/1"
    assert request.community_actor_url == local_community.actor_url


@pytest.mark.asyncio
async def test_inbound_local_community_post_update_edits_discord_starter(tmp_path: Path) -> None:
    """A remote post update for a local community should edit the starter message."""
    database, local_runtime = _runtime(tmp_path)
    local_community = _local_community(database)
    database.local_community_content.create_local_community_thread(
        local_community_id=local_community.id,
        discord_thread_id=200,
        discord_starter_message_id=300,
        ap_activity_id="https://lemmy.example/activities/create/post/1",
        ap_object_id="https://lemmy.example/post/1",
        direction="ap_to_discord",
        origin_kind="remote_follower",
    )
    starter_message = SimpleNamespace(edit=AsyncMock())
    thread = SimpleNamespace(fetch_message=AsyncMock(return_value=starter_message))
    bot = build_bot(threads={200: thread})
    local_runtime.bot = bot
    runtime = build_runtime_namespace(
        settings=SimpleNamespace(federation_allowlist=[]),
        database=database,
        local_community_runtime=local_runtime,
        community_runtime=SimpleNamespace(),
        bot=bot,
    )

    result = await dispatch_activitypub_event(_local_post_event(event_type="post.updated"), runtime)

    assert result.status == "processed"
    starter_message.edit.assert_awaited_once_with(
        content="**Remote topic**\n\nAuthor: `bob@lemmy.example`\n\nupdated remote post body\n\nhttps://lemmy.example/post/1"
    )


@pytest.mark.asyncio
async def test_inbound_local_community_post_delete_marks_discord_starter_deleted(
    tmp_path: Path,
) -> None:
    """A remote post delete for a local community should mark the starter deleted."""
    database, local_runtime = _runtime(tmp_path)
    local_community = _local_community(database)
    database.local_community_content.create_local_community_thread(
        local_community_id=local_community.id,
        discord_thread_id=200,
        discord_starter_message_id=300,
        ap_activity_id="https://lemmy.example/activities/create/post/1",
        ap_object_id="https://lemmy.example/post/1",
        direction="ap_to_discord",
        origin_kind="remote_follower",
    )
    starter_message = SimpleNamespace(edit=AsyncMock())
    thread = SimpleNamespace(fetch_message=AsyncMock(return_value=starter_message))
    bot = build_bot(threads={200: thread})
    local_runtime.bot = bot
    runtime = build_runtime_namespace(
        settings=SimpleNamespace(federation_allowlist=[]),
        database=database,
        local_community_runtime=local_runtime,
        community_runtime=SimpleNamespace(),
        bot=bot,
    )

    result = await dispatch_activitypub_event(_local_post_event(event_type="post.deleted"), runtime)

    assert result.status == "processed"
    starter_message.edit.assert_awaited_once_with(content="*deleted by creator*")


@pytest.mark.asyncio
async def test_inbound_local_community_comment_update_edits_discord_message(
    tmp_path: Path,
) -> None:
    """A remote comment update for a local community should edit the Discord copy."""
    database, local_runtime = _runtime(tmp_path)
    local_community = _local_community(database)
    thread_row = database.local_community_content.create_local_community_thread(
        local_community_id=local_community.id,
        discord_thread_id=200,
        discord_starter_message_id=300,
        ap_activity_id="https://lemmy.example/activities/create/post/1",
        ap_object_id="https://lemmy.example/post/1",
        direction="ap_to_discord",
        origin_kind="remote_follower",
    )
    database.local_community_content.create_local_community_message(
        local_community_thread_id=thread_row.id,
        discord_message_id=301,
        ap_activity_id="https://lemmy.example/activities/create/comment/1",
        ap_object_id="https://lemmy.example/comment/1",
        parent_ap_object_id="https://lemmy.example/post/1",
        parent_discord_message_id=300,
        direction="ap_to_discord",
    )
    mirrored_message = SimpleNamespace(edit=AsyncMock())
    thread = SimpleNamespace(fetch_message=AsyncMock(return_value=mirrored_message))
    bot = build_bot(threads={200: thread})
    local_runtime.bot = bot
    runtime = build_runtime_namespace(
        settings=SimpleNamespace(federation_allowlist=[]),
        database=database,
        local_community_runtime=local_runtime,
        community_runtime=SimpleNamespace(),
        bot=bot,
    )

    result = await dispatch_activitypub_event(
        _local_comment_event(event_type="comment.updated"),
        runtime,
    )

    assert result.status == "processed"
    mirrored_message.edit.assert_awaited_once_with(
        content="`bob@lemmy.example`\n\nupdated remote comment body"
    )


@pytest.mark.asyncio
async def test_inbound_local_community_comment_delete_marks_discord_message_deleted(
    tmp_path: Path,
) -> None:
    """A remote comment delete for a local community should mark the Discord copy deleted."""
    database, local_runtime = _runtime(tmp_path)
    local_community = _local_community(database)
    thread_row = database.local_community_content.create_local_community_thread(
        local_community_id=local_community.id,
        discord_thread_id=200,
        discord_starter_message_id=300,
        ap_activity_id="https://lemmy.example/activities/create/post/1",
        ap_object_id="https://lemmy.example/post/1",
        direction="ap_to_discord",
        origin_kind="remote_follower",
    )
    database.local_community_content.create_local_community_message(
        local_community_thread_id=thread_row.id,
        discord_message_id=301,
        ap_activity_id="https://lemmy.example/activities/create/comment/1",
        ap_object_id="https://lemmy.example/comment/1",
        parent_ap_object_id="https://lemmy.example/post/1",
        parent_discord_message_id=300,
        direction="ap_to_discord",
    )
    mirrored_message = SimpleNamespace(edit=AsyncMock())
    thread = SimpleNamespace(fetch_message=AsyncMock(return_value=mirrored_message))
    bot = build_bot(threads={200: thread})
    local_runtime.bot = bot
    runtime = build_runtime_namespace(
        settings=SimpleNamespace(federation_allowlist=[]),
        database=database,
        local_community_runtime=local_runtime,
        community_runtime=SimpleNamespace(),
        bot=bot,
    )

    result = await dispatch_activitypub_event(
        _local_comment_event(event_type="comment.deleted"),
        runtime,
    )

    assert result.status == "processed"
    mirrored_message.edit.assert_awaited_once_with(content="*deleted by creator*")
