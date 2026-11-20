"""Signed remote Group receipt, payload identity and crash-window regression.

Run the real SQLite BEGIN IMMEDIATE projection/receipt boundary while the
external Group Actor GET is replaced by a fixed trusted test Actor document.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from src.activitypub_models import ActivityPubEvent
from src.community_sync.group_view_mutations import (
    GroupViewMutationService, GroupMutationConflict, GroupMutationDenied,
)
from src.db import Database
from src.models import ActivityPubEventReceipt, BridgeActorFollow, CommunityThreadGroup

GROUP = 'https://nodebb.example/category/2'
OWNER = 'https://nodebb.example/uid/9'
NOTE = 'https://nodebb.example/post/79'
INBOX = GROUP + '/inbox'


class _Allow:
    """The test exercises the committed projection, not policy enumeration."""

    def federation_decision(self, _actor: str):
        return type('Decision', (), {'allowed': True})()


async def _actor(_group: str) -> dict:
    """Exact NodeBB Group verification response at the secure GET edge."""
    return dict(type='Group', id=GROUP, inbox=INBOX)


def _setup(path: Path) -> tuple[Database, GroupViewMutationService]:
    """Seed the same accepted Follow, subscription and immutable first Create."""
    db = Database(f'sqlite:///{path}')
    db.create_all()
    with db.session() as session:
        session.add(BridgeActorFollow(
            community_actor_id=GROUP, follow_activity_id=GROUP+'/follows/1',
            remote_software_profile='nodebb', community_inbox_url=INBOX,
            status='accepted', follow_dispatch_state='sent',
        ))
    db.remote_subscriptions.create_subscription(discord_channel_id=100,
        lemmy_community_actor_id=GROUP, lemmy_community_name='NodeBB',
        lemmy_community_id=None, status='accepted')
    db.discord_fanout_groups.get_or_create_remote_thread_group(
        community_actor_id=GROUP, ap_object_id=NOTE, source_owner_actor_id=OWNER,
        ap_activity_id=NOTE+'/activities/create', first_object_json={
            'ap_id': NOTE, 'kind': 'post', 'body_markdown': 'Original',
            'published_at': '2026-11-18T12:00:00Z', 'author_name': 'alice',
        },
    )
    return db, GroupViewMutationService(db, _Allow(), actor_reader=_actor)


def _update(number: int, content: str, *, delivery_id: str | None = None,
            signed_group: bool = True, owner: str = OWNER, group: str = GROUP) -> ActivityPubEvent:
    """Construct one normalized Group Announce or signed direct owner Update."""
    inner = f'{GROUP}/activities/update/{number}'
    outer = delivery_id or (f'{GROUP}/announce/update/{number}' if signed_group else inner)
    source = {
        'type': 'Update', 'id': inner, 'actor': owner, 'audience': group,
        'object': {'type': 'Note', 'id': NOTE, 'attributedTo': OWNER,
                   'audience': group, 'content': content},
    }
    return ActivityPubEvent.model_validate({
        'event_type': 'post.updated', 'delivery_id': outer,
        'community_actor_id': group, 'actor_id': owner,
        'verified_sender_actor_id': group if signed_group else owner,
        'source_announce_id': outer if signed_group else None,
        'source_activity_id': inner, 'source_activity_json': source,
        'occurred_at': '2026-11-18T12:00:00Z',
        'object': {'ap_id': NOTE, 'kind': 'post', 'lemmy_id': 79,
                   'published_at': '2026-11-18T12:00:00Z', 'author_name': 'alice',
                   'body_markdown': content},
    })


@pytest.mark.asyncio
async def test_group_receipt_retains_canonical_payload_identity_across_later_mutations(tmp_path: Path) -> None:
    """A reused Activity ID with another body conflicts even after later U2."""
    db, service = _setup(tmp_path / 'receipts.db')
    try:
        u1 = _update(1, 'A')
        u2 = _update(2, 'C')
        assert (await service.apply(u1)).revision == 1
        assert (await service.apply(u2)).revision == 2
        assert (await service.apply(u1)).status == 'duplicate'
        with pytest.raises(GroupMutationConflict):
            await service.apply(_update(1, 'B'))
        with db.session() as session:
            [group] = session.scalars(select(CommunityThreadGroup)).all()
            receipts = session.scalars(select(ActivityPubEventReceipt)).all()
            assert group.view_revision == 2
            assert group.display_object_json['body_markdown'] == 'C'
            assert len(receipts) == 2
            assert all(len(receipt.mutation_fingerprint) == 64 for receipt in receipts)
            assert [receipt.group_view_revision for receipt in receipts] == [1, 2]
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('competing_body', ['A', 'B'])
async def test_receipt_inserted_during_actor_verification_is_rechecked_under_writer_lock(
        tmp_path: Path, competing_body: str) -> None:
    """A concurrent signed delivery cannot double-apply or hide payload conflict.

    The first handler sees no receipt before its Actor GET. A second handler
    commits the wire ID while that external verification is in progress; the
    first handler must re-read the receipt inside its own BEGIN IMMEDIATE.
    """
    db, service = _setup(tmp_path / f'actor-race-{competing_body}.db')
    competitor = GroupViewMutationService(db, _Allow(), actor_reader=_actor)
    checks = []

    async def actor_with_concurrent_delivery(group: str) -> dict:
        """Commit a competing authorized event while the first GET is pending."""
        checks.append(await competitor.apply(_update(1, competing_body)))
        return await _actor(group)

    service.actor_reader = actor_with_concurrent_delivery
    try:
        if competing_body == 'A':
            receipt = await service.apply(_update(1, 'A'))
            assert receipt.status == 'duplicate'
            assert receipt.revision == 1
        else:
            with pytest.raises(GroupMutationConflict, match='different payload'):
                await service.apply(_update(1, 'A'))
        assert len(checks) == 1 and checks[0].revision == 1
        with db.session() as session:
            group = session.scalar(select(CommunityThreadGroup))
            receipts = session.scalars(select(ActivityPubEventReceipt)).all()
            assert group.view_revision == 1
            assert group.display_object_json['body_markdown'] == competing_body
            assert len(receipts) == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_failed_signature_cannot_commit_receipt_or_view(tmp_path: Path) -> None:
    """A forged outer signer is not authorized merely by its inner Note actor."""
    db, service = _setup(tmp_path / 'denials.db')
    try:
        forged = _update(1, 'A').model_copy(update={'verified_sender_actor_id': OWNER})
        with pytest.raises(GroupMutationDenied, match='verified outer signer'):
            await service.apply(forged)
        incorrect_owner = _update(1, 'A', owner='https://evil.example/actors/1')
        with pytest.raises(GroupMutationDenied):
            await service.apply(incorrect_owner)
        with db.session() as session:
            [group] = session.scalars(select(CommunityThreadGroup)).all()
            assert group.view_revision == 0
            assert session.scalars(select(ActivityPubEventReceipt)).all() == []
    finally:
        db.close()


@pytest.mark.asyncio
async def test_direct_owner_mutation_advances_same_group_view(tmp_path: Path) -> None:
    """A real author-signed Update changes the admitted Group projection only."""
    db, service = _setup(tmp_path / 'owner.db')
    try:
        event = _update(1, 'Signed owner', signed_group=False)
        result = await service.apply(event)
        assert result.revision == 1
        with db.session() as session:
            [group] = session.scalars(select(CommunityThreadGroup)).all()
            assert group.display_object_json['body_markdown'] == 'Signed owner'
            assert group.inbound_create_snapshot_json['body_markdown'] == 'Original'
    finally:
        db.close()


@pytest.mark.asyncio
async def test_replay_with_equivalent_jsonld_context_is_duplicate(tmp_path: Path) -> None:
    """JSON-LD envelope decoration cannot invent a new authenticated revision."""
    db, service = _setup(tmp_path / 'semantic_replay.db')
    try:
        first = _update(1, 'same')
        assert (await service.apply(first)).revision == 1
        reordered = first.model_copy(update={'source_activity_json': {
            **first.source_activity_json, '@context': 'https://www.w3.org/ns/activitystreams',
        }})
        assert (await service.apply(reordered)).status == 'duplicate'
        changed_addresses = first.model_copy(update={'source_activity_json': {
            **first.source_activity_json, 'cc': ['https://unrelated.example/group'],
        }})
        with pytest.raises(GroupMutationConflict):
            await service.apply(changed_addresses)
        with db.session() as session:
            assert session.scalar(select(CommunityThreadGroup)).view_revision == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_mutation_does_not_add_a_reply_parent_to_original_root(tmp_path: Path) -> None:
    """Even normalized metadata cannot turn a retained root into a reply."""
    db, service = _setup(tmp_path / 'root_parent.db')
    try:
        initial = _update(1, 'same')
        invalid = initial.model_copy(update={'object': initial.object.model_copy(update={
            'parent_ap_id': 'https://nodebb.example/post/74',
        })})
        with pytest.raises(GroupMutationDenied):
            await service.apply(invalid)
        with db.session() as session:
            assert session.scalar(select(CommunityThreadGroup)).view_revision == 0
            assert session.scalars(select(ActivityPubEventReceipt)).all() == []
    finally:
        db.close()

@pytest.mark.asyncio
async def test_cancelled_follow_cannot_create_revision_but_old_receipt_still_replays(tmp_path: Path) -> None:
    """Idempotency remains stable when the owner no longer accepts new Group work."""
    from src.models import ChannelCommunitySubscription
    db, service = _setup(tmp_path / 'cancelled_follow.db')
    try:
        first = _update(1, 'A')
        assert (await service.apply(first)).revision == 1
        with db.immediate_session() as session:
            follow = session.scalar(select(BridgeActorFollow))
            follow.cancel_requested = True
            binding = session.scalar(select(ChannelCommunitySubscription))
            binding.status = 'cancelled'
        assert (await service.apply(first)).status == 'duplicate'
        with pytest.raises(GroupMutationConflict):
            await service.apply(_update(1, 'B'))
        with pytest.raises(GroupMutationDenied):
            await service.apply(_update(2, 'C'))
        with db.session() as session:
            assert session.scalar(select(CommunityThreadGroup)).view_revision == 1
            assert len(session.scalars(select(ActivityPubEventReceipt)).all()) == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_late_authenticated_update_never_replaces_newer_group_body(tmp_path: Path) -> None:
    """A signed old Note.updated cannot rewind a later authenticated version."""
    db, service = _setup(tmp_path / 'ordered.db')
    try:
        def timed(index: int, body: str, time: str) -> ActivityPubEvent:
            """Keep a single signed body and source timestamp together."""
            event = _update(index, body)
            raw = dict(event.source_activity_json)
            raw['object'] = {**raw['object'], 'updated': time}
            return event.model_copy(update={'source_activity_json': raw})

        new = timed(1, 'NEW', '2026-11-18T12:02:00Z')
        old = timed(2, 'OLD', '2026-11-18T12:01:00Z')
        assert (await service.apply(new)).revision == 1
        assert (await service.apply(old)).status == 'processed'
        with db.session() as session:
            row = session.scalar(select(CommunityThreadGroup))
            assert row.view_revision == 1
            assert row.display_object_json['body_markdown'] == 'NEW'
            assert len(session.scalars(select(ActivityPubEventReceipt)).all()) == 2
        assert (await service.apply(old)).status == 'duplicate'
    finally:
        db.close()


@pytest.mark.asyncio
async def test_untimed_group_updates_continue_in_commit_order(tmp_path: Path) -> None:
    """Absence of an authenticated update timestamp must not block new content."""
    db, service = _setup(tmp_path / 'untimed.db')
    try:
        await service.apply(_update(1, 'A'))
        assert (await service.apply(_update(2, 'B'))).revision == 2
        with db.session() as session:
            row = session.scalar(select(CommunityThreadGroup))
            assert row.display_object_json['body_markdown'] == 'B'
    finally:
        db.close()


@pytest.mark.asyncio
async def test_unzoned_group_update_timestamp_cannot_bypass_source_order(tmp_path: Path) -> None:
    """An ambiguous datetime never becomes a trusted ordering authority."""
    db, service = _setup(tmp_path / 'bad-time.db')
    try:
        event = _update(1, 'Unordered')
        raw = dict(event.source_activity_json)
        raw['object'] = {**raw['object'], 'updated': '2026-11-18T12:00:00'}
        with pytest.raises(GroupMutationDenied, match='timezone'):
            await service.apply(event.model_copy(update={'source_activity_json': raw}))
        with db.session() as session:
            assert session.scalar(select(CommunityThreadGroup)).view_revision == 0
            assert session.scalars(select(ActivityPubEventReceipt)).all() == []
    finally:
        db.close()

@pytest.mark.asyncio
async def test_source_clock_survives_timestampless_update_and_legacy_receive_time(tmp_path: Path) -> None:
    """Signed source ordering is independent of the bridge's receive clock."""
    db, service = _setup(tmp_path / 'source-clock.db')

    def with_timestamp(number: int, body: str, stamp: str | None) -> ActivityPubEvent:
        """Build one fully signed normalized Update with optional Note.updated."""
        event = _update(number, body)
        source = dict(event.source_activity_json)
        source['object'] = dict(source['object'])
        if stamp is not None:
            source['object']['updated'] = stamp
        return event.model_copy(update={'source_activity_json': source})

    try:
        first = await service.apply(with_timestamp(1, 'A', '2026-11-18T12:00:00Z'))
        assert first.revision == 1
        second = await service.apply(with_timestamp(2, 'B', None))
        assert second.revision == 2
        third = await service.apply(with_timestamp(3, 'C', '2026-11-18T12:01:00Z'))
        assert third.revision == 3
        stale = await service.apply(with_timestamp(4, 'OLD', '2026-11-18T11:59:00Z'))
        assert stale.revision == 3
        assert 'stale' in stale.detail.lower() and 'deleted' not in stale.detail.lower()
        with db.session() as session:
            row = session.scalar(select(CommunityThreadGroup))
            assert row.display_object_json['body_markdown'] == 'C'
            assert row.view_source_updated_at.replace(tzinfo=timezone.utc) == datetime(2026, 11, 18, 12, 1, tzinfo=timezone.utc)
        # Before this new column existed, a CP48/49 committed row stored the
        # *receive* time in view_updated_at. It is not trusted source ordering.
        with db.immediate_session() as session:
            row = session.scalar(select(CommunityThreadGroup))
            row.view_updated_at = datetime(2026, 11, 20, 15, 0, tzinfo=timezone.utc)
            row.view_source_updated_at = None
        after_legacy = await service.apply(with_timestamp(5, 'After legacy', '2026-11-18T12:02:00Z'))
        assert after_legacy.revision == 4
    finally:
        db.close()
