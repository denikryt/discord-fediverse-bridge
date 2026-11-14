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


@pytest.mark.parametrize(
    "case", [row for row in SUBSCRIPTION_CASES if row.action == "unsubscribe"],
    ids=lambda case: case.id,
)
@pytest.mark.asyncio
async def test_subscription_contract(case: SubscriptionCase, tmp_path: Path) -> None:
    """Exercise each unsubscribe contract through SQLite CAS and DiscordOps.

    Unlike the old mock-count contract, this asserts that the affected rows
    actually survive or disappear with the correct Follow generation.
    """
    database = Database(f"sqlite:///{tmp_path / 'unsubscribe-contract.db'}")
    database.migrate()
    gateway = AsyncMock()
    gateway.unfollow_community.return_value = UnfollowCommunityResult(
        accepted=case.remote_outcome == "accepted",
        error="outcome unknown" if case.remote_outcome == "failed" else None,
    )
    policy = BridgePolicyService(
        settings=SimpleNamespace(), repository=database.bridge_policy_entries,
    )
    try:
        if case.channel_state != "missing":
            follow = database.bridge_actor_follows.create_bridge_actor_follow(
                community_actor_id=ACTOR_ID,
                community_inbox_url=f"{ACTOR_ID}/inbox",
                follow_activity_id=(
                    None if case.follow_state == "missing_id" else FOLLOW_ACTIVITY_ID
                ),
                status="accepted",
            )
            with database.immediate_session() as session:
                # A successful historical Follow send is distinguishable from
                # the prepared state. Last-channel Undo is only allowed here.
                session.get(BridgeActorFollow, follow.id).follow_dispatch_state = "sent"
            database.remote_subscriptions.create_subscription(
                discord_channel_id=CHANNEL_ID,
                lemmy_community_actor_id=ACTOR_ID,
                lemmy_community_name="hackers", lemmy_community_id=42,
                follow_activity_id=follow.follow_activity_id,
                status="accepted",
            )
            if case.subscription_count > 1:
                database.remote_subscriptions.create_subscription(
                    discord_channel_id=456,
                    lemmy_community_actor_id=ACTOR_ID,
                    lemmy_community_name="hackers", lemmy_community_id=42,
                    follow_activity_id=follow.follow_activity_id,
                    status="accepted",
                )

        result = await run_operation_definition_async(
            unsubscribe_operation,
            UnsubscribeInput(
                database=database, fedify_gateway=gateway,
                channel_id=CHANNEL_ID, channel_mention=CHANNEL_MENTION,
                policy_service=policy,
            ),
        )
        assert result.applied is case.expected.applied
        assert result.reason == case.expected.reason
        assert gateway.unfollow_community.await_count == case.expected.unfollow_calls
        with database.session() as session:
            channel = session.scalar(select(ChannelCommunitySubscription).where(
                ChannelCommunitySubscription.discord_channel_id == CHANNEL_ID
            ))
            others = list(session.scalars(select(ChannelCommunitySubscription).where(
                ChannelCommunitySubscription.discord_channel_id == 456
            )))
            follows = list(session.scalars(select(BridgeActorFollow)))
            assert (channel is not None) is (
                case.channel_state != "missing" and case.follow_state == "missing_id"
            )
            assert len(others) == int(case.subscription_count > 1)
            assert len(follows) == int(
                case.channel_state != "missing" and
                (case.follow_state == "missing_id" or
                 case.subscription_count > 1 or
                 case.remote_outcome == "failed")
            )
            if case.remote_outcome == "failed":
                assert follows[0].follow_dispatch_state == "undo_sending"
                assert follows[0].cancel_requested
    finally:
        database.close()


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
