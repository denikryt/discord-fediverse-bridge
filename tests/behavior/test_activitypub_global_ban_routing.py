"""Behavior coverage for global-first ActivityPub moderation routing."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.activitypub_handlers import dispatch_activitypub_event
from src.activitypub_models import LocalFollowRequestEvent
from src.db import Database
from src.local_communities.service import LocalCommunityService


def _event(*, target_kind: str = "user") -> LocalFollowRequestEvent:
    return LocalFollowRequestEvent(
        event_type="local.follow_requested",
        delivery_id="https://remote.example/follows/1",
        occurred_at=datetime.now(timezone.utc),
        target_actor_kind=target_kind,
        target_actor_id=(
            "https://bridge.example/actors/alice"
            if target_kind == "user"
            else "https://bridge.example/communities/cats"
        ),
        actor_id="https://remote.example/users/bob",
        object={
            "follow_activity_id": "https://remote.example/follows/1",
            "remote_inbox_url": "https://remote.example/users/bob/inbox",
            "remote_shared_inbox_url": "https://remote.example/inbox",
        },
    )


def _runtime(tmp_path) -> SimpleNamespace:
    database = Database(f"sqlite:///{tmp_path / 'bridge.db'}")
    database.create_all()
    database.migrate()
    database.community_actor_bans.create_active_ban(
        local_community_id=None,
        actor_handle="bob@remote.example",
        actor_url="https://remote.example/users/bob",
        created_by_discord_user_id="1",
        reason="instance-wide abuse",
    )
    return SimpleNamespace(
        database=database,
        settings=SimpleNamespace(normalized_public_base_url="https://bridge.example"),
        bridge_policy_service=SimpleNamespace(
            federation_decision=lambda _subject: SimpleNamespace(
                allowed=True, reason=SimpleNamespace(value="allowed")
            )
        ),
        user_actor_follow_service=SimpleNamespace(handle_follow=AsyncMock()),
        local_community_runtime=SimpleNamespace(handle_follow_request=AsyncMock()),
    )


@pytest.mark.asyncio
async def test_global_ban_blocks_user_follow_before_actor_kind_service(
    tmp_path,
) -> None:
    """Global policy must run before User Actor lifecycle dispatch."""
    runtime = _runtime(tmp_path)
    try:
        result = await dispatch_activitypub_event(_event(target_kind="user"), runtime)

        assert result.status == "skipped"
        assert result.outcome.value == "ignored_by_ban"
        runtime.user_actor_follow_service.handle_follow.assert_not_awaited()
    finally:
        runtime.database.close()


@pytest.mark.asyncio
async def test_global_ban_blocks_community_follow_before_community_service(
    tmp_path,
) -> None:
    """The same global policy must block community targets before scoped checks."""
    runtime = _runtime(tmp_path)
    try:
        result = await dispatch_activitypub_event(
            _event(target_kind="community"), runtime
        )

        assert result.status == "skipped"
        assert result.outcome.value == "ignored_by_ban"
        runtime.local_community_runtime.handle_follow_request.assert_not_awaited()
    finally:
        runtime.database.close()


@pytest.mark.asyncio
async def test_community_scoped_ban_does_not_block_user_actor_follow(tmp_path) -> None:
    """A community policy must not leak into a User Actor relationship."""
    database = Database(f"sqlite:///{tmp_path / 'scoped.db'}")
    database.create_all()
    database.migrate()
    LocalCommunityService(
        database=database,
        base_url="https://bridge.example",
        keypair_generator=lambda: ("public", "private"),
    ).create_local_community(
        discord_guild_id=1,
        discord_forum_channel_id=2,
        slug="cats",
        name="Cats",
        description="Cats",
        created_by_discord_user_id="1",
    )
    community = database.local_communities.get_local_community_by_slug("cats")
    assert community is not None
    database.community_actor_bans.create_active_ban(
        local_community_id=community.id,
        actor_handle="bob@remote.example",
        actor_url="https://remote.example/users/bob",
        created_by_discord_user_id="1",
        reason="community-only abuse",
    )
    follow_service = SimpleNamespace(
        handle_follow=AsyncMock(
            return_value=SimpleNamespace(
                status="processed", outcome=SimpleNamespace(value="applied")
            )
        )
    )
    runtime = SimpleNamespace(
        database=database,
        settings=SimpleNamespace(normalized_public_base_url="https://bridge.example"),
        bridge_policy_service=SimpleNamespace(
            federation_decision=lambda _subject: SimpleNamespace(
                allowed=True, reason=SimpleNamespace(value="allowed")
            )
        ),
        user_actor_follow_service=follow_service,
    )
    try:
        result = await dispatch_activitypub_event(_event(target_kind="user"), runtime)

        assert result.status == "processed"
        follow_service.handle_follow.assert_awaited_once()
    finally:
        database.close()
