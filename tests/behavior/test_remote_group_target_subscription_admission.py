"""Accepted remote Group bindings must be verified inside each Discord target claim."""

from __future__ import annotations

from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from sqlalchemy import select

from src.db import Database
from src.models import ChannelCommunitySubscription, CommunityDiscordTargetClaim

GROUP_A = "https://nodebb.example/category/9"
GROUP_B = "https://nodebb.example/category/12"
OWNER = "https://person.example/users/alice"


def _database(tmp_path: Path) -> Database:
    """Create one real SQLite v1 test world with a subscribed remote Group."""
    db = Database(f"sqlite:///{tmp_path / 'bridge.sqlite3'}")
    db.create_all()
    db.remote_subscriptions.create_subscription(
        discord_channel_id=100,
        lemmy_community_actor_id=GROUP_A,
        lemmy_community_name="NodeBB group",
        lemmy_community_id=None,
        status="accepted",
    )
    return db


def _thread(db: Database, group: str = GROUP_A) -> int:
    """Create a canonical root whose original verified Group cannot be changed."""
    return db.discord_fanout_groups.get_or_create_remote_thread_group(
        community_actor_id=group,
        ap_object_id=f"{group}/post/1",
        source_owner_actor_id=OWNER,
        ap_activity_id=f"{group}/activities/create/1",
        first_object_json={"ap_id": f"{group}/post/1", "kind": "post"},
    ).id


def _claim_count(db: Database) -> int:
    """Count durable pre-REST claims without treating skipped targets as sends."""
    with db.session() as session:
        return len(session.scalars(select(CommunityDiscordTargetClaim)).all())


@pytest.mark.parametrize("status", ["pending", "failed", "cancelled", "blocked"])
def test_unsubscribed_root_cannot_create_claim_after_stale_candidate_read(
    tmp_path: Path, status: str,
) -> None:
    """A stale candidate from the earlier runtime read cannot reach Discord REST."""
    db = _database(tmp_path)
    try:
        root = _thread(db)
        # The runtime already selected channel 100. Undo or Follow state changes
        # before its separate target reservation writer transaction begins.
        with db.session() as session:
            subscription = session.scalar(select(ChannelCommunitySubscription))
            subscription.status = status
        result = db.discord_fanout_groups.reserve_remote_target(
            kind="post", group_id=root, discord_channel_id=100,
        )
        assert result.state == "not_subscribed"
        assert result.attempt_id is None
        assert _claim_count(db) == 0
    finally:
        db.close()


def test_binding_changed_to_different_group_cannot_claim_old_group(tmp_path: Path) -> None:
    """An accepted binding for B never authorizes a stale event for Group A."""
    db = _database(tmp_path)
    try:
        root_a = _thread(db)
        root_b = _thread(db, GROUP_B)
        with db.session() as session:
            subscription = session.scalar(select(ChannelCommunitySubscription))
            subscription.lemmy_community_actor_id = GROUP_B
        old = db.discord_fanout_groups.reserve_remote_target(
            kind="post", group_id=root_a, discord_channel_id=100,
        )
        new = db.discord_fanout_groups.reserve_remote_target(
            kind="post", group_id=root_b, discord_channel_id=100,
        )
        assert old.state == "not_subscribed"
        assert new.state == "reserved"
        assert _claim_count(db) == 1
    finally:
        db.close()


def test_reply_claim_requires_same_group_and_accepted_binding(tmp_path: Path) -> None:
    """Reply placement cannot bypass exact Group binding via an old root delivery."""
    db = _database(tmp_path)
    try:
        root = _thread(db)
        reply = db.discord_fanout_groups.get_or_create_remote_message_group(
            community_actor_id=GROUP_A,
            ap_object_id=f"{GROUP_A}/post/2",
            source_owner_actor_id=OWNER,
            ap_activity_id=f"{GROUP_A}/activities/create/2",
            thread_group_id=root,
            parent_message_group_id=None,
            first_object_json={"ap_id": f"{GROUP_A}/post/2", "kind": "comment"},
        )
        with db.session() as session:
            subscription = session.scalar(select(ChannelCommunitySubscription))
            subscription.status = "pending"
        rejected = db.discord_fanout_groups.reserve_remote_target(
            kind="comment", group_id=reply.id, discord_channel_id=100,
        )
        assert rejected.state == "not_subscribed"
        assert _claim_count(db) == 0
        with db.session() as session:
            subscription = session.scalar(select(ChannelCommunitySubscription))
            subscription.status = "accepted"
        accepted = db.discord_fanout_groups.reserve_remote_target(
            kind="comment", group_id=reply.id, discord_channel_id=100,
        )
        assert accepted.state == "reserved"
    finally:
        db.close()


def test_failed_attempt_not_rearmed_while_unsubscribed_but_retries_after_accept(
    tmp_path: Path,
) -> None:
    """Subscription removal must not overwrite the durable failed claim state."""
    db = _database(tmp_path)
    try:
        root = _thread(db)
        repo = db.discord_fanout_groups
        first = repo.reserve_remote_target(kind="post", group_id=root, discord_channel_id=100)
        assert first.state == "reserved"
        repo.mark_remote_target(
            kind="post", group_id=root, discord_channel_id=100,
            attempt_id=first.attempt_id, status="failed",
        )
        with db.session() as session:
            subscription = session.scalar(select(ChannelCommunitySubscription))
            subscription.status = "pending"
        denied = repo.reserve_remote_target(kind="post", group_id=root, discord_channel_id=100)
        assert denied.state == "not_subscribed"
        with db.session() as session:
            claim = session.scalar(select(CommunityDiscordTargetClaim))
            assert claim.status == "failed"
            assert claim.attempt_id == first.attempt_id
        with db.session() as session:
            subscription = session.scalar(select(ChannelCommunitySubscription))
            subscription.status = "accepted"
        retried = repo.reserve_remote_target(kind="post", group_id=root, discord_channel_id=100)
        assert retried.state == "reserved"
        assert retried.attempt_id != first.attempt_id
    finally:
        db.close()


def test_unknown_claim_stays_non_retryable_after_subscription_changes(tmp_path: Path) -> None:
    """Undo and new Accept cannot turn a possibly sent REST call into a retry."""
    db = _database(tmp_path)
    try:
        root = _thread(db)
        repo = db.discord_fanout_groups
        first = repo.reserve_remote_target(kind="post", group_id=root, discord_channel_id=100)
        assert first.state == "reserved"
        repo.mark_remote_target(
            kind="post", group_id=root, discord_channel_id=100,
            attempt_id=first.attempt_id, status="unknown",
        )
        with db.session() as session:
            subscription = session.scalar(select(ChannelCommunitySubscription))
            subscription.status = "pending"
        assert repo.reserve_remote_target(
            kind="post", group_id=root, discord_channel_id=100,
        ).state == "not_subscribed"
        with db.session() as session:
            subscription = session.scalar(select(ChannelCommunitySubscription))
            subscription.status = "accepted"
        assert repo.reserve_remote_target(
            kind="post", group_id=root, discord_channel_id=100,
        ).state == "unknown"
        assert _claim_count(db) == 1
    finally:
        db.close()


def test_removed_channel_binding_never_creates_remote_claim(tmp_path: Path) -> None:
    """A deleted binding must reject a previously selected candidate channel."""
    db = _database(tmp_path)
    try:
        root = _thread(db)
        with db.session() as session:
            subscription = session.scalar(select(ChannelCommunitySubscription))
            session.delete(subscription)
        assert db.discord_fanout_groups.reserve_remote_target(
            kind="post", group_id=root, discord_channel_id=100,
        ).state == "not_subscribed"
        assert _claim_count(db) == 0
    finally:
        db.close()


def test_unsubscribe_writer_serializes_with_remote_claim(tmp_path: Path) -> None:
    """A concurrent claim waits for a pending Undo transaction and observes its result."""
    db = _database(tmp_path)
    try:
        root = _thread(db)
        entered = Event()

        def try_reserve():
            """Attempt the normal repository writer transaction from another thread."""
            entered.set()
            return db.discord_fanout_groups.reserve_remote_target(
                kind="post", group_id=root, discord_channel_id=100,
            )

        # The Undo-side writer owns SQLite's reserved writer slot first.
        # Claim admission must serialize behind it rather than reading the
        # previously committed accepted subscription on a separate connection.
        with db.engine.connect() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            connection.exec_driver_sql(
                "UPDATE channel_community_subscriptions SET status='pending' "
                "WHERE discord_channel_id=100"
            )
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(try_reserve)
                assert entered.wait(timeout=3)
                assert not future.done()
                connection.commit()
                result = future.result(timeout=5)
        assert result.state == "not_subscribed"
        assert _claim_count(db) == 0
    finally:
        db.close()
