"""Regression tests for ignoring remote content after unsubscribe."""

from __future__ import annotations
from support.runtime import build_test_policy_service

from pathlib import Path
from types import SimpleNamespace

import pytest

from src.activitypub_handlers import HandlerResult, dispatch_activitypub_event
from src.inbound_activity_outcomes import InboundActivityOutcome
from support.activitypub import build_comment_created_event, build_post_created_event
from support.db import build_database


class _NoopCommunityRuntime:
    """Record whether inbound content reached the remote runtime."""
    def __init__(self) -> None:
        self.post_events = []
        self.comment_events = []
    async def handle_inbound_post(self, event, runtime):
        """Record one post event."""
        self.post_events.append(event)
        return HandlerResult(status="processed", outcome=InboundActivityOutcome.APPLIED, detail="post created")
    async def handle_inbound_comment(self, event, runtime):
        """Record one comment event."""
        self.comment_events.append(event)
        return HandlerResult(status="processed", outcome=InboundActivityOutcome.APPLIED, detail="comment created")


class _NoopLocalCommunityRuntime:
    """Reject incorrect local-community routing in these tests."""
    async def handle_inbound_post(self, event, runtime):
        """Reject unexpected local post routing."""
        raise AssertionError("local community post route should not be used")
    async def handle_inbound_comment(self, event, runtime):
        """Reject unexpected local comment routing."""
        raise AssertionError("local community comment route should not be used")


def _runtime(database, community_runtime):
    """Build the minimal runtime namespace used by dispatch."""
    return SimpleNamespace(database=database, community_runtime=community_runtime, local_community_runtime=_NoopLocalCommunityRuntime(), settings=SimpleNamespace(federation_allowlist=[]), bot=SimpleNamespace(), bridge_policy_service=build_test_policy_service(database, SimpleNamespace(federation_allowlist=[])))


@pytest.mark.asyncio
async def test_unsubscribed_remote_post_create_is_skipped_before_thread_creation(tmp_path: Path) -> None:
    """Unmapped remote posts from unsubscribed communities should not create threads."""
    database = build_database(tmp_path, "unsubscribed-post.sqlite3")
    community_runtime = _NoopCommunityRuntime()
    event = build_post_created_event(object_id="https://remote.example/post/1")
    result = await dispatch_activitypub_event(event, _runtime(database, community_runtime))
    assert result.status == "skipped"
    assert result.detail == "no subscriptions for this community"
    assert result.outcome.value == "ignored_no_subscription"
    assert database.discord_fanout_groups.get_thread_group_by_ap_object(event.object.ap_id) is None
    assert community_runtime.post_events == []


@pytest.mark.asyncio
async def test_unsubscribed_remote_comment_without_mapped_context_is_skipped(tmp_path: Path) -> None:
    """Unmapped orphan comments after unsubscribe should not backfill or fan out."""
    database = build_database(tmp_path, "unsubscribed-comment.sqlite3")
    community_runtime = _NoopCommunityRuntime()
    event = build_comment_created_event(object_id="https://remote.example/comment/1", post_ap_id="https://remote.example/post/1", parent_ap_id="https://remote.example/comment/parent")
    result = await dispatch_activitypub_event(event, _runtime(database, community_runtime))
    assert result.status == "skipped"
    assert result.detail == "no subscriptions for this community"
    assert result.outcome.value == "ignored_no_subscription"
    assert database.discord_fanout_groups.get_message_group_by_ap_object(event.object.ap_id) is None
    assert database.discord_fanout_groups.get_thread_group_by_ap_object(event.object.post_ap_id) is None
    assert community_runtime.comment_events == []


@pytest.mark.asyncio
async def test_pending_follow_still_implicit_accepts_before_unsubscribed_skip(tmp_path: Path) -> None:
    """Inbound content for a pending follow should promote state before skip checks."""
    database = build_database(tmp_path, "implicit-accept.sqlite3")
    community_actor_id = "https://remote.example/c/news"
    follow_activity_id = "https://bridge.example/activities/follow/1"
    database.bridge_actor_follows.create_bridge_actor_follow(community_actor_id=community_actor_id, follow_activity_id=follow_activity_id, community_inbox_url=f"{community_actor_id}/inbox", status="pending")
    database.remote_subscriptions.create_subscription(discord_channel_id=123, lemmy_community_actor_id=community_actor_id, lemmy_community_name="news", lemmy_community_id=456, community_handle="!news@remote.example", community_inbox_url=f"{community_actor_id}/inbox", follow_activity_id=follow_activity_id, status="pending")
    community_runtime = _NoopCommunityRuntime()
    event = build_post_created_event(object_id="https://remote.example/post/2", community_actor_id=community_actor_id)
    result = await dispatch_activitypub_event(event, _runtime(database, community_runtime))
    assert result.status == "processed"
    assert database.bridge_actor_follows.get_bridge_actor_follow(community_actor_id).status == "accepted"
    assert database.remote_subscriptions.get_subscriptions_by_community(community_actor_id)[0].status == "accepted"
    assert community_runtime.post_events == [event]
