"""Durable outbound Group Follow admission and signed Accept regression scenarios.

The gateway transport is an outer boundary: these tests assert the actual
SQLite state that must exist before an outbound HTTP request can be attempted.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from src.db import Database
from src.db.repositories.bridge_actor_follows import (
    BridgeActorFollowRepository,
    FollowReservationConflict,
)
from src.models import BridgeActorFollow, ChannelCommunitySubscription

_GROUP = "https://nodebb.example/category/42"
_INBOX = "https://nodebb.example/category/42/inbox"
_FOLLOW = "https://bridge.example/activities/follows/first"


def _reserve(db: Database, channel: int, *, profile: str = "nodebb") -> tuple[int, bool]:
    """Simulate a verified moderator request at the writer-owned repository boundary."""
    with db.immediate_session() as session:
        row, must_send = BridgeActorFollowRepository.reserve_with_binding(
            session,
            community_actor_id=_GROUP,
            community_inbox_url=_INBOX,
            follow_activity_id=f"{_FOLLOW}-{channel}",
            remote_software_profile=profile,
            discord_channel_id=channel,
            discord_guild_id=5,
            community_name="General",
            numeric_id=None,
            community_handle="!general@nodebb.example",
            initiated_by_discord_user_id="1",
        )
        return row.id, must_send


def _db(tmp_path: Path) -> Database:
    """Start a full v1 SQLite database, not a simplified mock schema."""
    db = Database(f"sqlite:///{tmp_path / 'follow.db'}")
    db.migrate()
    return db


def test_follow_is_durable_before_claim_and_second_channel_reuses_generation(tmp_path: Path) -> None:
    """Two channels need one immutable Follow, even if the first send has not started."""
    db = _db(tmp_path)
    try:
        first, needs_send = _reserve(db, 10)
        assert needs_send
        with db.session() as session:
            follow = session.get(BridgeActorFollow, first)
            assert follow.follow_activity_id == f"{_FOLLOW}-10"
            assert follow.follow_dispatch_state == "prepared"
            assert follow.status == "pending"
            assert session.scalar(select(ChannelCommunitySubscription).where(
                ChannelCommunitySubscription.discord_channel_id == 10
            )).follow_activity_id == follow.follow_activity_id
        second, may_claim = _reserve(db, 11)
        assert second == first and may_claim
        assert db.bridge_actor_follows.claim_prepared_follow(first)
        assert not db.bridge_actor_follows.claim_prepared_follow(second)
        with db.session() as session:
            assert len(session.scalars(select(BridgeActorFollow)).all()) == 1
            assert len(session.scalars(select(ChannelCommunitySubscription)).all()) == 2
    finally:
        db.close()


def test_fast_signed_accept_wins_over_late_send_completion(tmp_path: Path) -> None:
    """A transport callback never downgrades an already accepted Group generation."""
    db = _db(tmp_path)
    try:
        follow_id, _ = _reserve(db, 10)
        assert db.bridge_actor_follows.claim_prepared_follow(follow_id)
        assert db.bridge_actor_follows.accept_verified_follow(
            follow_activity_id=f"{_FOLLOW}-10", actor_id=_GROUP
        )
        assert db.bridge_actor_follows.complete_prepared_follow(follow_id)
        with db.session() as session:
            follow = session.get(BridgeActorFollow, follow_id)
            sub = session.scalar(select(ChannelCommunitySubscription))
            assert (follow.status, follow.follow_dispatch_state, sub.status) == (
                "accepted", "sent", "accepted"
            )
        assert not db.bridge_actor_follows.accept_verified_follow(
            follow_activity_id=f"{_FOLLOW}-10", actor_id=_GROUP
        )
    finally:
        db.close()


def test_forged_accept_and_conflicting_profile_cannot_change_follow(tmp_path: Path) -> None:
    """Neither a foreign Group signature nor a different software profile wins."""
    db = _db(tmp_path)
    try:
        follow_id, _ = _reserve(db, 10)
        assert not db.bridge_actor_follows.accept_verified_follow(
            follow_activity_id=f"{_FOLLOW}-10", actor_id="https://other.example/category/42"
        )
        with pytest.raises(FollowReservationConflict, match="profile conflicts"):
            _reserve(db, 11, profile="threadiverse")
        with db.session() as session:
            follow = session.get(BridgeActorFollow, follow_id)
            assert follow.status == "pending"
            assert follow.remote_software_profile == "nodebb"
            assert len(session.scalars(select(ChannelCommunitySubscription)).all()) == 1
    finally:
        db.close()


def test_cancelled_follow_does_not_accept_or_claim(tmp_path: Path) -> None:
    """Last-channel cancellation must block an unsent Follow and late Accept."""
    db = _db(tmp_path)
    try:
        follow_id, _ = _reserve(db, 10)
        with db.immediate_session() as session:
            session.get(BridgeActorFollow, follow_id).cancel_requested = True
        assert not db.bridge_actor_follows.claim_prepared_follow(follow_id)
        assert not db.bridge_actor_follows.accept_verified_follow(
            follow_activity_id=f"{_FOLLOW}-10", actor_id=_GROUP
        )
        # Before Undo was claimed, a later subscribe is allowed to rebind
        # the same generation, never generating a replacement Follow ID.
        same_id, can_send = _reserve(db, 11)
        assert same_id == follow_id and can_send
        assert not db.bridge_actor_follows.get_bridge_actor_follow(_GROUP).cancel_requested
        assert db.bridge_actor_follows.claim_prepared_follow(follow_id)
    finally:
        db.close()


def test_unknown_transport_outcome_keeps_first_id_across_restart(tmp_path: Path) -> None:
    """A claimed but unresolved send is not a license to generate a second Follow."""
    db = _db(tmp_path)
    try:
        follow_id, _ = _reserve(db, 10)
        assert db.bridge_actor_follows.claim_prepared_follow(follow_id)
    finally:
        db.close()
    db = _db(tmp_path)
    try:
        same_id, can_send = _reserve(db, 11)
        assert same_id == follow_id and not can_send
        with db.session() as session:
            row = session.get(BridgeActorFollow, follow_id)
            assert row.follow_activity_id == f"{_FOLLOW}-10"
            assert row.follow_dispatch_state == "sending"
    finally:
        db.close()


def test_proven_threadiverse_implicit_accept_but_nodebb_never_implicit(tmp_path: Path) -> None:
    """Only stored threadiverse evidence permits implicit promotion on inbound content."""
    db = _db(tmp_path)
    try:
        node_id, _ = _reserve(db, 10, profile="nodebb")
        assert db.bridge_actor_follows.accept_implicit_threadiverse_follow(_GROUP) == []
        with db.session() as session:
            assert session.get(BridgeActorFollow, node_id).status == "pending"

        with db.immediate_session() as session:
            # A second, independently verified Lemmy Group is a separate Follow.
            from src.db.repositories.bridge_actor_follows import BridgeActorFollowRepository
            remote, _ = BridgeActorFollowRepository.reserve_with_binding(
                session,
                community_actor_id="https://lemmy.example/c/science",
                community_inbox_url="https://lemmy.example/inbox",
                follow_activity_id="https://bridge.example/activities/follow/lemmy",
                remote_software_profile="threadiverse",
                discord_channel_id=11,
                discord_guild_id=5,
                community_name="science",
                numeric_id=22,
                community_handle="!science@lemmy.example",
                initiated_by_discord_user_id="1",
            )
        changed = db.bridge_actor_follows.accept_implicit_threadiverse_follow(
            "https://lemmy.example/c/science"
        )
        assert [row.discord_channel_id for row in changed] == [11]
        assert db.remote_subscriptions.get_subscription_by_channel(11).status == "accepted"
        assert db.remote_subscriptions.get_subscription_by_channel(10).status == "pending"
    finally:
        db.close()


def test_concurrent_discovery_cannot_redirect_saved_follow_inbox(tmp_path: Path) -> None:
    """A newer actor document cannot substitute a different recipient for pending Follow."""
    db = _db(tmp_path)
    try:
        original_id, _ = _reserve(db, 10)
        with pytest.raises(FollowReservationConflict, match="inbox differs"):
            with db.immediate_session() as session:
                BridgeActorFollowRepository.reserve_with_binding(
                    session,
                    community_actor_id=_GROUP,
                    community_inbox_url="https://attacker.example/inbox",
                    follow_activity_id=f"{_FOLLOW}-new",
                    remote_software_profile="nodebb",
                    discord_channel_id=11,
                    discord_guild_id=5,
                    community_name="General",
                    numeric_id=None,
                    community_handle="!general@nodebb.example",
                    initiated_by_discord_user_id="1",
                )
        with db.session() as session:
            assert session.get(BridgeActorFollow, original_id).community_inbox_url == _INBOX
            assert len(session.scalars(select(ChannelCommunitySubscription)).all()) == 1
    finally:
        db.close()


def test_concurrent_channel_reservations_have_one_follow_sender(tmp_path: Path) -> None:
    """Parallel moderator requests serialize at SQLite and claim the same generation."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    db = _db(tmp_path)
    start = Barrier(2)

    def subscribe(channel: int) -> tuple[int, bool]:
        """Run one independent moderator reservation on a worker connection."""
        start.wait(timeout=5)
        return _reserve(db, channel)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            a, b = list(pool.map(subscribe, (10, 11)))
        assert a[0] == b[0]
        assert a[1] and b[1]
        assert [db.bridge_actor_follows.claim_prepared_follow(a[0]),
                db.bridge_actor_follows.claim_prepared_follow(b[0])].count(True) == 1
        with db.session() as session:
            assert len(session.scalars(select(BridgeActorFollow)).all()) == 1
            bindings = session.scalars(select(ChannelCommunitySubscription)).all()
            assert {row.discord_channel_id for row in bindings} == {10, 11}
            assert len({row.follow_activity_id for row in bindings}) == 1
    finally:
        db.close()


def test_nodebb_requires_explicit_accept_but_verified_threadiverse_can_implicit(tmp_path: Path) -> None:
    """Inbound delivery alone cannot activate a pending NodeBB Group subscription."""
    db = _db(tmp_path)
    try:
        follow_id, _ = _reserve(db, 10)
        assert db.bridge_actor_follows.accept_implicit_threadiverse_follow(_GROUP) == []
        assert db.remote_subscriptions.get_subscription_by_channel(10).status == "pending"
        # Confirmed Lemmy software is a distinct, intentionally permitted path.
        with db.immediate_session() as session:
            session.get(BridgeActorFollow, follow_id).remote_software_profile = "threadiverse"
        promoted = db.bridge_actor_follows.accept_implicit_threadiverse_follow(_GROUP)
        assert [row.discord_channel_id for row in promoted] == [10]
        assert db.bridge_actor_follows.get_bridge_actor_follow(_GROUP).status == "accepted"
        assert db.remote_subscriptions.get_subscription_by_channel(10).status == "accepted"
        assert db.bridge_actor_follows.accept_implicit_threadiverse_follow(_GROUP) == []
    finally:
        db.close()
