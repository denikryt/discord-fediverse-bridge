"""Operation-level tests for the subscribe channel lifecycle."""

from __future__ import annotations
from src.bridge_policy import BridgePolicyService

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx

import pytest

from discordops import run_operation_definition_async

from src.operations import SubscribeInput, subscribe_operation
from tests_constants import BRIDGE_EXAMPLE_DOMAIN, LEMMY_EXAMPLE_DOMAIN


@pytest.mark.asyncio
async def test_subscribe_operation_rejects_unregistered_user() -> None:
    """Subscribe requires a registered Discord user before any follow work runs."""
    community_actor_url = f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers"
    database = Mock()
    database.bridge_policy_entries.list_all_active.return_value = []
    database.users.get_user_by_discord_user_id.return_value = None
    fedify_gateway = AsyncMock()

    result = await run_operation_definition_async(
        subscribe_operation,
        SubscribeInput(
            database=database,
            fedify_gateway=fedify_gateway,
            discord_user_id="1234567890",
            channel_id=123,
            channel_mention="<#123>",
            actor_id=community_actor_url,
            community_name="hackers",
            numeric_id=777,
            community_handle=f"!hackers@{LEMMY_EXAMPLE_DOMAIN}",
            policy_service=BridgePolicyService(
                settings=SimpleNamespace(), repository=database.bridge_policy_entries
            ),
        ),
    )

    assert result.applied is False
    assert result.reason == "discord_user_not_registered"
    assert result.message == (
        "You must register with the bridge before using this command. Use `/register` first."
    )
    fedify_gateway.follow_community.assert_not_awaited()
    database.remote_subscriptions.create_subscription.assert_not_called()


@pytest.mark.asyncio
async def test_subscribe_operation_rejects_accepted_subscription() -> None:
    """Accepted subscriptions short-circuit before another bridge follow."""
    community_actor_url = f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers"
    database = Mock()
    database.bridge_policy_entries.list_all_active.return_value = []
    database.users.get_user_by_discord_user_id.return_value = SimpleNamespace(id=1)
    database.remote_subscriptions.get_subscription_by_channel.return_value = (
        SimpleNamespace(
            status="accepted",
            community_handle=f"!hackers@{LEMMY_EXAMPLE_DOMAIN}",
            lemmy_community_name="hackers",
            lemmy_community_actor_id=community_actor_url,
        )
    )
    fedify_gateway = AsyncMock()

    result = await run_operation_definition_async(
        subscribe_operation,
        SubscribeInput(
            database=database,
            fedify_gateway=fedify_gateway,
            discord_user_id="1234567890",
            channel_id=123,
            channel_mention="<#123>",
            actor_id=community_actor_url,
            community_name="hackers",
            numeric_id=777,
            community_handle=f"!hackers@{LEMMY_EXAMPLE_DOMAIN}",
            policy_service=BridgePolicyService(
                settings=SimpleNamespace(), repository=database.bridge_policy_entries
            ),
        ),
    )

    assert result.applied is False
    assert result.reason == "channel_subscription_already_accepted"
    assert (
        result.message
        == f"Channel <#123> is already subscribed to **!hackers@{LEMMY_EXAMPLE_DOMAIN}**."
    )
    fedify_gateway.follow_community.assert_not_awaited()
    database.remote_subscriptions.create_subscription.assert_not_called()


@pytest.mark.asyncio
async def test_subscribe_operation_rejects_pending_subscription() -> None:
    """Pending subscriptions surface their waiting state without a second follow."""
    community_actor_url = f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers"
    database = Mock()
    database.bridge_policy_entries.list_all_active.return_value = []
    database.users.get_user_by_discord_user_id.return_value = SimpleNamespace(id=1)
    database.remote_subscriptions.get_subscription_by_channel.return_value = (
        SimpleNamespace(
            status="pending",
            community_handle=f"!hackers@{LEMMY_EXAMPLE_DOMAIN}",
            lemmy_community_name="hackers",
            lemmy_community_actor_id=community_actor_url,
        )
    )
    fedify_gateway = AsyncMock()

    result = await run_operation_definition_async(
        subscribe_operation,
        SubscribeInput(
            database=database,
            fedify_gateway=fedify_gateway,
            discord_user_id="1234567890",
            channel_id=123,
            channel_mention="<#123>",
            actor_id=community_actor_url,
            community_name="hackers",
            numeric_id=777,
            community_handle=f"!hackers@{LEMMY_EXAMPLE_DOMAIN}",
            policy_service=BridgePolicyService(
                settings=SimpleNamespace(), repository=database.bridge_policy_entries
            ),
        ),
    )

    assert result.applied is False
    assert result.reason == "channel_subscription_already_pending"
    assert result.message == (
        f"Channel <#123> is still waiting for **!hackers@{LEMMY_EXAMPLE_DOMAIN}** to accept the bridge follow."
    )
    fedify_gateway.follow_community.assert_not_awaited()
    database.remote_subscriptions.create_subscription.assert_not_called()


# Successful subscribe paths are SQLite integration tests: mock repositories
# could falsely pass while Follow was sent before the commit.
from pathlib import Path
from src.db import Database
from src.models import BridgeActorFollow, ChannelCommunitySubscription
from sqlalchemy import select


def _live_setup(tmp_path: Path) -> tuple[Database, str, AsyncMock, BridgePolicyService]:
    """Start a real v1 persistence boundary with fake HTTP and registered user."""
    actor = f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers"
    db = Database(f"sqlite:///{tmp_path / 'follow.db'}")
    db.migrate()
    db.users.get_user_by_discord_user_id = Mock(return_value=SimpleNamespace(id=1))
    gateway = AsyncMock()
    gateway.prepare_community_follow.return_value = SimpleNamespace(
        community_actor_url=actor,
        community_inbox_url=f"{actor}/inbox",
        follow_activity_id=f"https://{BRIDGE_EXAMPLE_DOMAIN}/activities/follow/1",
    )
    policy = BridgePolicyService(settings=SimpleNamespace(), repository=db.bridge_policy_entries)
    return db, actor, gateway, policy


async def _run_live(db: Database, actor: str, gateway: AsyncMock,
                    policy: BridgePolicyService, channel: int = 123):
    """Invoke the actual DiscordOps operation against SQLite and fake transport."""
    return await run_operation_definition_async(
        subscribe_operation,
        SubscribeInput(
            database=db, fedify_gateway=gateway,
            discord_user_id="1234567890", channel_id=channel,
            channel_mention=f"<#{channel}>", actor_id=actor,
            community_name="hackers", numeric_id=777,
            community_handle=f"!hackers@{LEMMY_EXAMPLE_DOMAIN}",
            policy_service=policy, remote_software_profile="threadiverse",
        ),
    )


@pytest.mark.asyncio
async def test_subscribe_creates_bridge_follow_when_none_exists(tmp_path: Path) -> None:
    """Network sender observes both immutable rows already committed in SQLite."""
    db, actor, gateway, policy = _live_setup(tmp_path)
    async def send(prepared):
        """Assert durable actor and channel identity before any HTTP delivery."""
        follow = db.bridge_actor_follows.get_bridge_actor_follow(actor)
        sub = db.remote_subscriptions.get_subscription_by_channel(123)
        assert follow.follow_activity_id == prepared.follow_activity_id
        assert follow.follow_dispatch_state == "sending"
        assert sub.follow_activity_id == follow.follow_activity_id
        assert sub.status == "pending"
    gateway.send_prepared_community_follow.side_effect = send
    try:
        result = await _run_live(db, actor, gateway, policy)
        assert result.applied and result.reason == "pending_remote_confirmation"
        gateway.follow_community.assert_not_awaited()
        gateway.prepare_community_follow.assert_awaited_once_with(actor)
        gateway.send_prepared_community_follow.assert_awaited_once()
        row = db.bridge_actor_follows.get_bridge_actor_follow(actor)
        assert row.follow_dispatch_state == "sent"
        assert row.remote_software_profile == "threadiverse"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_subscribe_reuses_existing_bridge_follow_when_accepted(tmp_path: Path) -> None:
    """A second channel inherits accepted generation without sending again."""
    db, actor, gateway, policy = _live_setup(tmp_path)
    try:
        assert (await _run_live(db, actor, gateway, policy)).applied
        original = db.bridge_actor_follows.get_bridge_actor_follow(actor)
        assert db.bridge_actor_follows.accept_verified_follow(
            follow_activity_id=original.follow_activity_id, actor_id=actor
        )
        result = await _run_live(db, actor, gateway, policy, channel=456)
        assert result.applied and "Subscribed" in result.message
        assert gateway.send_prepared_community_follow.await_count == 1
        assert db.remote_subscriptions.get_subscription_by_channel(456).status == "accepted"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_subscribe_reuses_existing_bridge_follow_when_pending(tmp_path: Path) -> None:
    """A second pending channel cannot allocate or send another Follow ID."""
    db, actor, gateway, policy = _live_setup(tmp_path)
    try:
        assert (await _run_live(db, actor, gateway, policy)).applied
        result = await _run_live(db, actor, gateway, policy, channel=789)
        assert result.applied and "Waiting for federation acceptance" in result.message
        assert gateway.send_prepared_community_follow.await_count == 1
        with db.session() as session:
            assert len(session.scalars(select(BridgeActorFollow)).all()) == 1
            subs = session.scalars(select(ChannelCommunitySubscription)).all()
            assert len(subs) == 2
            assert len({row.follow_activity_id for row in subs}) == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_subscribe_operation_marks_failed_when_follow_dispatch_fails(tmp_path: Path) -> None:
    """Ambiguous gateway errors preserve the pending generation, not a false failure."""
    db, actor, gateway, policy = _live_setup(tmp_path)
    gateway.send_prepared_community_follow.side_effect = TimeoutError("after send")
    try:
        result = await _run_live(db, actor, gateway, policy)
        assert result.applied and result.reason == "pending_remote_confirmation"
        row = db.bridge_actor_follows.get_bridge_actor_follow(actor)
        assert row.follow_dispatch_state == "sending"
        assert row.follow_activity_id
        assert db.remote_subscriptions.get_subscription_by_channel(123).status == "pending"
        gateway.follow_community.assert_not_awaited()
    finally:
        db.close()


@pytest.mark.asyncio
async def test_follow_actor_resolution_failure_is_logged_without_losing_generation(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """A gateway sender-lookup error is visible; the committed Follow is not replaced.

    The internal gateway returns the real Fedify failure. Since generic HTTP
    failures are potentially ambiguous, this operation keeps the original
    Follow ID and its pending binding rather than allocating a second Follow.
    """
    db, actor, gateway, policy = _live_setup(tmp_path)
    request = httpx.Request("POST", "http://gateway.test/send-prepared-community-follow")
    response = httpx.Response(
        500,
        json={"error": 'No actor found for the given username "services/bridge".'},
        request=request,
    )
    gateway.send_prepared_community_follow.side_effect = httpx.HTTPStatusError(
        "Follow sender resolution failed", request=request, response=response,
    )
    try:
        result = await _run_live(db, actor, gateway, policy)
        assert not result.applied and result.reason == "follow_sender_resolution_failed"
        assert "Follow was not dispatched" in result.message
        assert 'No actor found for the given username "services/bridge".' in caplog.text
        follow = db.bridge_actor_follows.get_bridge_actor_follow(actor)
        assert follow.follow_dispatch_state == "sending"
        assert follow.follow_activity_id == gateway.prepare_community_follow.return_value.follow_activity_id
        assert db.remote_subscriptions.get_subscription_by_channel(123).status == "pending"
        gateway.send_prepared_community_follow.assert_awaited_once()
    finally:
        db.close()


@pytest.mark.asyncio
async def test_subscribe_operation_retries_failed_subscription(tmp_path: Path) -> None:
    """Only legacy failed rows with no Follow ID may be replaced safely."""
    db, actor, gateway, policy = _live_setup(tmp_path)
    try:
        db.bridge_actor_follows.create_bridge_actor_follow(
            community_actor_id=actor, follow_activity_id=None,
            community_inbox_url=None, status="failed",
        )
        db.remote_subscriptions.create_subscription(
            discord_channel_id=123, lemmy_community_actor_id=actor,
            lemmy_community_name="hackers", lemmy_community_id=777,
            follow_activity_id=None, status="failed",
        )
        result = await _run_live(db, actor, gateway, policy)
        assert result.applied
        row = db.bridge_actor_follows.get_bridge_actor_follow(actor)
        assert row.status == "pending" and row.follow_activity_id
        assert gateway.send_prepared_community_follow.await_count == 1
        with db.session() as session:
            assert len(session.scalars(select(BridgeActorFollow)).all()) == 1
            assert len(session.scalars(select(ChannelCommunitySubscription)).all()) == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_subscribe_preparation_failure_leaves_no_relationship(tmp_path: Path) -> None:
    """Failed actor verification has zero persistent or remote side effects."""
    db, actor, gateway, policy = _live_setup(tmp_path)
    gateway.prepare_community_follow.side_effect = TimeoutError("actor fetch")
    try:
        result = await _run_live(db, actor, gateway, policy)
        assert not result.applied and result.reason == "follow_prepare_failed"
        assert db.bridge_actor_follows.get_bridge_actor_follow(actor) is None
        assert db.remote_subscriptions.get_subscription_by_channel(123) is None
        gateway.send_prepared_community_follow.assert_not_awaited()
    finally:
        db.close()
