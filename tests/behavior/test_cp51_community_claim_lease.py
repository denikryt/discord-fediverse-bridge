"""Behavioral regressions for Community Create preflight and worker filtering.

Each scenario uses the real SQLite repository/worker and substitutes only the
outer Discord transport. A slow read-only lookup is not a sent Discord POST.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from src.community_sync.group_view_delivery import CommunityGroupViewDeliveryWorker
from src.db import Database
from src.models import (CommunityDiscordTargetClaim, CommunityThreadGroup,
                        CommunityThreadGroupDelivery)

GROUP = 'https://group.example/category/7'
NOTE = 'https://author.example/notes/9'
OWNER = 'https://author.example/actors/alice'


class _Policy:
    """Allow the known, authenticated Group and author in these scenarios."""

    def federation_decision(self, _actor):
        """Delegate no policy restrictions beyond the test's subscriptions."""
        return SimpleNamespace(allowed=True)


class _History:
    """Observe reconciliation/edit calls without creating Discord messages."""

    def __init__(self, proven=None):
        self.proven = proven
        self.reconciles = []
        self.edits = []

    async def reconcile(self, snapshot):
        """Return an externally controlled history proof (or no proof)."""
        self.reconciles.append(snapshot)
        return self.proven

    async def edit(self, snapshot):
        """Record edits to proven Discord IDs."""
        self.edits.append(snapshot)


def _world(path: Path):
    """Create a verified remote root with one accepted Discord target."""
    db = Database(f'sqlite:///{path}')
    db.create_all()
    db.remote_subscriptions.create_subscription(
        discord_channel_id=10, lemmy_community_actor_id=GROUP,
        lemmy_community_name='Test Group', lemmy_community_id=None, status='accepted')
    repo = db.discord_fanout_groups
    root = repo.get_or_create_remote_thread_group(
        community_actor_id=GROUP, ap_object_id=NOTE,
        source_owner_actor_id=OWNER, ap_activity_id=NOTE+'/create',
        first_object_json={'ap_id': NOTE, 'kind': 'post', 'author_name': 'alice',
                           'title': 'Topic', 'body_markdown': 'Original', 'url': NOTE},
    )
    return db, repo, root.id


@pytest.mark.asyncio
async def test_expired_preflight_can_retry_without_unknown_or_duplicate_post(tmp_path: Path) -> None:
    """A stalled Discord GET never becomes a fictitious lost POST receipt."""
    db, repo, root_id = _world(tmp_path/'preflight.sqlite')
    try:
        initial = repo.reserve_remote_target(kind='post', group_id=root_id, discord_channel_id=10)
        assert initial.state == 'reserved'
        with db.session() as session:
            pending = session.scalar(select(CommunityDiscordTargetClaim))
            assert pending.attempt_started_at is None
            assert pending.next_attempt_at is not None
        # A worker wakes after an old read-only Discord lookup. No POST began.
        with db.immediate_session() as session:
            claim = session.scalar(select(CommunityDiscordTargetClaim))
            claim.next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        transport = _History()
        worker = CommunityGroupViewDeliveryWorker(database=db, bot=object(),
            policy_service=_Policy(), transport=transport)
        assert await worker.drain_once() == 0
        assert transport.reconciles == []
        # The original handler must lose the old lease; replay may safely send
        # exactly once using a new attempt ID and the same canonical Group.
        assert repo.confirm_remote_target_pre_send(kind='post', group_id=root_id,
            discord_channel_id=10, attempt_id=initial.attempt_id) is None
        replacement = repo.reserve_remote_target(kind='post', group_id=root_id,
                                                  discord_channel_id=10)
        assert replacement.state == 'reserved' and replacement.attempt_id != initial.attempt_id
        assert repo.confirm_remote_target_pre_send(kind='post', group_id=root_id,
            discord_channel_id=10, attempt_id=replacement.attempt_id) is not None
        repo.finish_remote_target(kind='post', group_id=root_id, discord_channel_id=10,
            attempt_id=replacement.attempt_id, discord_thread_id=201, discord_object_id=201)
        with db.session() as session:
            claims = list(session.scalars(select(CommunityDiscordTargetClaim)))
            deliveries = list(session.scalars(select(CommunityThreadGroupDelivery)))
        assert len(claims) == len(deliveries) == 1
        assert claims[0].status == 'completed' and claims[0].applied_revision == 0
        assert repo.reserve_remote_target(kind='post', group_id=root_id,
            discord_channel_id=10).state == 'delivered'
    finally:
        db.close()


@pytest.mark.asyncio
async def test_lost_ack_after_post_boundary_stays_unknown_without_repost(tmp_path: Path) -> None:
    """An expired possibly-sent Create can only be reconciled, never resent."""
    db, repo, root_id = _world(tmp_path/'lost-ack.sqlite')
    try:
        attempt = repo.reserve_remote_target(kind='post', group_id=root_id, discord_channel_id=10)
        assert repo.confirm_remote_target_pre_send(kind='post', group_id=root_id,
            discord_channel_id=10, attempt_id=attempt.attempt_id) is not None
        with db.immediate_session() as session:
            claim = session.scalar(select(CommunityDiscordTargetClaim))
            assert claim.attempt_started_at is not None  # POST may have begun.
            claim.attempt_started_at = datetime.now(timezone.utc) - timedelta(minutes=4)
        transport = _History(proven=None)
        worker = CommunityGroupViewDeliveryWorker(database=db, bot=object(),
            policy_service=_Policy(), transport=transport)
        assert await worker.drain_once() == 1
        assert len(transport.reconciles) == 1
        with db.session() as session:
            claim = session.scalar(select(CommunityDiscordTargetClaim))
            assert claim.status == 'unknown' and claim.applied_revision is None
        assert repo.reserve_remote_target(kind='post', group_id=root_id,
            discord_channel_id=10).state == 'unknown'
        assert repo.confirm_remote_target_pre_send(kind='post', group_id=root_id,
            discord_channel_id=10, attempt_id=attempt.attempt_id) is None
    finally:
        db.close()


@pytest.mark.asyncio
async def test_unprovable_legacy_completed_and_unstarted_claimed_are_filtered_before_writer(tmp_path, monkeypatch) -> None:
    """Unactionable historical claims do not consume writer transactions."""
    db, repo, root_id = _world(tmp_path/'legacy.sqlite')
    try:
        with db.immediate_session() as session:
            group = session.get(CommunityThreadGroup, root_id)
            group.view_revision = 1
            session.add(CommunityDiscordTargetClaim(
                thread_group_id=root_id, discord_channel_id=10, kind='post',
                status='completed', attempt_id='legacy', applied_revision=None,
            ))
        # The second item is a historical claimed target without a POST clock
        # or an explicit pending-preflight deadline.
        db.remote_subscriptions.create_subscription(
            discord_channel_id=11, lemmy_community_actor_id=GROUP,
            lemmy_community_name='Test Group', lemmy_community_id=None, status='accepted')
        with db.immediate_session() as session:
            session.add(CommunityDiscordTargetClaim(
                thread_group_id=root_id, discord_channel_id=11, kind='post',
                status='claimed', attempt_id='legacy-unstarted',
                attempt_started_at=None, next_attempt_at=None,
            ))
        original = db.immediate_session
        count = 0

        @contextmanager
        def counted_writer():
            """Count actual write locks, leaving the SQLite writer unchanged."""
            nonlocal count
            count += 1
            with original() as session:
                yield session

        monkeypatch.setattr(db, 'immediate_session', counted_writer)
        worker = CommunityGroupViewDeliveryWorker(database=db, bot=object(),
            policy_service=_Policy(), transport=_History())
        assert await worker.drain_once() == 0
        assert await worker.drain_once() == 0
        assert count == 0
    finally:
        db.close()
