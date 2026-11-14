"""Last-unsubscribe/Follow-send interleavings with real v1 SQLite state.

The only mocked boundary is the network after the transaction; these tests
prove that the sender and final unsubscriber cannot both win the same CAS.
"""
from __future__ import annotations
from pathlib import Path
from sqlalchemy import select
from src.db import Database
from src.db.repositories.bridge_actor_follows import BridgeActorFollowRepository
from src.models import BridgeActorFollow, ChannelCommunitySubscription

GROUP = 'https://nodebb.example/category/42'
FOLLOW = 'https://bridge.example/actors/services/bridge/activities/follow/fixed'
INBOX = 'https://nodebb.example/category/42/inbox'


def _db(path: Path) -> Database:
    """Open the same persistent SQLite file through the normal migration path."""
    db = Database(f'sqlite:///{path}')
    db.migrate()
    return db


def _reserve(db: Database, channel: int = 7, *, follow_activity_id: str = FOLLOW) -> int:
    """Reserve one verified, pending NodeBB Follow and Discord forum binding."""
    with db.immediate_session() as s:
        follow, _ = BridgeActorFollowRepository.reserve_with_binding(
            s, community_actor_id=GROUP, community_inbox_url=INBOX,
            follow_activity_id=follow_activity_id, remote_software_profile='nodebb',
            discord_channel_id=channel, discord_guild_id=8,
            community_name='General', numeric_id=None,
            community_handle='!general@nodebb.example',
            initiated_by_discord_user_id='1')
        return follow.id


def test_prepared_unsubscribe_cancels_before_any_network_and_restarts_new_generation(tmp_path: Path) -> None:
    """The last channel can atomically cancel an unsent Follow before its claim."""
    db = _db(tmp_path/'db.sqlite')
    try:
        follow_id = _reserve(db)
        result = db.bridge_actor_follows.cancel_channel_binding(7)
        assert result.outcome == 'prepared_cancelled'
        assert not db.bridge_actor_follows.claim_prepared_follow(follow_id)
        assert db.remote_subscriptions.get_subscription_by_channel(7) is None
        assert db.bridge_actor_follows.get_bridge_actor_follow(GROUP) is None
        _reserve(db, follow_activity_id=FOLLOW+'-new')
        assert db.bridge_actor_follows.get_bridge_actor_follow(GROUP).follow_activity_id == FOLLOW+'-new'
    finally:
        db.close()


def test_sending_cancel_and_resubscribe_reuse_generation_without_resend(tmp_path: Path) -> None:
    """An unconfirmed Follow send is never replaced with a new ID on restart."""
    path=tmp_path/'db.sqlite'
    db=_db(path)
    try:
        follow_id=_reserve(db)
        assert db.bridge_actor_follows.claim_prepared_follow(follow_id)
        result=db.bridge_actor_follows.cancel_channel_binding(7)
        assert result.outcome=='await_follow_send'
        assert db.bridge_actor_follows.get_bridge_actor_follow(GROUP).cancel_requested
    finally:
        db.close()
    db=_db(path)
    try:
        with db.immediate_session() as s:
            follow, should_send=BridgeActorFollowRepository.reserve_with_binding(
                s, community_actor_id=GROUP,community_inbox_url=INBOX,
                follow_activity_id=FOLLOW+'-new',remote_software_profile='nodebb',
                discord_channel_id=8,discord_guild_id=8,community_name='General',
                numeric_id=None,community_handle='!general@nodebb.example',
                initiated_by_discord_user_id='1')
            assert follow.id==follow_id and not should_send
            assert not follow.cancel_requested
        assert not db.bridge_actor_follows.claim_prepared_follow(follow_id)
    finally:
        db.close()


def test_confirmed_send_claims_deterministic_undo_once_and_blocks_rebind(tmp_path: Path) -> None:
    """Undo claim persists before network so an uncertain result blocks a new Follow."""
    db=_db(tmp_path/'db.sqlite')
    try:
        follow_id=_reserve(db)
        assert db.bridge_actor_follows.claim_prepared_follow(follow_id)
        assert db.bridge_actor_follows.complete_prepared_follow(follow_id)
        assert db.bridge_actor_follows.cancel_channel_binding(7).outcome=='ready_for_undo'
        assert db.bridge_actor_follows.claim_follow_undo(GROUP,FOLLOW)
        assert not db.bridge_actor_follows.claim_follow_undo(GROUP,FOLLOW)
        assert db.bridge_actor_follows.get_bridge_actor_follow(GROUP).follow_dispatch_state=='undo_sending'
        from src.db.repositories.bridge_actor_follows import FollowReservationConflict
        import pytest
        with pytest.raises(FollowReservationConflict, match='cleanup'):
            _reserve(db, channel=8)
        assert db.remote_subscriptions.get_subscription_by_channel(8) is None
    finally:
        db.close()


def test_confirmed_undo_cleanup_checks_generation_and_no_live_channels(tmp_path: Path) -> None:
    """An Undo result cannot erase a new generation or any surviving binding."""
    db=_db(tmp_path/'db.sqlite')
    try:
        follow_id=_reserve(db)
        db.bridge_actor_follows.claim_prepared_follow(follow_id)
        db.bridge_actor_follows.complete_prepared_follow(follow_id)
        db.bridge_actor_follows.cancel_channel_binding(7)
        db.bridge_actor_follows.claim_follow_undo(GROUP,FOLLOW)
        assert not db.bridge_actor_follows.finish_follow_undo(GROUP,FOLLOW+'-wrong')
        assert db.bridge_actor_follows.finish_follow_undo(GROUP,FOLLOW)
        assert db.bridge_actor_follows.get_bridge_actor_follow(GROUP) is None
        assert not db.bridge_actor_follows.finish_follow_undo(GROUP,FOLLOW)
    finally:
        db.close()


def test_nonlast_unsubscribe_keeps_original_follow_and_second_channel(tmp_path: Path) -> None:
    """A channel leaving a shared Group never requests Undo on behalf of peers."""
    db=_db(tmp_path/'db.sqlite')
    try:
        fid=_reserve(db)
        _reserve(db,8)
        result=db.bridge_actor_follows.cancel_channel_binding(7)
        assert result.outcome=='other_bindings'
        assert db.remote_subscriptions.get_subscription_by_channel(8) is not None
        assert db.bridge_actor_follows.get_bridge_actor_follow(GROUP).id==fid
        assert not db.bridge_actor_follows.get_bridge_actor_follow(GROUP).cancel_requested
    finally:
        db.close()


def test_sent_cancel_is_rebindable_only_before_undo_claim(tmp_path: Path) -> None:
    """A proven Follow may be recovered while Undo has not acquired its CAS."""
    db = _db(tmp_path / "db.sqlite")
    try:
        source_row = _reserve(db)
        assert db.bridge_actor_follows.claim_prepared_follow(source_row)
        assert db.bridge_actor_follows.complete_prepared_follow(source_row)
        assert db.bridge_actor_follows.cancel_channel_binding(7).outcome == "ready_for_undo"
        with db.immediate_session() as session:
            rebound, sending = BridgeActorFollowRepository.reserve_with_binding(
                session,
                community_actor_id=GROUP,
                community_inbox_url=INBOX,
                follow_activity_id=FOLLOW + "-new",
                remote_software_profile="nodebb",
                discord_channel_id=8,
                discord_guild_id=8,
                community_name="General",
                numeric_id=None,
                community_handle="!general@nodebb.example",
                initiated_by_discord_user_id="1",
            )
            assert rebound.id == source_row
            assert rebound.follow_activity_id == FOLLOW
            assert not rebound.cancel_requested
            assert not sending
        # Undo must lose the CAS: it cannot erase a generation now used by a
        # live channel even if an old unsubscribe caller resumes late.
        assert not db.bridge_actor_follows.claim_follow_undo(GROUP, FOLLOW)
        assert db.remote_subscriptions.get_subscription_by_channel(8) is not None
    finally:
        db.close()
