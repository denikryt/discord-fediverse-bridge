"""Executable typed subscription lifecycle contracts."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from discordops import run_operation_definition_async

from src.bridge_policy import BridgePolicyService
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


def _database(case: SubscriptionCase, tmp_path):
    """Persist each declared precondition through real transactional repositories."""
    from src.db import Database

    database = Database(f"sqlite:///{tmp_path / 'contract.db'}")
    database.migrate()
    if case.registered:
        user_actor = "https://bridge.example/actors/users/moderator"
        database.users.create_user(
            discord_user_id=str(CHANNEL_ID),
            activitypub_username="moderator",
            actor_url=user_actor,
            inbox_url=user_actor + "/inbox",
            outbox_url=user_actor + "/outbox",
            followers_url=user_actor + "/followers",
            public_key_pem="public",
            private_key_pem="private",
        )
    if case.follow_state != "missing":
        database.bridge_actor_follows.create_bridge_actor_follow(
            community_actor_id=ACTOR_ID,
            follow_activity_id=(
                None if case.follow_state == "missing_id" else FOLLOW_ACTIVITY_ID
            ),
            community_inbox_url=ACTOR_ID + "/inbox",
            status=(
                "accepted" if case.follow_state == "missing_id" else case.follow_state
            ),
        )
    if case.channel_state != "missing":
        database.remote_subscriptions.create_subscription(
            discord_channel_id=CHANNEL_ID,
            lemmy_community_actor_id=ACTOR_ID,
            lemmy_community_name="hackers",
            lemmy_community_id=42,
            community_handle=COMMUNITY_HANDLE,
            follow_activity_id=(
                None if case.follow_state == "missing_id" else FOLLOW_ACTIVITY_ID
            ),
            status=case.channel_state,
        )
    count = database.remote_subscriptions.count_subscriptions_for_community(ACTOR_ID)
    for offset in range(case.subscription_count - count):
        database.remote_subscriptions.create_subscription(
            discord_channel_id=CHANNEL_ID + 1 + offset,
            lemmy_community_actor_id=ACTOR_ID,
            lemmy_community_name="hackers",
            lemmy_community_id=42,
            follow_activity_id=FOLLOW_ACTIVITY_ID,
            status="accepted",
        )
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


def _assert_effects(case, result, database, gateway, before):
    """Compare actual durable row changes and external requests, not DAO call counts."""
    expected = case.expected
    assert result.applied is expected.applied and result.reason == expected.reason
    assert gateway.follow_community.await_count == expected.follow_calls
    assert gateway.unfollow_community.await_count == expected.unfollow_calls
    after_channel = len(database.remote_subscriptions.list_subscriptions())
    after_follow = len(database.bridge_actor_follows.list_bridge_actor_follows())
    assert (
        after_channel - before[0]
        == expected.created_channel_rows - expected.deleted_channel_rows
    )
    if expected.deleted_follow_rows:
        assert before[1] - after_follow == expected.deleted_follow_rows
    if case.action == "subscribe" and result.applied:
        row = database.remote_subscriptions.get_subscription_by_channel(CHANNEL_ID)
        assert (
            database.platform_surfaces.resolve_claim(
                "discord-primary", str(CHANNEL_ID)
            ).remote_subscription_id
            == row.id
        )
    with database.engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []


@pytest.mark.parametrize("case", SUBSCRIPTION_CASES, ids=lambda case: case.id)
@pytest.mark.asyncio
async def test_subscription_contract(case: SubscriptionCase, tmp_path) -> None:
    """Execute one declared subscription lifecycle contract."""

    database = _database(case, tmp_path)
    gateway = _gateway(case)

    before = (
        len(database.remote_subscriptions.list_subscriptions()),
        len(database.bridge_actor_follows.list_bridge_actor_follows()),
    )
    result = await _execute_case(case, database, gateway)

    _assert_effects(case, result, database, gateway, before)
    database.close()
