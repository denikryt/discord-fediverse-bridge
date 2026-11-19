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
