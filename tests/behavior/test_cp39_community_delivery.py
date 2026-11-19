"""Durable Community Group-view transport boundaries with real SQLite claims.

Fake only Discord history/REST, which is the network boundary. The Group view,
unknown Create claim, adopted delivery and revision CAS all remain real ORM.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from src.db import Database
from src.models import (CommunityDiscordTargetClaim, CommunityThreadGroup,
                        CommunityThreadGroupDelivery)
from src.community_sync.group_view_delivery import (
    CommunityGroupViewDeliveryWorker, CommunityDiscordViewTransport,
    GroupClaimSnapshot, _render,
)
from src.formatting import feed_comment_content

GROUP = 'https://nodebb.example/category/2'
OWNER = 'https://nodebb.example/uid/2'
NOTE = 'https://nodebb.example/post/42'


class _Policy:
    """Accept the two configured actors for this isolated SQLite harness."""

    def federation_decision(self, _actor):
        return SimpleNamespace(allowed=True)


class _Transport:
    """Record the actual worker operation instead of dispatching Discord REST."""

    def __init__(self, *, proven=None, fail=False):
        self.proven = proven
        self.fail = fail
        self.reconciled = []
        self.edited = []
        self.on_edit = None

    async def reconcile(self, claim):
        self.reconciled.append(claim)
        if self.fail:
            raise PermissionError('history forbidden')
        return self.proven

    async def edit(self, claim):
        self.edited.append((claim, _render(claim)))
        if self.on_edit:
            self.on_edit()
            self.on_edit = None
        if self.fail:
            raise RuntimeError('Discord edit failed')


def _setup(tmp_path: Path):
    """Produce one accepted channel and a bot-owned remote canonical Group row."""
    db = Database(f'sqlite:///{tmp_path / "community.sqlite"}')
    db.create_all()
    db.remote_subscriptions.create_subscription(discord_channel_id=10,
        lemmy_community_actor_id=GROUP, lemmy_community_name='Group',
        lemmy_community_id=None, status='accepted')
    repo = db.discord_fanout_groups
    group = repo.get_or_create_remote_thread_group(
        community_actor_id=GROUP, ap_object_id=NOTE,
        source_owner_actor_id=OWNER, ap_activity_id=NOTE+'/create',
        first_object_json={'ap_id': NOTE, 'kind': 'post', 'author_name': 'bob',
                           'title': 'Topic', 'body_markdown': 'First',
                           'published_at': '2026-11-18T12:00:00Z', 'url': NOTE},
    )
    return db, repo, group.id


@pytest.mark.asyncio
async def test_unknown_create_is_adopted_without_repost_then_latest_delete_applies(tmp_path: Path) -> None:
    """Group Delete after a lost ACK changes the recovered, original Discord ID."""
    db, repo, group_id = _setup(tmp_path)
    try:
        r = repo.reserve_remote_target(kind='post', group_id=group_id, discord_channel_id=10)
        assert r.state == 'reserved'
        repo.mark_remote_target(kind='post', group_id=group_id, discord_channel_id=10,
                                attempt_id=r.attempt_id, status='unknown')
        with db.immediate_session() as session:
            group = session.get(CommunityThreadGroup, group_id)
            group.view_state = 'deleted'
            group.view_revision = 1
        transport = _Transport(proven=(201, 201))
        worker = CommunityGroupViewDeliveryWorker(database=db, bot=object(),
            policy_service=_Policy(), transport=transport)
        assert await worker.drain_once() == 1
        with db.session() as session:
            [claim] = session.scalars(select(CommunityDiscordTargetClaim)).all()
            assert claim.status == 'completed' and claim.applied_revision == 0
        assert await worker.drain_once() == 1
        assert len(transport.reconciled) == 1 and len(transport.edited) == 1
        edited_snapshot, edited_body = transport.edited[0]
        assert edited_snapshot.thread_id == 201
        assert edited_snapshot.message_id == 201
        assert '*Deleted from Community*' in edited_body
        assert edited_body.splitlines()[-1] == NOTE
        with db.session() as session:
            [claim] = session.scalars(select(CommunityDiscordTargetClaim)).all()
            assert claim.applied_revision == 1 and claim.status == 'completed'
    finally:
        db.close()


@pytest.mark.asyncio
async def test_update_finished_after_newer_delete_keeps_delete_due(tmp_path: Path) -> None:
    """Old successful Edit must not advance the cursor beyond what it sent."""
    db, repo, group_id = _setup(tmp_path)
    try:
        r = repo.reserve_remote_target(kind='post', group_id=group_id, discord_channel_id=10)
        repo.finish_remote_target(kind='post', group_id=group_id, discord_channel_id=10,
            attempt_id=r.attempt_id, discord_thread_id=201, discord_object_id=201)
        with db.immediate_session() as session:
            g = session.get(CommunityThreadGroup, group_id)
            g.view_revision = 1
            g.display_object_json = {**g.display_object_json, 'body_markdown': 'Updated'}
        transport = _Transport()
        worker = CommunityGroupViewDeliveryWorker(database=db, bot=object(),
            policy_service=_Policy(), transport=transport)

        def during_network():
            with db.immediate_session() as session:
                g = session.get(CommunityThreadGroup, group_id)
                g.view_revision = 2
                g.view_state = 'deleted'

        transport.on_edit = during_network
        assert await worker.drain_once() == 1
        with db.session() as session:
            [claim] = session.scalars(select(CommunityDiscordTargetClaim)).all()
            assert claim.applied_revision == 1 and claim.next_attempt_at is not None
        assert await worker.drain_once() == 1
        assert transport.edited[0][0].view_state == 'live'
        assert transport.edited[1][0].view_state == 'deleted'
        with db.session() as session:
            [claim] = session.scalars(select(CommunityDiscordTargetClaim)).all()
            assert claim.applied_revision == 2
    finally:
        db.close()


@pytest.mark.asyncio
async def test_unproven_unknown_and_markerless_legacy_never_repost(tmp_path: Path) -> None:
    """Permission refusal and old layout remain durable unresolved claims."""
    db, repo, group_id = _setup(tmp_path)
    try:
        r = repo.reserve_remote_target(kind='post', group_id=group_id, discord_channel_id=10)
        repo.mark_remote_target(kind='post', group_id=group_id, discord_channel_id=10,
                                attempt_id=r.attempt_id, status='unknown')
        transport = _Transport(fail=True)
        worker = CommunityGroupViewDeliveryWorker(database=db, bot=object(),
            policy_service=_Policy(), transport=transport)
        assert await worker.drain_once() == 1
        assert await worker.drain_once() == 0
        with db.session() as session:
            [claim] = session.scalars(select(CommunityDiscordTargetClaim)).all()
            assert claim.status == 'unknown' and claim.last_error == 'history forbidden'
            assert claim.next_attempt_at is not None
        with db.immediate_session() as session:
            claim = session.scalars(select(CommunityDiscordTargetClaim)).one()
            claim.reconciliation_marker_version = None
            claim.next_attempt_at = None
        assert await worker.drain_once() == 0
        with db.session() as session:
            [claim] = session.scalars(select(CommunityDiscordTargetClaim)).all()
            assert claim.status == 'unknown' and 'manual' in claim.last_error
    finally:
        db.close()


class _FakeThread:
    """One forum child with an actual first bot message and Discord history API."""

    def __init__(self, thread_id: int, parent_id: int, bot_id: int, content: str,
                 *, messages=None, allow=True):
        self.id = thread_id
        self.parent_id = parent_id
        self.guild = SimpleNamespace(me=object(), id=12)
        self._starter = SimpleNamespace(id=thread_id, author=SimpleNamespace(id=bot_id),
                                        content=content, channel=SimpleNamespace(id=thread_id))
        self._messages = messages if messages is not None else [self._starter]
        self._allow = allow

    def permissions_for(self, _user):
        return SimpleNamespace(view_channel=self._allow, read_message_history=self._allow)

    async def fetch_message(self, _id):
        return self._starter

    async def history(self, *, limit):
        for msg in self._messages[:limit]:
            yield msg


class _FakeForum:
    """Expose active and archived entries separately, as discord.py does."""

    def __init__(self, threads, *, forum_id=10):
        self.id = forum_id
        self.threads = threads
        self.guild = SimpleNamespace(me=object())

    def permissions_for(self, _user):
        return SimpleNamespace(view_channel=True, read_message_history=True)

    async def archived_threads(self, *, limit):
        for thread in []:
            yield thread


class _FakeBot:
    """Return a known Discord server topology without making external calls."""

    def __init__(self, threads, *, bot_id=9):
        self.user = SimpleNamespace(id=bot_id)
        self.threads = {t.id: t for t in threads}
        self.forum = _FakeForum(threads)

    async def fetch_forum_channel(self, channel_id):
        assert channel_id == 10
        return self.forum

    async def get_thread_by_id(self, thread_id):
        return self.threads[thread_id]


def _snapshot(*, kind='post', thread_id=None) -> GroupClaimSnapshot:
    """Provide a typed unknown claim whose canonical identity is unambiguous."""
    return GroupClaimSnapshot(claim_id=1, kind=kind, group_id=1, channel_id=10,
        object_id=NOTE, actor_id=OWNER, author_name='alice', title='Topic',
        body='Hello', external_url=None, view_state='live', view_revision=0,
        applied_revision=None, reserved_revision=0, token='t',
        operation='reconcile', thread_id=thread_id)


@pytest.mark.asyncio
async def test_real_history_matcher_requires_exact_bot_footer_or_first_comment_marker() -> None:
    """Foreign sender, URL prefixes and body quotations are never POST proof."""
    matched = _FakeThread(111, 10, 9, 'Header\n\nbody\n\n'+NOTE)
    wrong_author = _FakeThread(112, 10, 17, 'Header\n\n'+NOTE)
    body_only = _FakeThread(113, 10, 9, NOTE+'\n\nNo footer')
    bot = _FakeBot([matched, wrong_author, body_only])
    transport = CommunityDiscordViewTransport(bot)
    assert await transport.reconcile(_snapshot()) == (111, 111)
    second = _FakeThread(114, 10, 9, 'Other\n\n'+NOTE)
    bot.threads[114] = second
    bot.forum.threads.append(second)
    assert await transport.reconcile(_snapshot()) is None
    # Exact first paragraph is the only identifying part of a bot comment.
    marker = feed_comment_content(actor_name='alice', actor_id=OWNER, object_id=NOTE, body='body')
    quoted = SimpleNamespace(id=200, author=SimpleNamespace(id=9),
        content='`alice` [↗](<https://nodebb.example/post/420>)\n\n'+NOTE,
        channel=SimpleNamespace(id=111))
    valid = SimpleNamespace(id=201, author=SimpleNamespace(id=9),
        content=marker, channel=SimpleNamespace(id=111))
    bot.threads[111]._messages = [quoted, valid]
    assert await transport.reconcile(_snapshot(kind='comment', thread_id=111)) == (111, 201)
    bot.threads[111]._messages = [quoted]
    assert await transport.reconcile(_snapshot(kind='comment', thread_id=111)) is None

@pytest.mark.asyncio
async def test_same_note_in_two_groups_cannot_adopt_another_groups_root(tmp_path: Path) -> None:
    """A root footer identifies the Note but does not prove Group ownership."""
    db, repo, group_id = _setup(tmp_path)
    try:
        unknown = repo.reserve_remote_target(kind='post', group_id=group_id, discord_channel_id=10)
        repo.mark_remote_target(kind='post', group_id=group_id, discord_channel_id=10,
                                attempt_id=unknown.attempt_id, status='unknown')
        second_group = 'https://other-nodebb.example/category/4'
        # This other Group was subscribed to this forum in the past. Community
        # subscriptions are exclusive per channel, but old bot roots survive
        # removal/reassignment; their marker cannot prove the new claim.
        other = repo.get_or_create_remote_thread_group(
            community_actor_id=second_group, ap_object_id=NOTE,
            source_owner_actor_id=OWNER, ap_activity_id=NOTE+'/second-group-create',
            first_object_json={'ap_id': NOTE, 'kind': 'post', 'author_name': 'bob',
                               'title': 'Other', 'body_markdown': 'Other view'},
        )
        with db.immediate_session() as session:
            session.add(CommunityDiscordTargetClaim(
                thread_group_id=other.id, discord_channel_id=10, kind='post',
                status='completed', attempt_id='historical', applied_revision=0,
                reserved_view_revision=0))
            session.add(CommunityThreadGroupDelivery(
                thread_group_id=other.id, discord_channel_id=10,
                discord_thread_id=500, discord_starter_message_id=500,
                role='inbound'))
        transport = _Transport(proven=(500, 500))
        worker = CommunityGroupViewDeliveryWorker(database=db, bot=object(),
            policy_service=_Policy(), transport=transport)
        assert await worker.drain_once() == 0
        assert transport.reconciled == []
        with db.session() as session:
            claim = session.scalar(select(CommunityDiscordTargetClaim).where(
                CommunityDiscordTargetClaim.thread_group_id == group_id))
            assert claim.status == 'unknown'
            assert 'another Group' in claim.last_error
    finally:
        db.close()
