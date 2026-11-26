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


@pytest.mark.asyncio
async def test_deleted_known_copy_is_cleaned_after_unsubscribe(tmp_path: Path) -> None:
    """Cancelling a future distribution does not veto a committed old tombstone."""
    from src.models import ChannelCommunitySubscription
    db, repo, group_id = _setup(tmp_path)
    try:
        attempt = repo.reserve_remote_target(kind='post', group_id=group_id, discord_channel_id=10)
        repo.finish_remote_target(kind='post', group_id=group_id, discord_channel_id=10,
            attempt_id=attempt.attempt_id, discord_thread_id=201, discord_object_id=201)
        with db.immediate_session() as session:
            group = session.get(CommunityThreadGroup, group_id)
            group.view_revision = 1
            group.view_state = 'deleted'
            session.scalar(select(ChannelCommunitySubscription)).status = 'cancelled'
        transport = _Transport()
        worker = CommunityGroupViewDeliveryWorker(database=db, bot=object(),
            policy_service=_Policy(), transport=transport)
        assert await worker.drain_once() == 1
        assert transport.edited[0][0].view_state == 'deleted'
        with db.session() as session:
            assert session.scalar(select(CommunityDiscordTargetClaim)).applied_revision == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_unknown_message_404_completes_only_tombstone(tmp_path: Path) -> None:
    """Discord 10008 proves a deleted copy is gone, but not a live Edit."""
    db, repo, group_id = _setup(tmp_path)
    try:
        attempt = repo.reserve_remote_target(kind='post', group_id=group_id, discord_channel_id=10)
        repo.finish_remote_target(kind='post', group_id=group_id, discord_channel_id=10,
            attempt_id=attempt.attempt_id, discord_thread_id=201, discord_object_id=201)
        with db.immediate_session() as session:
            group = session.get(CommunityThreadGroup, group_id)
            group.view_revision = 1
            group.view_state = 'deleted'

        class Missing(_Transport):
            """Return the exact documented discord.py Unknown Message REST code."""
            async def edit(self, claim):
                """No bot-owned message survives at the recorded ID."""
                self.edited.append((claim, _render(claim)))
                error = RuntimeError('Unknown Message')
                error.status, error.code = 404, 10008
                raise error

        transport = Missing()
        worker = CommunityGroupViewDeliveryWorker(database=db, bot=object(),
            policy_service=_Policy(), transport=transport)
        assert await worker.drain_once() == 1
        with db.session() as session:
            claim = session.scalar(select(CommunityDiscordTargetClaim))
            assert claim.applied_revision == 1 and claim.last_error is None
    finally:
        db.close()


@pytest.mark.asyncio
async def test_worker_rotates_past_newly_pending_early_claims(tmp_path: Path) -> None:
    """Twenty busy destinations cannot indefinitely prevent delivery to #21."""
    db, repo, _ = _setup(tmp_path)
    try:
        for index in range(21):
            root = repo.get_or_create_remote_thread_group(
                community_actor_id=GROUP, ap_object_id=f'{NOTE}/{index}',
                source_owner_actor_id=OWNER, ap_activity_id=f'{NOTE}/{index}/create',
                first_object_json={'ap_id': f'{NOTE}/{index}', 'kind': 'post',
                                   'author_name': 'bob', 'body_markdown': 'First'})
            r = repo.reserve_remote_target(kind='post', group_id=root.id, discord_channel_id=10)
            repo.finish_remote_target(kind='post', group_id=root.id, discord_channel_id=10,
                attempt_id=r.attempt_id, discord_thread_id=300+index, discord_object_id=300+index)
        with db.immediate_session() as session:
            for row in session.scalars(select(CommunityThreadGroup)):
                row.view_revision = 1
        transport = _Transport()
        worker = CommunityGroupViewDeliveryWorker(database=db, bot=object(),
            policy_service=_Policy(), transport=transport)
        assert await worker.drain_once() == 20
        with db.immediate_session() as session:
            # Recurrent work at the front must not starve the last claim.
            early_claims = list(session.scalars(select(CommunityDiscordTargetClaim).order_by(CommunityDiscordTargetClaim.id)))[:20]
            for claim in early_claims:
                session.get(CommunityThreadGroup, claim.thread_group_id).view_revision += 1
        assert await worker.drain_once() >= 1
        with db.session() as session:
            claims = list(session.scalars(select(CommunityDiscordTargetClaim).order_by(CommunityDiscordTargetClaim.id)))
            assert claims[-1].applied_revision == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_404_unknown_message_does_not_complete_live_update(tmp_path: Path) -> None:
    """Only a committed Delete can interpret an absent Discord ID as success."""
    db, repo, group_id = _setup(tmp_path)
    try:
        attempt = repo.reserve_remote_target(kind='post', group_id=group_id, discord_channel_id=10)
        repo.finish_remote_target(kind='post', group_id=group_id, discord_channel_id=10,
            attempt_id=attempt.attempt_id, discord_thread_id=201, discord_object_id=201)
        with db.immediate_session() as session:
            group = session.get(CommunityThreadGroup, group_id)
            group.view_revision = 1
            group.display_object_json = {**group.display_object_json, 'body_markdown': 'Changed'}

        class MissingLive(_Transport):
            """The remote Discord message has disappeared despite a live Group Note."""
            async def edit(self, claim):
                """Return a concrete Discord Unknown Message response."""
                error = RuntimeError('Unknown Message')
                error.status, error.code = 404, 10008
                raise error

        worker = CommunityGroupViewDeliveryWorker(database=db, bot=object(),
            policy_service=_Policy(), transport=MissingLive())
        assert await worker.drain_once() == 1
        with db.session() as session:
            claim = session.scalar(select(CommunityDiscordTargetClaim))
            assert claim.applied_revision == 0 and claim.next_attempt_at is not None
    finally:
        db.close()

@pytest.mark.asyncio
async def test_idle_completed_claims_are_not_writer_scanned(tmp_path: Path) -> None:
    """An idle drain pages due rows and opens no per-claim writer transactions."""
    db, repo, group_id = _setup(tmp_path)
    try:
        for index in range(60):
            root = repo.get_or_create_remote_thread_group(
                community_actor_id=GROUP, ap_object_id=f'{NOTE}/idle/{index}',
                source_owner_actor_id=OWNER, ap_activity_id=f'{NOTE}/idle/{index}/create',
                first_object_json={'ap_id': f'{NOTE}/idle/{index}', 'kind': 'post',
                                   'body_markdown': 'First', 'author_name': 'bob'})
            reservation = repo.reserve_remote_target(kind='post', group_id=root.id, discord_channel_id=10)
            repo.finish_remote_target(kind='post', group_id=root.id, discord_channel_id=10,
                attempt_id=reservation.attempt_id, discord_thread_id=1000 + index,
                discord_object_id=1000 + index)
        transport = _Transport()
        worker = CommunityGroupViewDeliveryWorker(database=db, bot=object(),
            policy_service=_Policy(), transport=transport)
        writes = 0
        original = db.immediate_session

        def tracked():
            """Count only writer transactions; read-only candidate pages are fine."""
            nonlocal writes
            writes += 1
            return original()

        db.immediate_session = tracked
        assert await worker.drain_once() == 0
        assert writes == 0
        assert transport.edited == []
    finally:
        db.close()

@pytest.mark.asyncio
async def test_paginated_scan_reaches_late_due_copy_without_unbounded_writers(tmp_path: Path) -> None:
    """Many markerless unknowns cannot consume unbounded work or hide a late Edit."""
    db, repo, group_id = _setup(tmp_path)
    try:
        for index in range(26):
            obj = f'{NOTE}/unknown/{index}'
            root = repo.get_or_create_remote_thread_group(
                community_actor_id=GROUP, ap_object_id=obj,
                source_owner_actor_id=OWNER, ap_activity_id=obj+'/create',
                first_object_json={'ap_id': obj, 'kind': 'post',
                                   'body_markdown': 'First', 'author_name': 'bob'})
            reserved = repo.reserve_remote_target(kind='post', group_id=root.id, discord_channel_id=10)
            repo.mark_remote_target(kind='post', group_id=root.id, discord_channel_id=10,
                                    attempt_id=reserved.attempt_id, status='unknown')
            with db.immediate_session() as session:
                claim = session.scalar(select(CommunityDiscordTargetClaim).where(
                    CommunityDiscordTargetClaim.thread_group_id == root.id))
                claim.reconciliation_marker_version = None
        late = repo.reserve_remote_target(kind='post', group_id=group_id, discord_channel_id=10)
        repo.finish_remote_target(kind='post', group_id=group_id, discord_channel_id=10,
            attempt_id=late.attempt_id, discord_thread_id=900, discord_object_id=900)
        with db.immediate_session() as session:
            session.get(CommunityThreadGroup, group_id).view_revision = 1
        worker = CommunityGroupViewDeliveryWorker(database=db, bot=object(),
            policy_service=_Policy(), transport=_Transport())
        worker.MAX_SCANNED_CLAIMS = 10
        worker.PAGE_SIZE = 4
        original = db.immediate_session
        writes = 0

        def count_writes():
            """Record each preflight claim lock and result commit."""
            nonlocal writes
            writes += 1
            return original()

        db.immediate_session = count_writes
        for _ in range(3):
            before = writes
            assert await worker.drain_once() <= worker.MAX_NETWORK_ACTIONS
            assert writes - before <= worker.MAX_SCANNED_CLAIMS + 1
        with db.session() as session:
            claim = session.scalar(select(CommunityDiscordTargetClaim).where(
                CommunityDiscordTargetClaim.thread_group_id == group_id))
            assert claim.applied_revision == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_group_root_update_and_delete_keep_embeds_suppressed_and_sync_title(tmp_path: Path) -> None:
    """Both retained forum roots converge to the new title and safe tombstone."""
    db, repo, group_id = _setup(tmp_path)
    try:
        db.remote_subscriptions.create_subscription(discord_channel_id=20,
            lemmy_community_actor_id=GROUP, lemmy_community_name='Group',
            lemmy_community_id=None, status='accepted')
        threads = []
        for channel_id, thread_id in ((10, 201), (20, 202)):
            r = repo.reserve_remote_target(kind='post', group_id=group_id, discord_channel_id=channel_id)
            repo.finish_remote_target(kind='post', group_id=group_id, discord_channel_id=channel_id,
                attempt_id=r.attempt_id, discord_thread_id=thread_id, discord_object_id=thread_id)
            thread = _EditableThread(thread_id, channel_id, 9, 'Original\n\n'+NOTE)
            threads.append(thread)
        worker = CommunityGroupViewDeliveryWorker(database=db, bot=_FakeBot(threads), policy_service=_Policy())
        with db.immediate_session() as session:
            group = session.get(CommunityThreadGroup, group_id)
            group.view_revision = 1
            group.display_object_json = {**group.display_object_json, 'title': 'New topic', 'body_markdown': 'New body'}
        assert await worker.drain_once() == 2
        for thread in threads:
            assert thread.name == 'New topic'
            assert 'New body' in thread._starter.content
            assert thread._starter.suppressed
            assert thread._starter.content.splitlines()[-1] == NOTE
        with db.immediate_session() as session:
            group = session.get(CommunityThreadGroup, group_id)
            group.view_revision = 2
            group.view_state = 'deleted'
        assert await worker.drain_once() == 2
        for thread in threads:
            assert 'Deleted from Community' in thread._starter.content
            assert 'New body' not in thread._starter.content
            assert thread._starter.suppressed
            assert thread.name == 'New topic'  # Keep the existing thread lifecycle on Delete.
            assert thread.renames == ['New topic']
        with db.session() as session:
            assert [c.applied_revision for c in session.scalars(select(CommunityDiscordTargetClaim))] == [2, 2]
    finally:
        db.close()


class _EditableMessage:
    """Model Discord's content and suppress-embeds flag as separate state."""

    def __init__(self, message_id, thread_id, bot_id, content):
        """Create a retained, initially suppressed bot message."""
        self.id = message_id
        self.channel = SimpleNamespace(id=thread_id)
        self.author = SimpleNamespace(id=bot_id)
        self.content = content
        self.suppressed = True

    async def edit(self, *, content, suppress=False, **_kwargs):
        """Simulate discord.py's default, which clears suppression on edit."""
        self.content = content
        self.suppressed = suppress


class _EditableThread(_FakeThread):
    """Model independent message and thread PATCH operations by exact ID."""

    def __init__(self, thread_id, parent_id, bot_id, content):
        """Expose the saved forum starter and distinct replies."""
        super().__init__(thread_id, parent_id, bot_id, content)
        self.name = 'Topic'
        self.renames = []
        self.fail_rename = False
        self._starter = _EditableMessage(thread_id, thread_id, bot_id, content)
        self.messages = {thread_id: self._starter}

    async def fetch_message(self, message_id):
        """Require the exact stored Discord ID, not a thread-level guess."""
        return self.messages[message_id]

    async def edit(self, *, name):
        """Record only completed thread-name updates."""
        if self.fail_rename:
            raise RuntimeError('Discord thread name edit failed')
        self.name = name
        self.renames.append(name)


@pytest.mark.asyncio
async def test_group_root_title_failure_leaves_revision_due_and_comment_never_renames(tmp_path: Path) -> None:
    """A partial root edit is retryable, while a reply cannot rename its parent."""
    db, repo, group_id = _setup(tmp_path)
    try:
        r = repo.reserve_remote_target(kind='post', group_id=group_id, discord_channel_id=10)
        repo.finish_remote_target(kind='post', group_id=group_id, discord_channel_id=10,
            attempt_id=r.attempt_id, discord_thread_id=201, discord_object_id=201)
        thread = _EditableThread(201, 10, 9, 'Original\n\n'+NOTE)
        transport = CommunityDiscordViewTransport(_FakeBot([thread]))
        worker = CommunityGroupViewDeliveryWorker(database=db, bot=object(), policy_service=_Policy(), transport=transport)
        with db.immediate_session() as session:
            group = session.get(CommunityThreadGroup, group_id)
            group.view_revision = 1
            group.display_object_json = {**group.display_object_json, 'title': 'X'*120}
        thread.fail_rename = True
        assert await worker.drain_once() == 1
        with db.immediate_session() as session:
            claim = session.scalar(select(CommunityDiscordTargetClaim))
            assert claim.applied_revision == 0 and claim.next_attempt_at is not None
            claim.next_attempt_at = None
        thread.fail_rename = False
        assert await worker.drain_once() == 1
        assert thread.name == 'X'*99+'…'
        with db.session() as session:
            assert session.scalar(select(CommunityDiscordTargetClaim)).applied_revision == 1
        thread.messages[301] = _EditableMessage(301, 201, 9, 'Reply')
        await transport.edit(replace(_snapshot(kind='comment', thread_id=201), message_id=301, title='Not a topic'))
        assert thread.renames == ['X'*99+'…']
        assert thread.messages[301].suppressed
        # A cache that has not seen the previous PATCH can still report the
        # old title. Restoring that title must not skip the Discord operation.
        thread.name = 'Topic'
        with db.immediate_session() as session:
            group = session.get(CommunityThreadGroup, group_id)
            group.view_revision = 2
            group.display_object_json = {**group.display_object_json, 'title': 'Topic'}
        assert await worker.drain_once() == 1
        assert thread.renames == ['X'*99+'…', 'Topic']
    finally:
        db.close()


@pytest.mark.asyncio
async def test_group_comment_delete_without_inner_audience_updates_both_saved_copies(tmp_path: Path) -> None:
    """NodeBB /post/ reply deletion uses the proven Group and exact two Discord IDs."""
    from src.activitypub_models import ActivityPubEvent
    from src.community_sync.edit_delete import canonicalize_remote_delete_kind
    from src.community_sync.group_view_mutations import GroupViewMutationService
    from src.models import BridgeActorFollow, CommunityMessageGroup

    db, repo, group_id = _setup(tmp_path)
    comment_id = 'https://nodebb.example/post/43'
    try:
        db.remote_subscriptions.create_subscription(discord_channel_id=20,
            lemmy_community_actor_id=GROUP, lemmy_community_name='Group',
            lemmy_community_id=None, status='accepted')
        with db.session() as session:
            session.add(BridgeActorFollow(community_actor_id=GROUP,
                follow_activity_id=GROUP+'/follow', community_inbox_url=GROUP+'/inbox',
                remote_software_profile='nodebb', status='accepted', follow_dispatch_state='sent'))
        reply = repo.get_or_create_remote_message_group(community_actor_id=GROUP, ap_object_id=comment_id,
            source_owner_actor_id=OWNER, ap_activity_id=comment_id+'/create',
            thread_group_id=group_id, parent_message_group_id=None,
            first_object_json={'ap_id': comment_id, 'kind': 'comment', 'body_markdown': 'Reply',
                'author_name': 'bob', 'published_at': '2026-11-18T12:00:00Z'})
        threads = []
        for channel_id, thread_id, message_id in ((10, 201, 301), (20, 202, 302)):
            for kind, gid, mid in (('post', group_id, thread_id), ('comment', reply.id, message_id)):
                r = repo.reserve_remote_target(kind=kind, group_id=gid, discord_channel_id=channel_id)
                repo.finish_remote_target(kind=kind, group_id=gid, discord_channel_id=channel_id,
                    attempt_id=r.attempt_id, discord_thread_id=thread_id, discord_object_id=mid)
            thread = _EditableThread(thread_id, channel_id, 9, 'Root content')
            thread.messages[message_id] = _EditableMessage(message_id, thread_id, 9, 'Old reply')
            threads.append(thread)
        inner = {'type': 'Delete', 'actor': OWNER, 'id': comment_id+'/delete', 'object': comment_id}
        wire = GROUP+'/announce/delete-43'
        event = ActivityPubEvent.model_validate({'event_type': 'post.deleted', 'delivery_id': wire,
            'source_announce_id': wire, 'source_activity_id': inner['id'], 'source_activity_json': inner,
            'community_actor_id': GROUP, 'actor_id': OWNER, 'verified_sender_actor_id': GROUP,
            'occurred_at': '2026-11-18T13:00:00Z', 'object': {'ap_id': comment_id, 'kind': 'post',
                'lemmy_id': 43, 'author_name': '', 'published_at': '2026-11-18T13:00:00Z'}})
        event = canonicalize_remote_delete_kind(db, event)
        assert event.event_type == 'comment.deleted'

        async def verified_actor(_group):
            """Return the same Group/inbox that the accepted Follow stores."""
            return {'type': 'Group', 'id': GROUP, 'inbox': GROUP+'/inbox'}

        service = GroupViewMutationService(db, _Policy(), actor_reader=verified_actor)
        assert (await service.apply(event)).revision == 1
        assert (await service.apply(event)).status == 'duplicate'
        worker = CommunityGroupViewDeliveryWorker(database=db, bot=_FakeBot(threads), policy_service=_Policy())
        assert await worker.drain_once() == 2
        assert await worker.drain_once() == 0
        for thread, mid in zip(threads, (301, 302)):
            assert thread._starter.content == 'Root content'
            assert thread.name == 'Topic' and thread.renames == []
            assert '*Deleted from Community*' in thread.messages[mid].content
            assert 'Old reply' not in thread.messages[mid].content and thread.messages[mid].suppressed
        with db.session() as session:
            assert session.get(CommunityMessageGroup, reply.id).view_state == 'deleted'
            assert session.get(CommunityThreadGroup, group_id).view_state == 'live'
            claims = list(session.scalars(select(CommunityDiscordTargetClaim)))
            assert [c.applied_revision for c in claims if c.kind == 'comment'] == [1, 1]
            assert [c.applied_revision for c in claims if c.kind == 'post'] == [0, 0]
    finally:
        db.close()


@pytest.mark.asyncio
async def test_shared_community_edits_and_tombstones_keep_link_previews_suppressed() -> None:
    """Legacy/local Community helpers cannot restore an old embed on a bot copy."""
    from src.content_sync.edit_delete import edit_discord_message, mark_discord_message_deleted

    thread = _EditableThread(201, 10, 9, '`bob`\n\nOriginal')
    bot = _FakeBot([thread])
    await edit_discord_message(bot=bot, discord_thread_id=201, discord_message_id=201,
        new_content='Updated', preserve_header=True)
    assert thread._starter.content == '`bob`\n\nUpdated'
    assert thread._starter.suppressed
    await mark_discord_message_deleted(bot=bot, discord_thread_id=201, discord_message_id=201)
    assert thread._starter.content == '*deleted by creator*'
    assert thread._starter.suppressed
