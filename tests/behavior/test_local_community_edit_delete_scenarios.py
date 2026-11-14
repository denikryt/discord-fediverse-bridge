"""Behavior scenarios for local-community edit and delete propagation.

These scenarios exercise the local-community mode through its runtime and
handler entry points. They pin down the parity work required to bring
`src/local_communities` closer to the established `src/community_sync`
contracts for outbound Discord edits/deletes and inbound AP updates/deletes.
"""

from __future__ import annotations
from support.runtime import build_test_policy_service

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
        bridge_policy_service=build_test_policy_service(database),
    )
    runtime = LocalCommunityRuntime(
        database=database,
        fedify_gateway=gateway,
        content_publish_service=publish_service,
        bridge_prefix="[bridge]",
        bridge_policy_service=build_test_policy_service(database),
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
            "community_actor_id": "https://bridge.example/actors/communities/hackers",
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
            "community_actor_id": "https://bridge.example/actors/communities/hackers",
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
async def test_local_community_thread_starter_edit_updates_ap_post(
    tmp_path: Path,
) -> None:
    """Editing a Discord-backed local-community post should send one AP Update."""
    database, local_runtime = _runtime(tmp_path)
    local_community = _local_community(database)
    add_registered_user(database)
    database.message_mappings.create_message_mapping(
        source_platform="discord",
        source_id="300",
        activity_id="https://bridge.example/actors/users/alice/activities/create/posts/1",
        object_id="https://bridge.example/actors/users/alice/objects/posts/1",
        actor_url="https://bridge.example/actors/users/alice",
        community_actor_url=local_community.actor_url,
        discord_channel_id=100,
        discord_message_id=300,
    )
    database.activitypub_objects.create_published_activity_object(
        actor_username="alice",
        actor_url="https://bridge.example/actors/users/alice",
        community_actor_url=local_community.actor_url,
        activity_id="https://bridge.example/actors/users/alice/activities/create/posts/1",
        object_id="https://bridge.example/actors/users/alice/objects/posts/1",
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
        ap_activity_id="https://bridge.example/actors/users/alice/activities/create/posts/1",
        ap_object_id="https://bridge.example/actors/users/alice/objects/posts/1",
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
    assert request.ap_object_id == "https://bridge.example/actors/users/alice/objects/posts/1"
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
        ap_activity_id="https://bridge.example/actors/users/alice/activities/create/posts/1",
        ap_object_id="https://bridge.example/actors/users/alice/objects/posts/1",
        direction="discord_to_ap",
        origin_kind="discord_local",
    )
    database.message_mappings.create_message_mapping(
        source_platform="discord",
        source_id="301",
        activity_id="https://bridge.example/actors/users/alice/activities/create/comments/1",
        object_id="https://bridge.example/actors/users/alice/objects/comments/1",
        actor_url="https://bridge.example/actors/users/alice",
        community_actor_url=local_community.actor_url,
        discord_channel_id=100,
        discord_message_id=301,
    )
    database.activitypub_objects.create_published_activity_object(
        actor_username="alice",
        actor_url="https://bridge.example/actors/users/alice",
        community_actor_url=local_community.actor_url,
        activity_id="https://bridge.example/actors/users/alice/activities/create/comments/1",
        object_id="https://bridge.example/actors/users/alice/objects/comments/1",
        kind="comment",
        title=None,
        body_markdown="old comment",
        in_reply_to_object_id="https://bridge.example/actors/users/alice/objects/posts/1",
        discord_channel_id=100,
        discord_message_id=301,
    )
    database.local_community_content.create_local_community_message(
        local_community_thread_id=thread_row.id,
        discord_message_id=301,
        ap_activity_id="https://bridge.example/actors/users/alice/activities/create/comments/1",
        ap_object_id="https://bridge.example/actors/users/alice/objects/comments/1",
        parent_ap_object_id="https://bridge.example/actors/users/alice/objects/posts/1",
        parent_discord_message_id=300,
        direction="discord_to_ap",
    )

    await local_runtime.handle_discord_message_delete(
        message_id=301,
        runtime=build_runtime_namespace(fedify_gateway=local_runtime.fedify_gateway),
    )

    local_runtime.fedify_gateway.delete_content.assert_awaited_once()
    request = local_runtime.fedify_gateway.delete_content.await_args.args[0]
    assert request.ap_object_id == "https://bridge.example/actors/users/alice/objects/comments/1"
    assert request.community_actor_url == local_community.actor_url


@pytest.mark.asyncio
async def test_legacy_post_update_without_owner_is_denied(
    tmp_path: Path,
) -> None:
    """Unknown legacy owner cannot acquire post edit permission from a new event."""
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
        settings=SimpleNamespace(discord_guild_allowlist=[], federation_allowlist=[]),
        database=database,
        local_community_runtime=local_runtime,
        community_runtime=SimpleNamespace(),
        bot=bot,
        bridge_policy_service=build_test_policy_service(
            database,
            SimpleNamespace(discord_guild_allowlist=[], federation_allowlist=[]),
        ),
    )

    result = await dispatch_activitypub_event(
        _local_post_event(event_type="post.updated"), runtime
    )

    # These old fixtures intentionally have no immutable owner or verified
    # signed sender. CP22 must deny them rather than infer the owner now.
    assert result.status == "skipped"
    starter_message.edit.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_post_delete_without_owner_is_denied(
    tmp_path: Path,
) -> None:
    """Unknown legacy owner cannot authorize remote post deletion."""
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
        settings=SimpleNamespace(discord_guild_allowlist=[], federation_allowlist=[]),
        database=database,
        local_community_runtime=local_runtime,
        community_runtime=SimpleNamespace(),
        bot=bot,
        bridge_policy_service=build_test_policy_service(
            database,
            SimpleNamespace(discord_guild_allowlist=[], federation_allowlist=[]),
        ),
    )

    result = await dispatch_activitypub_event(
        _local_post_event(event_type="post.deleted"), runtime
    )

    assert result.status == "skipped"
    starter_message.edit.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_comment_update_without_owner_is_denied(
    tmp_path: Path,
) -> None:
    """Unknown legacy reply owner cannot authorize a later edit."""
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
        settings=SimpleNamespace(discord_guild_allowlist=[], federation_allowlist=[]),
        database=database,
        local_community_runtime=local_runtime,
        community_runtime=SimpleNamespace(),
        bot=bot,
        bridge_policy_service=build_test_policy_service(
            database,
            SimpleNamespace(discord_guild_allowlist=[], federation_allowlist=[]),
        ),
    )

    result = await dispatch_activitypub_event(
        _local_comment_event(event_type="comment.updated"),
        runtime,
    )

    assert result.status == "skipped"
    mirrored_message.edit.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_comment_delete_without_owner_is_denied(
    tmp_path: Path,
) -> None:
    """Unknown legacy reply owner cannot authorize deletion."""
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
        settings=SimpleNamespace(discord_guild_allowlist=[], federation_allowlist=[]),
        database=database,
        local_community_runtime=local_runtime,
        community_runtime=SimpleNamespace(),
        bot=bot,
        bridge_policy_service=build_test_policy_service(
            database,
            SimpleNamespace(discord_guild_allowlist=[], federation_allowlist=[]),
        ),
    )

    result = await dispatch_activitypub_event(
        _local_comment_event(event_type="comment.deleted"),
        runtime,
    )

    assert result.status == "skipped"
    mirrored_message.edit.assert_not_awaited()
