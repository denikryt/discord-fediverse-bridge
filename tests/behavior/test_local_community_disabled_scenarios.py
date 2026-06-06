"""Observable behavior for disabled local-community lifecycle gates."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from discordops import run_operation_definition
from src.activitypub_handlers import dispatch_activitypub_event
from src.activitypub_models import ActivityPubEvent, LocalCommunityFollowRequestEvent
from src.content_publish_service import ContentPublishService
from src.local_communities.runtime import LocalCommunityRuntime
from src.local_communities.service import LocalCommunityService
from src.models import LocalCommunityThread, LocalSubscriber, RemoteSubscriber
from src.operations import SubscribeLocalCommunityInput, subscribe_local_community_operation
from support.db import add_registered_user, build_database
from support.discord import build_starter_message, build_thread


def _runtime(tmp_path: Path, name: str = "disabled-community.db") -> tuple[object, LocalCommunityRuntime]:
    """Build one local-community runtime with mocked Discord/Fedify edges."""
    database = build_database(tmp_path, name)
    gateway = AsyncMock()
    runtime = LocalCommunityRuntime(
        database=database,
        fedify_gateway=gateway,
        content_publish_service=ContentPublishService(
            database=database,
            fedify_gateway=gateway,
            bridge_prefix="[bridge]",
        ),
        bridge_prefix="[bridge]",
    )
    return database, runtime


def _local_community(database: object, *, status: str = "disabled") -> object:
    """Create one local community and force the requested lifecycle status."""
    LocalCommunityService(
        database=database,
        base_url="https://bridge.example",
        keypair_generator=lambda: ("public-key", "private-key"),
    ).create_local_community(
        discord_guild_id=10,
        discord_forum_channel_id=100,
        slug="cats",
        name="Cats",
        description="Cat talk",
        created_by_discord_user_id="123",
    )
    with database.session() as session:
        community = session.merge(database.local_communities.get_local_community_by_slug("cats"))
        community.status = status
    return database.local_communities.get_local_community_by_slug("cats")


def _post_event() -> ActivityPubEvent:
    """Build one inbound remote post targeting the disabled local community."""
    return ActivityPubEvent.model_validate(
        {
            "event_type": "post.created",
            "delivery_id": "https://remote.example/activities/post/1",
            "occurred_at": "2026-05-19T10:00:00Z",
            "community_actor_id": "https://bridge.example/communities/cats",
            "actor_id": "https://remote.example/u/alice",
            "object": {
                "ap_id": "https://remote.example/post/1",
                "kind": "post",
                "lemmy_id": 1,
                "post_ap_id": None,
                "post_lemmy_id": None,
                "parent_ap_id": None,
                "title": "Remote topic",
                "body_markdown": "hello",
                "url": "https://remote.example/post/1",
                "published_at": "2026-05-19T10:00:00Z",
                "author_name": "alice",
            },
        }
    )


def _follow_event() -> LocalCommunityFollowRequestEvent:
    """Build one remote Follow targeting the disabled local community."""
    return LocalCommunityFollowRequestEvent.model_validate(
        {
            "event_type": "local.follow_requested",
            "delivery_id": "https://remote.example/activities/follow/1",
            "occurred_at": "2026-05-19T10:00:00Z",
            "community_actor_id": "https://bridge.example/communities/cats",
            "actor_id": "https://remote.example/u/alice",
            "object": {
                "follow_activity_id": "https://remote.example/activities/follow/1",
                "remote_inbox_url": "https://remote.example/u/alice/inbox",
            },
        }
    )


@pytest.mark.asyncio
async def test_disabled_community_skips_inbound_post_without_side_effects(tmp_path: Path) -> None:
    """Inbound ActivityPub content for disabled communities is ACKed and skipped."""
    database, local_runtime = _runtime(tmp_path, "disabled-inbound.db")
    _local_community(database, status="disabled")
    runtime = SimpleNamespace(
        database=database,
        local_community_runtime=local_runtime,
        community_runtime=SimpleNamespace(),
        settings=SimpleNamespace(discord_guild_allowlist=[], federation_allowlist=[]),
    )

    result = await dispatch_activitypub_event(_post_event(), runtime)

    assert result.status == "skipped"
    assert result.detail == "community is disabled"
    assert result.outcome.value == "ignored_by_disabled_community"
    assert database.local_community_content.get_local_community_thread_by_ap_object_id("https://remote.example/post/1") is None
    local_runtime.fedify_gateway.send_local_community_relay.assert_not_awaited()


@pytest.mark.asyncio
async def test_disabled_community_skips_remote_follow_without_accept(tmp_path: Path) -> None:
    """Remote Follow to a disabled community is acknowledged without subscriber state."""
    database, local_runtime = _runtime(tmp_path, "disabled-follow.db")
    _local_community(database, status="disabled")
    runtime = SimpleNamespace(
        database=database,
        local_community_runtime=local_runtime,
        community_runtime=SimpleNamespace(),
        settings=SimpleNamespace(discord_guild_allowlist=[], federation_allowlist=[]),
    )

    result = await dispatch_activitypub_event(_follow_event(), runtime)

    assert result.status == "skipped"
    assert result.detail == "community is disabled"
    assert result.outcome.value == "ignored_by_disabled_community"
    local_runtime.fedify_gateway.accept_local_community_follow.assert_not_awaited()
    with database.session() as session:
        assert session.scalar(select(func.count()).select_from(RemoteSubscriber)) == 0


@pytest.mark.asyncio
async def test_disabled_community_blocks_discord_thread_starter(tmp_path: Path) -> None:
    """Discord-originated posts do not publish or create rows while disabled."""
    database, runtime = _runtime(tmp_path, "disabled-discord-post.db")
    _local_community(database, status="disabled")
    add_registered_user(database)

    result = await runtime.handle_discord_thread_create(
        thread=build_thread(channel_id=100),
        starter_message=build_starter_message(),
    )

    assert result.status == "ignored"
    assert result.reason == "community_disabled"
    runtime.fedify_gateway.publish_local_community_content.assert_not_awaited()
    with database.session() as session:
        assert session.scalar(select(func.count()).select_from(LocalCommunityThread)) == 0


def test_disabled_community_blocks_local_subscriber_subscription(tmp_path: Path) -> None:
    """Local subscriber rows are not created for disabled target communities."""
    database, _runtime_obj = _runtime(tmp_path, "disabled-local-subscribe.db")
    community = _local_community(database, status="disabled")
    database.users.create_user(
        discord_user_id="1234567890",
        activitypub_username="alice",
        actor_url="https://bridge.example/actors/alice",
        inbox_url="https://bridge.example/actors/alice/inbox",
        outbox_url="https://bridge.example/actors/alice/outbox",
        followers_url="https://bridge.example/actors/alice/followers",
        public_key_pem="public-key",
        private_key_pem="private-key",
    )

    result = run_operation_definition(
        subscribe_local_community_operation,
        SubscribeLocalCommunityInput(
            database=database,
            discord_user_id="1234567890",
            guild_id=10,
            channel_id=200,
            channel_mention="<#200>",
            local_community_id=community.id,
            local_community_name="cats",
        ),
    )

    assert result.applied is False
    assert result.message == "Community cats is disabled and no longer available."
    with database.session() as session:
        assert session.scalar(select(func.count()).select_from(LocalSubscriber)) == 0
        assert session.scalar(select(func.count()).select_from(RemoteSubscriber)) == 0
