"""Physical Create receipts persist once across independently opened SQLite sessions."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from sqlalchemy import select
import pytest

from src.db import Database
from src.models import (ChannelSurface, LocalCommunity, LocalCommunityMessage,
                        LocalCommunityMessageSurface, LocalCommunityThread,
                        LocalCommunityThreadSurface, PlatformConnector)


def world(tmp_path):
    """Create an actual host forum claim and file-backed initial persistence implementation SQL schema."""
    db = Database(f"sqlite:///{tmp_path / 'receipts.db'}")
    db.migrate()
    with db.immediate_session() as session:
        connector = session.scalar(select(PlatformConnector).where(
            PlatformConnector.connector_key == 'discord-primary'))
        community = LocalCommunity(
            slug='hackers', display_name='Hackers', actor_url='https://bridge.test/actors/communities/hackers',
            inbox_url='https://bridge.test/actors/communities/hackers/inbox',
            outbox_url='https://bridge.test/actors/communities/hackers/outbox',
            followers_url='https://bridge.test/actors/communities/hackers/followers',
            public_key_pem='public', private_key_pem='private',
        )
        session.add(community)
        session.flush()
        session.add(ChannelSurface(connector_id=connector.id, external_channel_id='300',
                                   external_guild_id='10', kind='forum',
                                   local_community_id=community.id))
        community_id = community.id
    return db, community_id


def thread_arguments(community_id):
    """One remote post and one exact host Discord receipt."""
    return dict(local_community_id=community_id, discord_thread_id=400,
                discord_starter_message_id=401,
                ap_activity_id='https://remote.test/create/post',
                ap_object_id='https://remote.test/post',
                direction='ap_to_discord', origin_kind='remote_follower')


def message_arguments(thread_id):
    """One remote comment with an exact host message/parent receipt."""
    return dict(local_community_thread_id=thread_id, discord_message_id=700,
                ap_activity_id='https://remote.test/create/comment',
                ap_object_id='https://remote.test/comment',
                parent_ap_object_id='https://remote.test/post',
                parent_discord_message_id=401, direction='ap_to_discord')


def counts(db):
    """Query canonical and physical rows from a fresh SQLite transaction."""
    with db.session() as s:
        return tuple(len(list(s.scalars(select(model)))) for model in (
            LocalCommunityThread, LocalCommunityThreadSurface,
            LocalCommunityMessage, LocalCommunityMessageSurface,
        ))


def test_parallel_identical_thread_receipts_share_canonical_and_host_surface(tmp_path):
    """Two handlers consume one receipt concurrently without a UNIQUE conflict."""
    db, community_id = world(tmp_path)
    kwargs = thread_arguments(community_id)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: db.local_community_content.create_local_community_thread(**kwargs), range(2)))
    assert results[0].id == results[1].id
    assert counts(db) == (1, 1, 0, 0)
    db.close()


def test_thread_receipt_conflict_does_not_rewrite_existing_identity(tmp_path):
    """A same-object conflicting activity/thread cannot be adopted as replay."""
    db, community_id = world(tmp_path)
    kwargs = thread_arguments(community_id)
    first = db.local_community_content.create_local_community_thread(**kwargs)
    for changed in ({'ap_activity_id': 'https://forged.test/activity'},
                    {'discord_thread_id': 999}, {'discord_starter_message_id': 999}):
        with pytest.raises(ValueError):
            db.local_community_content.create_local_community_thread(**(kwargs | changed))
    assert counts(db) == (1, 1, 0, 0)
    assert db.local_community_content.get_local_community_thread_by_ap_object_id(kwargs['ap_object_id']).id == first.id
    db.close()


def test_parallel_identical_message_receipts_do_not_conflict(tmp_path):
    """Two handlers persist the same comment and host copy without a duplicate."""
    db, community_id = world(tmp_path)
    thread = db.local_community_content.create_local_community_thread(**thread_arguments(community_id))
    kwargs = message_arguments(thread.id)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: db.local_community_content.create_local_community_message(**kwargs), range(2)))
    assert results[0].id == results[1].id
    assert counts(db) == (1, 1, 1, 1)
    db.close()


def test_message_receipt_conflict_rejected_with_no_side_effect(tmp_path):
    """A same-object changed parent/activity/message cannot corrupt a prior copy."""
    db, community_id = world(tmp_path)
    thread = db.local_community_content.create_local_community_thread(**thread_arguments(community_id))
    kwargs = message_arguments(thread.id)
    first = db.local_community_content.create_local_community_message(**kwargs)
    for changed in ({'ap_activity_id': 'https://forged.test/activity'},
                    {'discord_message_id': 999}, {'parent_discord_message_id': 999},
                    {'parent_ap_object_id': 'https://forged.test/parent'}):
        with pytest.raises(ValueError):
            db.local_community_content.create_local_community_message(**(kwargs | changed))
    assert counts(db) == (1, 1, 1, 1)
    assert db.local_community_content.get_local_community_message_by_ap_object_id(kwargs['ap_object_id']).id == first.id
    db.close()


def test_generic_ap_mapping_recovers_after_partial_handler_failure(tmp_path):
    """A replay after canonical commit restores exactly one AP reply mapping."""
    from src.models import MessageMapping

    db, community_id = world(tmp_path)
    thread = db.local_community_content.create_local_community_thread(**thread_arguments(community_id))
    db.local_community_content.create_local_community_message(**message_arguments(thread.id))
    kwargs = dict(source_platform='activitypub', source_id='https://remote.test/comment',
                  activity_id='https://remote.test/create/comment',
                  object_id='https://remote.test/comment',
                  actor_url='https://remote.test/u/bob',
                  community_actor_url='https://bridge.test/actors/communities/hackers',
                  discord_channel_id=400, discord_message_id=700)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: db.message_mappings.create_message_mapping(**kwargs), range(2)))
    assert results[0].id == results[1].id
    with db.session() as session:
        assert len(session.scalars(select(MessageMapping)).all()) == 1
    db.close()


def test_generic_ap_mapping_same_object_different_author_rejected(tmp_path):
    """A forged duplicate author/Discord receipt cannot reuse saved AP mapping."""
    from src.models import MessageMapping

    db, _ = world(tmp_path)
    kwargs = dict(source_platform='activitypub', source_id='https://remote.test/comment',
                  activity_id='https://remote.test/create/comment',
                  object_id='https://remote.test/comment',
                  actor_url='https://remote.test/u/bob',
                  community_actor_url='https://bridge.test/actors/communities/hackers',
                  discord_channel_id=400, discord_message_id=700)
    db.message_mappings.create_message_mapping(**kwargs)
    for changed in ({'actor_url': 'https://forged.test/u/attacker'},
                    {'activity_id': 'https://forged.test/activity'},
                    {'discord_message_id': 800}):
        with pytest.raises(ValueError):
            db.message_mappings.create_message_mapping(**(kwargs | changed))
    with db.session() as session:
        assert len(session.scalars(select(MessageMapping)).all()) == 1
    db.close()
