"""Real-SQLite operation tests for last-channel Follow cancellation.

The DiscordOps operation and its repository transactions are exercised with
real persistence; only the signed gateway's HTTP result is simulated.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from discordops import run_operation_definition_async

from src.bridge_policy import BridgePolicyService
from src.db import Database
from src.db.repositories.bridge_actor_follows import BridgeActorFollowRepository
from src.fedify_gateway_client import UnfollowCommunityResult
from src.operations import UnsubscribeInput, unsubscribe_operation
from src.models import BridgeActorFollow


ACTOR = "https://lemmy.example/c/hackers"
INBOX = f"{ACTOR}/inbox"
FOLLOW = "https://bridge.example/actors/services/bridge/activities/follow/1"


def _setup(tmp_path: Path) -> tuple[Database, AsyncMock, BridgePolicyService]:
    """Create a v1 database and a fake remote gateway for one independent case."""
    db = Database(f"sqlite:///{tmp_path / 'unsubscribe.sqlite'}")
    db.migrate()
    gateway = AsyncMock()
    gateway.unfollow_community.return_value = UnfollowCommunityResult(accepted=True)
    policy = BridgePolicyService(settings=SimpleNamespace(), repository=db.bridge_policy_entries)
    return db, gateway, policy


def _reserve(db: Database, *, channel: int = 123, follow: str | None = FOLLOW,
             dispatch: str = "sent") -> None:
    """Persist the exact Group/Follow/channel identity before operation execution."""
    if follow is None:
        db.bridge_actor_follows.create_bridge_actor_follow(
            community_actor_id=ACTOR, community_inbox_url=INBOX,
            follow_activity_id=None, status="accepted",
        )
        db.remote_subscriptions.create_subscription(
            discord_channel_id=channel, lemmy_community_actor_id=ACTOR,
            lemmy_community_name="hackers", lemmy_community_id=42, follow_activity_id=None,
            status="accepted",
        )
        return
    with db.immediate_session() as session:
        row, _ = BridgeActorFollowRepository.reserve_with_binding(
            session, community_actor_id=ACTOR, community_inbox_url=INBOX,
            follow_activity_id=follow, remote_software_profile="threadiverse",
            discord_channel_id=channel, discord_guild_id=1,
            community_name="hackers", numeric_id=42,
            community_handle="!hackers@lemmy.example",
            initiated_by_discord_user_id="1",
        )
        # This fixture represents a completed network send. The operation
        # must not create Undo while a Follow remains merely prepared.
        row.follow_dispatch_state = dispatch
        if dispatch == "sent":
            row.status = "accepted"


async def _unsubscribe(db: Database, gateway: AsyncMock,
                       policy: BridgePolicyService, channel: int = 123):
    """Run the real operation with a verified channel and SQLite-backed state."""
    return await run_operation_definition_async(
        unsubscribe_operation,
        UnsubscribeInput(
            database=db, fedify_gateway=gateway, channel_id=channel,
            channel_mention=f"<#{channel}>", policy_service=policy,
        ),
    )


@pytest.mark.asyncio
async def test_unsubscribe_operation_rejects_missing_subscription(tmp_path: Path) -> None:
    """An unknown channel makes no repository changes and cannot send Undo."""
    db, gateway, policy = _setup(tmp_path)
    try:
        result = await _unsubscribe(db, gateway, policy)
        assert result.applied is False
        assert result.reason == "channel_subscription_not_found"
        assert result.message == "Channel <#123> has no active subscription."
        gateway.unfollow_community.assert_not_awaited()
    finally:
        db.close()


@pytest.mark.asyncio
async def test_unsubscribe_skips_unfollow_when_other_channels_remain(tmp_path: Path) -> None:
    """Removing one of two channel bindings leaves the original Follow intact."""
    db, gateway, policy = _setup(tmp_path)
    try:
        _reserve(db)
        _reserve(db, channel=456)
        result = await _unsubscribe(db, gateway, policy)
        assert result.applied is True
        assert db.remote_subscriptions.get_subscription_by_channel(123) is None
        assert db.remote_subscriptions.get_subscription_by_channel(456) is not None
        assert db.bridge_actor_follows.get_bridge_actor_follow(ACTOR).follow_activity_id == FOLLOW
        gateway.unfollow_community.assert_not_awaited()
    finally:
        db.close()


@pytest.mark.asyncio
async def test_unsubscribe_sends_unfollow_when_last_channel(tmp_path: Path) -> None:
    """A proven sent Follow is undone once using its original inbox and ID."""
    db, gateway, policy = _setup(tmp_path)
    try:
        _reserve(db)

        async def send(_actor: str, _follow: str, **kw: str) -> UnfollowCommunityResult:
            """Assert the durable Undo claim and removed channel before network."""
            row = db.bridge_actor_follows.get_bridge_actor_follow(ACTOR)
            assert row.follow_dispatch_state == "undo_sending" and row.cancel_requested
            assert db.remote_subscriptions.get_subscription_by_channel(123) is None
            assert kw["original_inbox_url"] == INBOX
            return UnfollowCommunityResult(accepted=True)

        gateway.unfollow_community.side_effect = send
        result = await _unsubscribe(db, gateway, policy)
        assert result.applied is True
        assert result.message == "Unsubscribed <#123> from **hackers**."
        gateway.unfollow_community.assert_awaited_once_with(
            ACTOR, FOLLOW, original_inbox_url=INBOX,
        )
        assert db.bridge_actor_follows.get_bridge_actor_follow(ACTOR) is None
    finally:
        db.close()


@pytest.mark.asyncio
async def test_unsubscribe_last_channel_undo_failure_keeps_follow_row(tmp_path: Path) -> None:
    """Unknown Undo HTTP result retains its immutable generation across retry."""
    db, gateway, policy = _setup(tmp_path)
    try:
        _reserve(db)
        gateway.unfollow_community.return_value = UnfollowCommunityResult(
            accepted=False, error="network result unknown"
        )
        result = await _unsubscribe(db, gateway, policy)
        assert result.applied is False
        assert result.reason == "remote_follow_cleanup_unresolved"
        assert FOLLOW in result.message and "Undo=" in result.message
        row = db.bridge_actor_follows.get_bridge_actor_follow(ACTOR)
        assert row.follow_activity_id == FOLLOW and row.follow_dispatch_state == "undo_sending"
        assert row.cancel_requested
        assert db.remote_subscriptions.get_subscription_by_channel(123) is None
        # Once a possibly delivered Undo is recorded, restarting the operation
        # may not send a second network request without independent evidence.
        assert (await _unsubscribe(db, gateway, policy)).reason == "channel_subscription_not_found"
        gateway.unfollow_community.assert_awaited_once()
    finally:
        db.close()


@pytest.mark.asyncio
async def test_unsubscribe_last_channel_with_missing_follow_id_keeps_local_state(tmp_path: Path) -> None:
    """Legacy NULL Follow identity cannot authorize deleting an accepted row."""
    db, gateway, policy = _setup(tmp_path)
    try:
        _reserve(db, follow=None)
        result = await _unsubscribe(db, gateway, policy)
        assert result.applied is False
        assert result.reason == "follow_activity_id_missing"
        assert db.remote_subscriptions.get_subscription_by_channel(123) is not None
        assert db.bridge_actor_follows.get_bridge_actor_follow(ACTOR).follow_activity_id is None
        gateway.unfollow_community.assert_not_awaited()
    finally:
        db.close()


@pytest.mark.asyncio
async def test_unsubscribe_unclaimed_follow_does_not_contact_remote(tmp_path: Path) -> None:
    """A prepared Follow is deleted before sender claim without issuing Undo."""
    db, gateway, policy = _setup(tmp_path)
    try:
        _reserve(db, dispatch="prepared")
        result = await _unsubscribe(db, gateway, policy)
        assert result.applied
        assert db.bridge_actor_follows.get_bridge_actor_follow(ACTOR) is None
        gateway.unfollow_community.assert_not_awaited()
    finally:
        db.close()


@pytest.mark.asyncio
async def test_unsubscribe_sending_follow_reports_pending_without_undo(tmp_path: Path) -> None:
    """Uncertain original Follow send keeps cancellation pending, not published."""
    db, gateway, policy = _setup(tmp_path)
    try:
        _reserve(db, dispatch="sending")
        result = await _unsubscribe(db, gateway, policy)
        assert not result.applied
        assert result.reason == "remote_follow_cleanup_pending"
        row = db.bridge_actor_follows.get_bridge_actor_follow(ACTOR)
        assert row.cancel_requested and row.follow_dispatch_state == "sending"
        gateway.unfollow_community.assert_not_awaited()
    finally:
        db.close()


async def _subscribe(db: Database, gateway: AsyncMock,
                     policy: BridgePolicyService, channel: int = 123):
    """Enter the actual subscribed Group through a registered moderator action."""
    from src.operations import SubscribeInput, subscribe_operation
    db.users.get_user_by_discord_user_id = Mock(
        return_value=SimpleNamespace(id=7)
    )
    return await run_operation_definition_async(
        subscribe_operation,
        SubscribeInput(
            database=db, fedify_gateway=gateway, discord_user_id="1",
            channel_id=channel, channel_mention=f"<#{channel}>",
            actor_id=ACTOR, community_name="hackers", numeric_id=42,
            community_handle="!hackers@lemmy.example", policy_service=policy,
            remote_software_profile="threadiverse",
        ),
    )


@pytest.mark.asyncio
async def test_resubscribe_after_undo_unknown_reports_actionable_unresolved(tmp_path: Path) -> None:
    """Restarting subscribe after a possible Undo cannot create a second Follow."""
    from src.activitypub_identity_urls import derive_service_undo_follow_activity_id
    db, gateway, policy = _setup(tmp_path)
    try:
        _reserve(db)
        gateway.unfollow_community.return_value = UnfollowCommunityResult(accepted=False, error="timeout")
        assert (await _unsubscribe(db, gateway, policy)).reason == "remote_follow_cleanup_unresolved"
        gateway.prepare_community_follow.return_value = SimpleNamespace(
            community_actor_url=ACTOR, community_inbox_url=INBOX,
            follow_activity_id=FOLLOW + "-new",
        )
        # Open a fresh Database instance to prove persistence, not a cached
        # in-process state machine, governs the next moderator command.
        db.close()
        db = Database(f"sqlite:///{tmp_path / 'unsubscribe.sqlite'}")
        db.migrate()
        policy = BridgePolicyService(settings=SimpleNamespace(), repository=db.bridge_policy_entries)
        result = await _subscribe(db, gateway, policy, channel=456)
        assert not result.applied
        assert result.reason == "remote_follow_cleanup_unresolved"
        assert FOLLOW in result.message
        assert derive_service_undo_follow_activity_id(FOLLOW) in result.message
        assert db.remote_subscriptions.get_subscription_by_channel(456) is None
        assert db.bridge_actor_follows.get_bridge_actor_follow(ACTOR).follow_activity_id == FOLLOW
        gateway.prepare_community_follow.assert_not_awaited()
        gateway.send_prepared_community_follow.assert_not_awaited()
        gateway.unfollow_community.assert_awaited_once()
    finally:
        db.close()


@pytest.mark.asyncio
async def test_sending_cancel_then_resubscribe_reuses_follow_without_second_send(tmp_path: Path) -> None:
    """On-demand recovery before Undo restores binding to the same generation."""
    db, gateway, policy = _setup(tmp_path)
    try:
        _reserve(db, dispatch="sending")
        assert (await _unsubscribe(db, gateway, policy)).reason == "remote_follow_cleanup_pending"
        gateway.prepare_community_follow.return_value = SimpleNamespace(
            community_actor_url=ACTOR, community_inbox_url=INBOX,
            follow_activity_id=FOLLOW + "-new",
        )
        recovered = await _subscribe(db, gateway, policy, channel=456)
        assert recovered.applied
        assert recovered.reason == "pending_remote_confirmation"
        row = db.bridge_actor_follows.get_bridge_actor_follow(ACTOR)
        assert row.follow_activity_id == FOLLOW
        assert not row.cancel_requested
        assert row.follow_dispatch_state == "sending"
        assert db.remote_subscriptions.get_subscription_by_channel(456).follow_activity_id == FOLLOW
        gateway.prepare_community_follow.assert_not_awaited()
        gateway.send_prepared_community_follow.assert_not_awaited()
        gateway.unfollow_community.assert_not_awaited()
    finally:
        db.close()


@pytest.mark.asyncio
async def test_unsubscribe_during_follow_send_undoes_only_after_send_completes(tmp_path: Path) -> None:
    """Last unsubscribe can cancel in-flight Follow without sending early Undo."""
    import asyncio
    db, gateway, policy = _setup(tmp_path)
    started = asyncio.Event()
    release = asyncio.Event()
    gateway.prepare_community_follow.return_value = SimpleNamespace(
        community_actor_url=ACTOR, community_inbox_url=INBOX,
        follow_activity_id=FOLLOW,
    )

    async def delayed_send(_prepared):
        """Suspend the remote sender after SQLite has claimed its generation."""
        started.set()
        await release.wait()

    gateway.send_prepared_community_follow.side_effect = delayed_send
    try:
        publishing = asyncio.create_task(_subscribe(db, gateway, policy))
        await asyncio.wait_for(started.wait(), timeout=3)
        pending = await _unsubscribe(db, gateway, policy)
        assert pending.reason == "remote_follow_cleanup_pending"
        gateway.unfollow_community.assert_not_awaited()
        release.set()
        cancelled = await publishing
        assert not cancelled.applied
        assert cancelled.reason == "follow_cancelled_during_dispatch"
        gateway.unfollow_community.assert_awaited_once_with(
            ACTOR, FOLLOW, original_inbox_url=INBOX,
        )
        assert db.bridge_actor_follows.get_bridge_actor_follow(ACTOR) is None
    finally:
        release.set()
        db.close()


@pytest.mark.asyncio
async def test_send_timeout_after_last_unsubscribe_never_reports_active_channel(tmp_path: Path) -> None:
    """A timed-out Follow cannot turn an already removed binding into success."""
    import asyncio
    db, gateway, policy = _setup(tmp_path)
    started = asyncio.Event()
    release = asyncio.Event()
    gateway.prepare_community_follow.return_value = SimpleNamespace(
        community_actor_url=ACTOR, community_inbox_url=INBOX,
        follow_activity_id=FOLLOW,
    )

    async def unknown_send(_prepared):
        """Model a lost response after possible remote delivery."""
        started.set()
        await release.wait()
        raise TimeoutError("delivery outcome unknown")

    gateway.send_prepared_community_follow.side_effect = unknown_send
    try:
        publishing = asyncio.create_task(_subscribe(db, gateway, policy))
        await asyncio.wait_for(started.wait(), timeout=3)
        assert (await _unsubscribe(db, gateway, policy)).reason == "remote_follow_cleanup_pending"
        release.set()
        result = await publishing
        assert not result.applied
        assert result.reason == "follow_cancelled_during_dispatch"
        row = db.bridge_actor_follows.get_bridge_actor_follow(ACTOR)
        assert row.follow_dispatch_state == "sending" and row.cancel_requested
        assert row.follow_activity_id == FOLLOW
        gateway.unfollow_community.assert_not_awaited()
    finally:
        release.set()
        db.close()


@pytest.mark.asyncio
async def test_resubscribe_while_original_follow_in_flight_prevents_late_undo(tmp_path: Path) -> None:
    """An atomic rebind wins over a late sender and keeps original Follow ID."""
    import asyncio
    db, gateway, policy = _setup(tmp_path)
    started = asyncio.Event()
    release = asyncio.Event()
    gateway.prepare_community_follow.return_value = SimpleNamespace(
        community_actor_url=ACTOR, community_inbox_url=INBOX,
        follow_activity_id=FOLLOW,
    )

    async def delayed_send(_prepared):
        """Hold the one official Follow send past cancellation and rebind."""
        started.set()
        await release.wait()

    gateway.send_prepared_community_follow.side_effect = delayed_send
    try:
        initial = asyncio.create_task(_subscribe(db, gateway, policy))
        await asyncio.wait_for(started.wait(), timeout=3)
        assert (await _unsubscribe(db, gateway, policy)).reason == "remote_follow_cleanup_pending"
        rebinding = await _subscribe(db, gateway, policy, channel=456)
        assert rebinding.applied and rebinding.reason == "pending_remote_confirmation"
        assert db.remote_subscriptions.get_subscription_by_channel(456).follow_activity_id == FOLLOW
        release.set()
        original = await initial
        assert not original.applied
        assert original.reason == "follow_cancelled_during_dispatch"
        row = db.bridge_actor_follows.get_bridge_actor_follow(ACTOR)
        assert row.follow_dispatch_state == "sent"
        assert not row.cancel_requested
        gateway.unfollow_community.assert_not_awaited()
        gateway.send_prepared_community_follow.assert_awaited_once()
    finally:
        release.set()
        db.close()
