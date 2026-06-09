"""Typed deterministic contracts for channel subscription lifecycle."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Literal
Action=Literal['subscribe','unsubscribe']
ChannelState=Literal['missing','accepted','pending','failed']
FollowState=Literal['missing','accepted','pending','failed','missing_id']
RemoteOutcome=Literal['accepted','failed','not_called']
@dataclass(frozen=True,slots=True)
class SubscriptionExpected:
    applied: bool
    reason: str | None
    follow_calls: int=0
    unfollow_calls: int=0
    create_channel_calls: int=0
    delete_channel_calls: int=0
    delete_follow_calls: int=0
@dataclass(frozen=True,slots=True)
class SubscriptionCase:
    id:str; action:Action; registered:bool; channel_state:ChannelState; follow_state:FollowState; subscription_count:int; remote_outcome:RemoteOutcome; expected:SubscriptionExpected
SUBSCRIPTION_CASES=(
    SubscriptionCase('subscribe.unregistered.rejected','subscribe',False,'missing','missing',0,'not_called',SubscriptionExpected(False,'discord_user_not_registered')),
    SubscriptionCase('subscribe.accepted.rejected','subscribe',True,'accepted','accepted',1,'not_called',SubscriptionExpected(False,'channel_subscription_already_accepted')),
    SubscriptionCase('subscribe.pending.rejected','subscribe',True,'pending','pending',1,'not_called',SubscriptionExpected(False,'channel_subscription_already_pending')),
    SubscriptionCase('subscribe.first.follow_pending','subscribe',True,'missing','missing',0,'accepted',SubscriptionExpected(True,None,follow_calls=1,create_channel_calls=1)),
    SubscriptionCase('subscribe.reuse_accepted','subscribe',True,'missing','accepted',1,'not_called',SubscriptionExpected(True,None,create_channel_calls=1)),
    SubscriptionCase('subscribe.reuse_pending','subscribe',True,'missing','pending',1,'not_called',SubscriptionExpected(True,None,create_channel_calls=1)),
    SubscriptionCase('unsubscribe.missing.rejected','unsubscribe',True,'missing','missing',0,'not_called',SubscriptionExpected(False,'channel_subscription_not_found')),
    SubscriptionCase('unsubscribe.non_last.local_only','unsubscribe',True,'accepted','accepted',2,'not_called',SubscriptionExpected(True,None,delete_channel_calls=1)),
    SubscriptionCase('unsubscribe.last.success','unsubscribe',True,'accepted','accepted',1,'accepted',SubscriptionExpected(True,None,unfollow_calls=1,delete_channel_calls=1,delete_follow_calls=1)),
    SubscriptionCase('unsubscribe.last.remote_failure','unsubscribe',True,'accepted','accepted',1,'failed',SubscriptionExpected(False,'remote_unfollow_failed',unfollow_calls=1,delete_channel_calls=1)),
    SubscriptionCase('unsubscribe.last.missing_follow_id','unsubscribe',True,'accepted','missing_id',1,'not_called',SubscriptionExpected(False,'follow_activity_id_missing')),
)
@dataclass(frozen=True,slots=True)
class RequiredRule:
    id:str; description:str; represented_by:tuple[str,...]
REQUIRED_SUBSCRIPTION_RULES=(
 RequiredRule('registration_gate','Subscribe requires bridge registration.',('subscribe.unregistered.rejected',)),
 RequiredRule('existing_channel_states','Accepted and pending channel states reject duplicate subscribe.',('subscribe.accepted.rejected','subscribe.pending.rejected')),
 RequiredRule('follow_creation','First subscription creates a Follow-backed channel row.',('subscribe.first.follow_pending',)),
 RequiredRule('follow_reuse','Existing accepted/pending bridge follows are reused.',('subscribe.reuse_accepted','subscribe.reuse_pending')),
 RequiredRule('missing_unsubscribe','Missing channel subscription is rejected.',('unsubscribe.missing.rejected',)),
 RequiredRule('shared_follow_retained','Non-last unsubscribe removes only channel state.',('unsubscribe.non_last.local_only',)),
 RequiredRule('last_unfollow_success','Last unsubscribe removes shared follow after successful Undo.',('unsubscribe.last.success',)),
 RequiredRule('last_unfollow_retry','Remote failure preserves shared follow for retry.',('unsubscribe.last.remote_failure',)),
 RequiredRule('missing_follow_id_safe','Missing Follow ID blocks unsafe local cleanup.',('unsubscribe.last.missing_follow_id',)),
)
