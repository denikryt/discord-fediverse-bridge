"""Executable typed subscription lifecycle contracts."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from discordops import run_operation_definition_async

from src.bridge_policy import BridgePolicyService
from src.db import Database
from src.models import BridgeActorFollow, ChannelCommunitySubscription
from sqlalchemy import select
from pathlib import Path
from src.fedify_gateway_client import UnfollowCommunityResult
from src.operations import (
    SubscribeInput,
    UnsubscribeInput,
    subscribe_operation,
    unsubscribe_operation,
)
from support.subscription_contracts import SUBSCRIPTION_CASES, SubscriptionCase

ACTOR_ID = "https://lemmy.example/c/hackers"
FOLLOW_ACTIVITY_ID = "https://bridge.example/actors/services/bridge/activities/follow/1"
CHANNEL_ID = 123
CHANNEL_MENTION = "<#123>"
COMMUNITY_HANDLE = "!hackers@lemmy.example"


def _subscription_row(case: SubscriptionCase) -> SimpleNamespace | None:
    """Build the existing channel-subscription row described by the case."""

    if case.channel_state == "missing":
        return None

    return SimpleNamespace(
        status=case.channel_state,
        community_handle=COMMUNITY_HANDLE,
        lemmy_community_name="hackers",
        lemmy_community_actor_id=ACTOR_ID,
    )


def _follow_row(case: SubscriptionCase) -> SimpleNamespace | None:
    """Build the shared Follow row described by the case."""

    if case.follow_state == "missing":
        return None

    status = (
        "accepted"
        if case.follow_state in {"accepted", "missing_id"}
        else case.follow_state
    )
    follow_activity_id = (
        None if case.follow_state == "missing_id" else FOLLOW_ACTIVITY_ID
    )
    return SimpleNamespace(
        status=status,
        community_actor_id=ACTOR_ID,
        community_inbox_url=f"{ACTOR_ID}/inbox",
        follow_activity_id=follow_activity_id,
    )


def _database(case: SubscriptionCase) -> Mock:
    """Configure the repository facade for one subscription contract case."""

    database = Mock()
    database.bridge_policy_entries.list_all_active.return_value = []
    database.users.get_user_by_discord_user_id.return_value = (
        SimpleNamespace(id=1) if case.registered else None
    )
    database.remote_subscriptions.get_subscription_by_channel.return_value = (
        _subscription_row(case)
    )
    database.remote_subscriptions.count_subscriptions_for_community.return_value = (
        case.subscription_count
    )
    database.bridge_actor_follows.get_bridge_actor_follow.return_value = _follow_row(
        case
    )
    database.remote_subscriptions.delete_subscription.return_value = True
    return database


def _gateway(case: SubscriptionCase) -> AsyncMock:
    """Configure the external gateway outcomes declared by the case."""

    gateway = AsyncMock()
    gateway.follow_community.return_value = SimpleNamespace(
        community_actor_url=ACTOR_ID,
        community_inbox_url=f"{ACTOR_ID}/inbox",
        follow_activity_id=FOLLOW_ACTIVITY_ID,
    )
    gateway.unfollow_community.return_value = UnfollowCommunityResult(
        accepted=case.remote_outcome == "accepted",
        error="network" if case.remote_outcome == "failed" else None,
    )
    return gateway


async def _execute_case(
    case: SubscriptionCase,
    database: Mock,
    gateway: AsyncMock,
):
    """Run the real subscribe or unsubscribe operation for one case."""

    policy = BridgePolicyService(
        settings=SimpleNamespace(),
        repository=database.bridge_policy_entries,
    )
    if case.action == "subscribe":
        operation_input = SubscribeInput(
            database=database,
            fedify_gateway=gateway,
            discord_user_id=str(CHANNEL_ID),
            channel_id=CHANNEL_ID,
            channel_mention=CHANNEL_MENTION,
            actor_id=ACTOR_ID,
            community_name="hackers",
            numeric_id=42,
            community_handle=COMMUNITY_HANDLE,
            policy_service=policy,
        )
        return await run_operation_definition_async(
            subscribe_operation,
            operation_input,
        )

    operation_input = UnsubscribeInput(
        database=database,
        fedify_gateway=gateway,
        channel_id=CHANNEL_ID,
        channel_mention=CHANNEL_MENTION,
        policy_service=policy,
    )
    return await run_operation_definition_async(
        unsubscribe_operation,
        operation_input,
    )


def _assert_effects(
    case: SubscriptionCase,
    result: object,
    database: Mock,
    gateway: AsyncMock,
) -> None:
    """Assert the complete observable operation and persistence effects."""

    expected = case.expected
    assert result.applied is expected.applied
    assert result.reason == expected.reason
    assert gateway.follow_community.await_count == expected.follow_calls
    assert gateway.unfollow_community.await_count == expected.unfollow_calls
    assert (
        database.remote_subscriptions.create_subscription.call_count
        == expected.create_channel_calls
    )
    assert (
        database.remote_subscriptions.delete_subscription.call_count
        == expected.delete_channel_calls
    )
    assert (
        database.bridge_actor_follows.delete_bridge_actor_follow.call_count
        == expected.delete_follow_calls
    )


@pytest.mark.parametrize("case", [row for row in SUBSCRIPTION_CASES if row.action == "unsubscribe"], ids=lambda case: case.id)
@pytest.mark.asyncio
async def test_subscription_contract(case: SubscriptionCase) -> None:
    """Execute one declared subscription lifecycle contract."""

    database = _database(case)
    gateway = _gateway(case)

    result = await _execute_case(case, database, gateway)

    _assert_effects(case, result, database, gateway)


@pytest.mark.parametrize("case", [row for row in SUBSCRIPTION_CASES if row.action == "subscribe"], ids=lambda case: case.id)
@pytest.mark.asyncio
async def test_subscribe_contract_with_real_sqlite(case: SubscriptionCase, tmp_path: Path) -> None:
    """Check declared subscription outcomes against the real atomic v1 writer."""
    database = Database(f"sqlite:///{tmp_path / 'follow.db'}")
    database.migrate()
    database.users.get_user_by_discord_user_id = Mock(return_value=(
        SimpleNamespace(id=1) if case.registered else None
    ))
    if case.follow_state != "missing":
        follow = database.bridge_actor_follows.create_bridge_actor_follow(
            community_actor_id=ACTOR_ID,
            follow_activity_id=FOLLOW_ACTIVITY_ID,
            community_inbox_url=f"{ACTOR_ID}/inbox",
            status=case.follow_state,
        )
        with database.immediate_session() as session:
            # Pre-existing accepted/pending generations have already passed
            # their sender claim; they are not newly prepared activities.
            session.get(BridgeActorFollow, follow.id).follow_dispatch_state = "sent"
    if case.channel_state != "missing":
        database.remote_subscriptions.create_subscription(
            discord_channel_id=CHANNEL_ID,
            lemmy_community_actor_id=ACTOR_ID,
            lemmy_community_name="hackers", lemmy_community_id=42,
            follow_activity_id=FOLLOW_ACTIVITY_ID,
            status=case.channel_state,
        )
    elif case.follow_state != "missing":
        # The shared Follow already has one independent channel binding.
        database.remote_subscriptions.create_subscription(
            discord_channel_id=456,
            lemmy_community_actor_id=ACTOR_ID,
            lemmy_community_name="hackers", lemmy_community_id=42,
            follow_activity_id=FOLLOW_ACTIVITY_ID,
            status=case.follow_state,
        )

    gateway = AsyncMock()
    gateway.prepare_community_follow.return_value = SimpleNamespace(
        community_actor_url=ACTOR_ID,
        community_inbox_url=f"{ACTOR_ID}/inbox",
        follow_activity_id=FOLLOW_ACTIVITY_ID,
    )
    policy = BridgePolicyService(settings=SimpleNamespace(), repository=database.bridge_policy_entries)
    try:
        result = await run_operation_definition_async(
            subscribe_operation,
            SubscribeInput(
                database=database, fedify_gateway=gateway,
                discord_user_id=str(CHANNEL_ID), channel_id=CHANNEL_ID,
                channel_mention=CHANNEL_MENTION,
                actor_id=ACTOR_ID, community_name="hackers", numeric_id=42,
                community_handle=COMMUNITY_HANDLE, policy_service=policy,
                remote_software_profile="threadiverse",
            ),
        )
        assert result.applied is case.expected.applied
        assert result.reason == case.expected.reason
        assert gateway.follow_community.await_count == 0
        assert gateway.send_prepared_community_follow.await_count == case.expected.follow_calls
        with database.session() as session:
            channel = session.scalar(select(ChannelCommunitySubscription).where(
                ChannelCommunitySubscription.discord_channel_id == CHANNEL_ID
            ))
            assert (channel is not None) is (case.channel_state != "missing" or case.expected.applied)
            expected_follows = 1 if case.follow_state != "missing" or case.expected.applied else 0
            assert len(session.scalars(select(BridgeActorFollow)).all()) == expected_follows
    finally:
        database.close()
