"""Executable typed subscription lifecycle contracts."""
from __future__ import annotations
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import pytest
from discordops import run_operation_definition_async
from src.bridge_policy import BridgePolicyService
from src.fedify_gateway_client import UnfollowCommunityResult
from src.operations import SubscribeInput, UnsubscribeInput, subscribe_operation, unsubscribe_operation
from support.subscription_contracts import SUBSCRIPTION_CASES, SubscriptionCase

ACTOR='https://lemmy.example/c/hackers'
FOLLOW='https://bridge.example/activities/follow/1'

def _db(case:SubscriptionCase)->Mock:
    db=Mock(); db.bridge_policy_entries.list_all_active.return_value=[]
    db.users.get_user_by_discord_user_id.return_value=SimpleNamespace(id=1) if case.registered else None
    if case.channel_state=='missing': sub=None
    else: sub=SimpleNamespace(status=case.channel_state,community_handle='!hackers@lemmy.example',lemmy_community_name='hackers',lemmy_community_actor_id=ACTOR)
    db.remote_subscriptions.get_subscription_by_channel.return_value=sub
    db.remote_subscriptions.count_subscriptions_for_community.return_value=case.subscription_count
    if case.follow_state=='missing': follow=None
    else: follow=SimpleNamespace(status='accepted' if case.follow_state in {'accepted','missing_id'} else case.follow_state,community_actor_id=ACTOR,community_inbox_url=f'{ACTOR}/inbox',follow_activity_id=None if case.follow_state=='missing_id' else FOLLOW)
    db.bridge_actor_follows.get_bridge_actor_follow.return_value=follow
    db.remote_subscriptions.delete_subscription.return_value=True
    return db

@pytest.mark.parametrize('case',SUBSCRIPTION_CASES,ids=lambda c:c.id)
@pytest.mark.asyncio
async def test_subscription_contract(case:SubscriptionCase)->None:
    db=_db(case); gateway=AsyncMock()
    gateway.follow_community.return_value=SimpleNamespace(community_actor_url=ACTOR,community_inbox_url=f'{ACTOR}/inbox',follow_activity_id=FOLLOW)
    gateway.unfollow_community.return_value=UnfollowCommunityResult(accepted=case.remote_outcome=='accepted',error='network' if case.remote_outcome=='failed' else None)
    policy=BridgePolicyService(settings=SimpleNamespace(),repository=db.bridge_policy_entries)
    if case.action=='subscribe':
        result=await run_operation_definition_async(subscribe_operation,SubscribeInput(db,gateway,'123',123,'<#123>',ACTOR,'hackers',42,'!hackers@lemmy.example',policy))
    else:
        result=await run_operation_definition_async(unsubscribe_operation,UnsubscribeInput(db,gateway,123,'<#123>',policy))
    e=case.expected
    assert result.applied is e.applied; assert result.reason==e.reason
    assert gateway.follow_community.await_count==e.follow_calls
    assert gateway.unfollow_community.await_count==e.unfollow_calls
    assert db.remote_subscriptions.create_subscription.call_count==e.create_channel_calls
    assert db.remote_subscriptions.delete_subscription.call_count==e.delete_channel_calls
    assert db.bridge_actor_follows.delete_bridge_actor_follow.call_count==e.delete_follow_calls
