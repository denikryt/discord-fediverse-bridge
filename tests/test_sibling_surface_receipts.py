"""Concurrent Discord sibling receipt writes reuse identity, never send again."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select

from src.models import ChannelSurface, LocalCommunityMessageSurface, LocalCommunityThreadSurface, PlatformConnector
from test_local_receipt_persistence import world, thread_arguments, message_arguments


def sibling_world(tmp_path):
    """Seed canonical post/comment and a second physical forum target."""
    db, community = world(tmp_path)
    with db.immediate_session() as session:
        connector = session.scalar(select(PlatformConnector).where(
            PlatformConnector.connector_key == 'discord-primary'))
        session.add(ChannelSurface(
            connector_id=connector.id, external_channel_id='301',
            external_guild_id='10', kind='forum'))
    thread = db.local_community_content.create_local_community_thread(**thread_arguments(community))
    message = db.local_community_content.create_local_community_message(**message_arguments(thread.id))
    thread_values = dict(local_community_thread_id=thread.id, discord_forum_channel_id=301,
                         discord_thread_id=800, discord_starter_message_id=801,
                         role='local_subscriber', local_subscriber_id=5)
    return db, thread_values, message


def test_identical_sibling_thread_receipts_are_idempotent_across_sqlite_sessions(tmp_path):
    """Two handlers processing one sibling Create cannot violate unique row identity."""
    db, values, _ = sibling_world(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(lambda _: db.local_community_surfaces.create_local_community_thread_surface(**values), range(2)))
    assert receipts[0].id == receipts[1].id
    with db.session() as session:
        assert len(session.scalars(select(LocalCommunityThreadSurface)).all()) == 2
    db.close()


def test_sibling_thread_changed_physical_receipt_or_owner_rejected(tmp_path):
    """A competing Discord POST cannot silently take over another sibling target."""
    db, values, _ = sibling_world(tmp_path)
    first = db.local_community_surfaces.create_local_community_thread_surface(**values)
    for change in ({'discord_thread_id': 999}, {'discord_starter_message_id': 999},
                   {'role': 'host'}, {'local_subscriber_id': 6}):
        with pytest.raises(ValueError):
            db.local_community_surfaces.create_local_community_thread_surface(**(values | change))
    assert db.local_community_surfaces.get_local_community_thread_surface(
        local_community_thread_id=values['local_community_thread_id'],
        discord_forum_channel_id=301).id == first.id
    db.close()


def test_identical_sibling_comment_receipts_reused_across_sessions(tmp_path):
    """Two sibling fanouts of one comment share one persisted physical receipt."""
    db, values, message = sibling_world(tmp_path)
    sibling = db.local_community_surfaces.create_local_community_thread_surface(**values)
    kwargs = dict(local_community_message_id=message.id,
                  local_community_thread_surface_id=sibling.id,
                  discord_forum_channel_id=301, discord_message_id=900,
                  parent_discord_message_id=801, role='local_subscriber',
                  local_subscriber_id=5)
    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(lambda _: db.local_community_surfaces.create_local_community_message_surface(**kwargs), range(2)))
    assert receipts[0].id == receipts[1].id
    with db.session() as session:
        assert len(session.scalars(select(LocalCommunityMessageSurface)).all()) == 2
    db.close()


def test_sibling_comment_conflicting_message_parent_or_owner_fails_closed(tmp_path):
    """A stale or forged sibling message cannot rewrite a verified reply mapping."""
    db, values, message = sibling_world(tmp_path)
    sibling = db.local_community_surfaces.create_local_community_thread_surface(**values)
    kwargs = dict(local_community_message_id=message.id,
                  local_community_thread_surface_id=sibling.id,
                  discord_forum_channel_id=301, discord_message_id=900,
                  parent_discord_message_id=801, role='local_subscriber',
                  local_subscriber_id=5)
    saved = db.local_community_surfaces.create_local_community_message_surface(**kwargs)
    for change in ({'discord_message_id': 999}, {'parent_discord_message_id': 999},
                   {'local_subscriber_id': 6}, {'role': 'host'},
                   {'discord_forum_channel_id': 300}):
        with pytest.raises(ValueError):
            db.local_community_surfaces.create_local_community_message_surface(**(kwargs | change))
    assert db.local_community_surfaces.get_local_community_message_surface(
        local_community_message_id=message.id,
        local_community_thread_surface_id=sibling.id).id == saved.id
    db.close()
