"""Local Service Follow generations remain safe across Accept failures and Undo.

Each scenario uses a real SQLite repository and the production follower handler;
only Fedify delivery and policy/network access are faked.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.db import Database
from src.inbound_activity_outcomes import InboundActivityOutcome
from src.local_communities.followers import LocalCommunityFollowerHandler
from src.local_communities.service import LocalCommunityService

ACTOR = "https://remote.example/actors/services/bridge"
OLD = "https://remote.example/follows/old"
NEW = "https://remote.example/follows/new"
INBOX = "https://remote.example/actors/services/bridge/inbox"


def _scenario(tmp_path: Path):
    """Create a public local community and a fake signed gateway boundary."""
    db = Database(f"sqlite:///{tmp_path / 'follow-generations.db'}")
    db.create_all()
    LocalCommunityService(
        database=db, base_url="https://bridge.example",
        keypair_generator=lambda: ("public-key", "private-key"),
    ).create_local_community(
        discord_guild_id=10, discord_forum_channel_id=100,
        slug="hackers", name="Hackers", description="Public forum",
        created_by_discord_user_id="123",
    )
    community = db.local_communities.get_local_community_by_slug("hackers")
    gateway = AsyncMock()
    policy = SimpleNamespace(federation_decision=lambda _: SimpleNamespace(allowed=True))
    handler = LocalCommunityFollowerHandler(
        database=db, fedify_gateway=gateway, bridge_policy_service=policy
    )
    return db, community, handler, gateway


async def _follow(handler, community, follow_id, *, is_retry=False, inbox=INBOX):
    """Deliver one verified Follow to the production local-Group handler."""
    return await handler.handle_follow_request(
        local_community_actor_id=community.actor_url, remote_actor_id=ACTOR,
        remote_inbox_url=inbox, follow_activity_id=follow_id, is_retry=is_retry,
    )


async def _undo(handler, community, follow_id):
    """Deliver an authenticated Undo for an original Follow identity."""
    return await handler.handle_unfollow_request(
        local_community_actor_id=community.actor_url, remote_actor_id=ACTOR,
        follow_activity_id=follow_id,
    )


@pytest.mark.asyncio
async def test_b4_stale_or_null_undo_does_not_delete_new_generation(tmp_path):
    """Undo(old/NULL) cannot erase a later accepted Follow(new)."""
    db, community, handler, gateway = _scenario(tmp_path)
    await _follow(handler, community, OLD)
    await _follow(handler, community, NEW)
    for attempt in (OLD, None, "https://remote.example/follows/foreign"):
        result = await _undo(handler, community, attempt)
        assert result.status == "skipped"
        assert result.outcome is InboundActivityOutcome.IGNORED_UNKNOWN_FOLLOW
        current = db.remote_subscribers.get_remote_subscriber(
            local_community_id=community.id, remote_actor_id=ACTOR
        )
        assert current is not None and current.follow_activity_id == NEW
    assert (await _undo(handler, community, NEW)).status == "processed"
    assert db.remote_subscribers.get_remote_subscriber(
        local_community_id=community.id, remote_actor_id=ACTOR
    ) is None
    assert (await _undo(handler, community, NEW)).status == "skipped"
    assert gateway.accept_local_community_follow.await_count == 2


@pytest.mark.asyncio
async def test_b5_failed_old_accept_retry_never_rolls_back_new_follow(tmp_path):
    """A persisted failed Follow receipt cannot revive a superseded generation."""
    db, community, handler, gateway = _scenario(tmp_path)
    gateway.accept_local_community_follow.side_effect = RuntimeError("Accept lost")
    with pytest.raises(RuntimeError, match="Accept lost"):
        await _follow(handler, community, OLD)
    db.event_receipts.create_event_receipt(
        delivery_id=OLD, event_type="local.follow_requested",
        object_ap_id=OLD, status="failed",
    )
    gateway.accept_local_community_follow.side_effect = None
    new = await _follow(handler, community, NEW)
    assert new.status == "processed"
    gateway.accept_local_community_follow.reset_mock()
    skipped = await _follow(handler, community, OLD, is_retry=True,
                            inbox="https://remote.example/changed-inbox")
    assert skipped.status == "skipped"
    assert skipped.outcome is InboundActivityOutcome.IGNORED_ALREADY_APPLIED
    assert skipped.detail == "superseded Follow retry"
    gateway.accept_local_community_follow.assert_not_awaited()
    current = db.remote_subscribers.get_remote_subscriber(
        local_community_id=community.id, remote_actor_id=ACTOR
    )
    assert current.follow_activity_id == NEW
    assert current.remote_inbox_url == INBOX
    # Current-ID retry is still a legitimate same-generation Accept recovery.
    result = await _follow(handler, community, NEW, is_retry=True)
    assert result.status == "processed"
    gateway.accept_local_community_follow.assert_awaited_once()
    assert (await _undo(handler, community, OLD)).status == "skipped"


@pytest.mark.asyncio
@pytest.mark.parametrize("replace_first", [False, True])
async def test_b6_failed_old_retry_after_undo_cannot_recreate_subscriber(tmp_path, replace_first):
    """A retry after either current or subsequent Undo never recreates a row."""
    db, community, handler, gateway = _scenario(tmp_path)
    gateway.accept_local_community_follow.side_effect = RuntimeError("Accept lost")
    with pytest.raises(RuntimeError):
        await _follow(handler, community, OLD)
    gateway.accept_local_community_follow.side_effect = None
    if replace_first:
        await _follow(handler, community, NEW)
    assert (await _undo(handler, community, NEW if replace_first else OLD)).status == "processed"
    gateway.accept_local_community_follow.reset_mock()
    skipped = await _follow(handler, community, OLD, is_retry=True)
    assert skipped.status == "skipped"
    assert skipped.outcome is InboundActivityOutcome.IGNORED_ALREADY_APPLIED
    assert skipped.detail == "unproven Follow retry after unsubscribe"
    gateway.accept_local_community_follow.assert_not_awaited()
    assert db.remote_subscribers.get_remote_subscriber(
        local_community_id=community.id, remote_actor_id=ACTOR
    ) is None
    # A genuinely first-seen *new* Follow can create a new accepted relation.
    fresh = await _follow(handler, community, "https://remote.example/follows/fresh")
    assert fresh.status == "processed"


def test_b4_atomic_undo_cannot_remove_concurrent_follow_refresh(tmp_path):
    """Concurrent Undo(old) and Follow(new) serialize under the SQLite writer lock."""
    db, community, handler, _gateway = _scenario(tmp_path)
    db.remote_subscribers.create_remote_subscriber(
        local_community_id=community.id, remote_actor_id=ACTOR,
        remote_inbox_url=INBOX, follow_activity_id=OLD,
    )

    def refresh():
        """Commit the new Follow generation in its owning transaction."""
        with db.immediate_session() as session:
            return db.remote_subscribers.create_or_refresh_accepted(
                session, local_community_id=community.id, remote_actor_id=ACTOR,
                remote_inbox_url=INBOX, follow_activity_id=NEW,
            ).transition

    def undo_old():
        """Run the exact Follow-ID guarded deletion concurrently."""
        return db.remote_subscribers.delete_accepted_if_current_follow(
            local_community_id=community.id, remote_actor_id=ACTOR,
            follow_activity_id=OLD,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(refresh)
        b = pool.submit(undo_old)
        assert a.result() in {"created", "refreshed"}
        assert b.result() in (True, False)
    # Even when Undo wins first, the later freshly verified Follow persists.
    current = db.remote_subscribers.get_remote_subscriber(
        local_community_id=community.id, remote_actor_id=ACTOR
    )
    assert current is not None and current.follow_activity_id == NEW


def test_failed_follow_receipt_is_marked_retry_before_status_reset(tmp_path):
    """Only persisted failed/deferred Follow receipts can set the retry flag."""
    from src.activitypub_models import LocalFollowRequestEvent
    from src.http_api import _begin_event_attempt

    db, community, _handler, _gateway = _scenario(tmp_path)
    runtime = SimpleNamespace(database=db)
    event = LocalFollowRequestEvent.model_validate({
        "event_type": "local.follow_requested",
        "delivery_id": OLD,
        "occurred_at": "2026-11-12T00:00:00Z",
        "target_actor_kind": "community",
        "target_actor_id": community.actor_url,
        "actor_id": ACTOR,
        "object": {"follow_activity_id": OLD, "remote_inbox_url": INBOX},
    })
    assert _begin_event_attempt(runtime, event) == (None, False)
    db.event_receipts.update_event_receipt(
        delivery_id=OLD, status="failed", outcome="processing_failed",
        detail="Accept network error",
    )
    assert _begin_event_attempt(runtime, event) == (None, True)
    receipt = db.event_receipts.get_event_receipt(OLD)
    assert receipt.status == "in_progress" and receipt.outcome is None


def test_receipt_with_reused_wire_id_cannot_claim_first_seen_follow(tmp_path):
    """A failed unrelated receipt cannot make a reused Follow ID first-seen."""
    from fastapi import HTTPException
    from src.activitypub_models import LocalFollowRequestEvent
    from src.http_api import _begin_event_attempt

    db, community, _handler, _gateway = _scenario(tmp_path)
    db.event_receipts.create_event_receipt(
        delivery_id=OLD, event_type="post.created", object_ap_id="https://remote.example/post/1",
        status="failed",
    )
    event = LocalFollowRequestEvent.model_validate({
        "event_type": "local.follow_requested", "delivery_id": OLD,
        "occurred_at": "2026-11-12T00:00:00Z",
        "target_actor_kind": "community", "target_actor_id": community.actor_url,
        "actor_id": ACTOR,
        "object": {"follow_activity_id": OLD, "remote_inbox_url": INBOX},
    })
    with pytest.raises(HTTPException) as error:
        _begin_event_attempt(SimpleNamespace(database=db), event)
    assert error.value.status_code == 409
    assert db.event_receipts.get_event_receipt(OLD).status == "failed"


def test_b6_retry_old_racing_undo_new_never_recreates_subscription(tmp_path):
    """Retry(old) and Undo(new) serialize; neither can reinstate old."""
    db, community, _handler, _gateway = _scenario(tmp_path)
    db.remote_subscribers.create_remote_subscriber(
        local_community_id=community.id, remote_actor_id=ACTOR,
        remote_inbox_url=INBOX, follow_activity_id=NEW,
    )

    def retry_old():
        """Deliver the receipt-proven old retry under the writer lock."""
        with db.immediate_session() as session:
            return db.remote_subscribers.create_or_refresh_accepted(
                session, local_community_id=community.id, remote_actor_id=ACTOR,
                remote_inbox_url=INBOX, follow_activity_id=OLD,
                is_retry=True,
            ).transition

    def undo_new():
        """Atomically remove the matching accepted generation."""
        return db.remote_subscribers.delete_accepted_if_current_follow(
            local_community_id=community.id, remote_actor_id=ACTOR,
            follow_activity_id=NEW,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        retry = pool.submit(retry_old)
        undo = pool.submit(undo_new)
        assert retry.result() in {"superseded_retry", "unproven_retry_after_unsubscribe"}
        assert undo.result() is True
    assert db.remote_subscribers.get_remote_subscriber(
        local_community_id=community.id, remote_actor_id=ACTOR
    ) is None


@pytest.mark.asyncio
async def test_missing_original_follow_id_never_accepts_new_generation(tmp_path):
    """A forged empty Follow ID produces no durable subscriber or Accept."""
    db, community, handler, gateway = _scenario(tmp_path)
    result = await _follow(handler, community, "")
    assert result.status == "skipped"
    assert result.outcome is InboundActivityOutcome.IGNORED_UNKNOWN_FOLLOW
    assert db.remote_subscribers.list_remote_subscribers(community.id) == []
    gateway.accept_local_community_follow.assert_not_awaited()
