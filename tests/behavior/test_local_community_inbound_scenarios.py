"""Behavior scenarios for inbound local-community follow and content routing."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from src.activitypub_models import ActivityPubEvent, BridgeGatewayEvent
from src.discord_publish_service import ContentPublishService
from src.local_communities.runtime import LocalCommunityRuntime
from src.local_communities.service import LocalCommunityService
from support.db import build_database
from support.discord import (
    build_bot,
    build_forum_channel_tuple_result,
    build_send_thread,
)


def _runtime(tmp_path: Path) -> tuple[object, LocalCommunityRuntime]:
    """Build a real local-community runtime with a fake gateway boundary."""
    database = build_database(tmp_path, "local-community-inbound.db")
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
    """Seed one local community and return its DB row."""
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


def _post_event(*, actor_id: str = "https://lemmy.example/u/bob") -> ActivityPubEvent:
    """Build one normalized inbound post targeting the local community actor."""
    return ActivityPubEvent.model_validate(
        {
            "event_type": "post.created",
            "delivery_id": "https://lemmy.example/activities/create/post/1",
            "occurred_at": "2026-05-19T10:00:00Z",
            "community_actor_id": "https://bridge.example/communities/hackers",
            "actor_id": actor_id,
            "object": {
                "ap_id": "https://lemmy.example/post/1",
                "kind": "post",
                "lemmy_id": 1,
                "post_ap_id": None,
                "post_lemmy_id": None,
                "parent_ap_id": None,
                "title": "Remote topic",
                "body_markdown": "hello from lemmy",
                "url": "https://lemmy.example/post/1",
                "published_at": "2026-05-19T10:00:00Z",
                "author_name": "bob",
            },
        }
    )


def _comment_event(
    *,
    actor_id: str = "https://lemmy.example/u/bob",
    parent_ap_id: str = "https://lemmy.example/post/1",
) -> ActivityPubEvent:
    """Build one normalized inbound comment targeting the local community actor."""
    return ActivityPubEvent.model_validate(
        {
            "event_type": "comment.created",
            "delivery_id": "https://lemmy.example/activities/create/comment/1",
            "occurred_at": "2026-05-19T10:05:00Z",
            "community_actor_id": "https://bridge.example/communities/hackers",
            "actor_id": actor_id,
            "object": {
                "ap_id": "https://lemmy.example/comment/1",
                "kind": "comment",
                "lemmy_id": 2,
                "post_ap_id": "https://lemmy.example/post/1",
                "post_lemmy_id": 1,
                "parent_ap_id": parent_ap_id,
                "title": None,
                "body_markdown": "hello comment",
                "url": "https://lemmy.example/comment/1",
                "published_at": "2026-05-19T10:05:00Z",
                "author_name": "bob",
            },
        }
    )


@pytest.mark.asyncio
async def test_remote_follow_to_local_community_persists_follower_and_sends_accept(
    tmp_path: Path,
) -> None:
    """A local-community follow request should persist the follower and accept it."""
    database, runtime = _runtime(tmp_path)
    local_community = _local_community(database)

    result = await runtime.handle_follow_request(
        local_community_actor_id=local_community.actor_url,
        remote_actor_id="https://lemmy.example/u/bob",
        remote_inbox_url="https://lemmy.example/u/bob/inbox",
        follow_activity_id="https://lemmy.example/activities/follow/1",
    )
    follower = database.get_local_community_follower(
        local_community_id=local_community.id,
        remote_actor_id="https://lemmy.example/u/bob",
    )

    assert result.status == "processed"
    assert follower is not None
    runtime.fedify_gateway.accept_local_community_follow.assert_awaited_once()


@pytest.mark.asyncio
async def test_repeated_remote_follow_resends_accept_and_refreshes_request_details(
    tmp_path: Path,
) -> None:
    """A repeated Follow should recover a lost Accept without duplicating rows."""
    database, runtime = _runtime(tmp_path)
    local_community = _local_community(database)
    database.create_local_community_follower(
        local_community_id=local_community.id,
        remote_actor_id="https://mastodon.social/ap/users/116015738644832902",
        remote_inbox_url="https://mastodon.social/ap/users/116015738644832902/old-inbox",
        follow_activity_id="https://mastodon.social/old-follow",
        status="accepted",
    )

    result = await runtime.handle_follow_request(
        local_community_actor_id=local_community.actor_url,
        remote_actor_id="https://mastodon.social/ap/users/116015738644832902",
        remote_inbox_url="https://mastodon.social/ap/users/116015738644832902/inbox",
        follow_activity_id="https://mastodon.social/new-follow",
    )
    follower = database.get_local_community_follower(
        local_community_id=local_community.id,
        remote_actor_id="https://mastodon.social/ap/users/116015738644832902",
    )
    followers = database.list_local_community_followers(local_community.id, status=None)

    assert result.status == "processed"
    assert follower is not None
    assert follower.remote_inbox_url == "https://mastodon.social/ap/users/116015738644832902/inbox"
    assert follower.follow_activity_id == "https://mastodon.social/new-follow"
    assert len(followers) == 1
    runtime.fedify_gateway.accept_local_community_follow.assert_awaited_once_with(
        community_slug="hackers",
        community_actor_url=local_community.actor_url,
        remote_actor_id="https://mastodon.social/ap/users/116015738644832902",
        remote_inbox_url="https://mastodon.social/ap/users/116015738644832902/inbox",
        follow_activity_id="https://mastodon.social/new-follow",
    )


@pytest.mark.asyncio
async def test_remote_follower_top_level_post_creates_new_discord_thread(
    tmp_path: Path,
) -> None:
    """An accepted remote follower should create a Discord thread for a new post."""
    database, runtime = _runtime(tmp_path)
    local_community = _local_community(database)
    database.create_local_community_follower(
        local_community_id=local_community.id,
        remote_actor_id="https://lemmy.example/u/bob",
        remote_inbox_url="https://lemmy.example/u/bob/inbox",
        follow_activity_id="https://lemmy.example/activities/follow/1",
    )
    forum_channel = build_forum_channel_tuple_result(
        channel_id=100,
        thread_id=200,
        starter_message_id=300,
    )
    runtime.bot = build_bot(forum_channels={100: forum_channel})

    result = await runtime.handle_inbound_post(_post_event(), SimpleNamespace())
    created = database.get_local_community_thread_by_ap_object_id("https://lemmy.example/post/1")

    assert result.status == "processed"
    assert created is not None


@pytest.mark.asyncio
async def test_remote_follower_nested_reply_uses_real_discord_message_reference(
    tmp_path: Path,
) -> None:
    """A nested remote reply must pass a real discord.py MessageReference."""
    database, runtime = _runtime(tmp_path)
    local_community = _local_community(database)
    database.create_local_community_follower(
        local_community_id=local_community.id,
        remote_actor_id="https://lemmy.example/u/bob",
        remote_inbox_url="https://lemmy.example/u/bob/inbox",
        follow_activity_id="https://lemmy.example/activities/follow/1",
    )
    thread_row = database.create_local_community_thread(
        local_community_id=local_community.id,
        discord_thread_id=200,
        discord_starter_message_id=300,
        ap_activity_id="https://bridge.example/users/alice/activities/create/post/1",
        ap_object_id="https://lemmy.example/post/1",
        direction="ap_to_discord",
        origin_kind="remote_follower",
    )
    database.create_local_community_message(
        local_community_thread_id=thread_row.id,
        discord_message_id=401,
        ap_activity_id="https://lemmy.example/activities/create/comment/0",
        ap_object_id="https://lemmy.example/comment/0",
        parent_ap_object_id="https://lemmy.example/post/1",
        parent_discord_message_id=300,
        direction="ap_to_discord",
    )

    async def send_with_type_check(content: str, **kwargs: object) -> object:
        """Assert the runtime passes one real discord.py MessageReference."""
        reference = kwargs.get("reference")
        assert isinstance(reference, discord.MessageReference)
        assert reference.message_id == 401
        assert reference.channel_id == 200
        assert reference.fail_if_not_exists is False
        return SimpleNamespace(id=402)

    runtime.bot = build_bot(
        threads={
            200: SimpleNamespace(
                id=200,
                send=AsyncMock(side_effect=send_with_type_check),
            )
        }
    )

    result = await runtime.handle_inbound_comment(
        _comment_event(parent_ap_id="https://lemmy.example/comment/0"),
        SimpleNamespace(),
    )
    created = database.get_local_community_message_by_ap_object_id(
        "https://lemmy.example/comment/1"
    )

    assert result.status == "processed"
    assert created is not None
    assert created.parent_discord_message_id == 401


@pytest.mark.asyncio
async def test_remote_non_follower_top_level_post_is_skipped(
    tmp_path: Path,
) -> None:
    """A remote actor without an accepted follower row should not create a thread."""
    database, runtime = _runtime(tmp_path)
    _local_community(database)
    forum_channel = build_forum_channel_tuple_result(
        channel_id=100,
        thread_id=200,
        starter_message_id=300,
    )
    runtime.bot = build_bot(forum_channels={100: forum_channel})

    result = await runtime.handle_inbound_post(_post_event(), SimpleNamespace())

    assert result.status == "skipped"
    assert database.get_local_community_thread_by_ap_object_id("https://lemmy.example/post/1") is None


@pytest.mark.asyncio
async def test_remote_follower_reply_creates_message_in_mapped_thread(
    tmp_path: Path,
) -> None:
    """An accepted remote follower reply should create a Discord message in the mapped thread."""
    database, runtime = _runtime(tmp_path)
    local_community = _local_community(database)
    database.create_local_community_follower(
        local_community_id=local_community.id,
        remote_actor_id="https://lemmy.example/u/bob",
        remote_inbox_url="https://lemmy.example/u/bob/inbox",
        follow_activity_id="https://lemmy.example/activities/follow/1",
    )
    database.create_local_community_thread(
        local_community_id=local_community.id,
        discord_thread_id=200,
        discord_starter_message_id=300,
        ap_activity_id="https://bridge.example/users/alice/activities/create/post/1",
        ap_object_id="https://lemmy.example/post/1",
        direction="ap_to_discord",
        origin_kind="remote_follower",
    )
    runtime.bot = build_bot(threads={200: build_send_thread(thread_id=200, sent_message_id=400)})

    result = await runtime.handle_inbound_comment(_comment_event(), SimpleNamespace())
    created = database.get_local_community_message_by_ap_object_id("https://lemmy.example/comment/1")

    generic_mapping = database.get_message_mapping_by_object_id("https://lemmy.example/comment/1")

    assert result.status == "processed"
    assert created is not None
    assert generic_mapping is not None
    assert generic_mapping.source_platform == "activitypub"
    assert generic_mapping.source_id == "https://lemmy.example/comment/1"
    assert generic_mapping.activity_id == "https://lemmy.example/activities/create/comment/1"
    assert generic_mapping.actor_url == "https://lemmy.example/u/bob"
    assert generic_mapping.community_actor_url == "https://bridge.example/communities/hackers"
    assert generic_mapping.discord_channel_id == 200
    assert generic_mapping.discord_message_id == 400

@pytest.mark.asyncio
async def test_remote_follower_reply_keeps_existing_generic_mapping(
    tmp_path: Path,
) -> None:
    """A pre-existing generic AP mapping must not break comment mirroring.

    System state: the remote comment has no local-community message yet, but a
    generic mapping already exists for the same AP object.  Action: mirror the
    inbound comment to Discord.  Assert: Discord/local-community persistence
    succeeds and the existing generic mapping remains the one used for later
    gateway parent lookups.
    """
    database, runtime = _runtime(tmp_path)
    local_community = _local_community(database)
    database.create_local_community_follower(
        local_community_id=local_community.id,
        remote_actor_id="https://lemmy.example/u/bob",
        remote_inbox_url="https://lemmy.example/u/bob/inbox",
        follow_activity_id="https://lemmy.example/activities/follow/1",
    )
    database.create_local_community_thread(
        local_community_id=local_community.id,
        discord_thread_id=200,
        discord_starter_message_id=300,
        ap_activity_id="https://bridge.example/users/alice/activities/create/post/1",
        ap_object_id="https://lemmy.example/post/1",
        direction="ap_to_discord",
        origin_kind="remote_follower",
    )
    database.create_message_mapping(
        source_platform="activitypub",
        source_id="https://lemmy.example/comment/1",
        activity_id="https://lemmy.example/activities/create/comment/1",
        object_id="https://lemmy.example/comment/1",
        actor_url="https://lemmy.example/u/bob",
        community_actor_url="https://bridge.example/communities/hackers",
        discord_channel_id=200,
        discord_message_id=399,
    )
    runtime.bot = build_bot(threads={200: build_send_thread(thread_id=200, sent_message_id=400)})

    result = await runtime.handle_inbound_comment(_comment_event(), SimpleNamespace())
    created = database.get_local_community_message_by_ap_object_id("https://lemmy.example/comment/1")
    generic_mapping = database.get_message_mapping_by_object_id("https://lemmy.example/comment/1")

    assert result.status == "processed"
    assert created is not None
    assert generic_mapping is not None
    assert generic_mapping.discord_message_id == 399
