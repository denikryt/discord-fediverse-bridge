"""CP36 regression: remote Community claims must respect Group view and uncertainty.

The tests use actual SQLite writers and only model the external Discord result.
A lost HTTP response is not evidence that Discord did not create the message.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

from src.db import Database
from src.models import CommunityDiscordTargetClaim, CommunityThreadGroupDelivery

GROUP = "https://nodebb.example/category/19"
OWNER = "https://users.example/actors/alice"
NOTE = "https://users.example/notes/79"


def _world(path: Path) -> tuple[Database, int]:
    """Seed a verified original Create and two independently subscribed forums."""
    db = Database(f"sqlite:///{path}")
    db.create_all()
    for channel in (101, 102):
        db.remote_subscriptions.create_subscription(
            discord_channel_id=channel, lemmy_community_actor_id=GROUP,
            lemmy_community_name="NodeBB", lemmy_community_id=None,
            status="accepted",
        )
    root = db.discord_fanout_groups.get_or_create_remote_thread_group(
        community_actor_id=GROUP, ap_object_id=NOTE,
        source_owner_actor_id=OWNER, ap_activity_id=f"{NOTE}/activity/create",
        first_object_json={"ap_id": NOTE, "kind": "post", "body_markdown": "Original"},
    )
    return db, root.id


def test_group_tombstone_prevents_late_fanout_even_while_person_note_exists(tmp_path: Path) -> None:
    """After Group removal, a second subscriber cannot receive first-Create content.

    Simulate the durable, already authorized Group-state commit inside the DB,
    then exercise the real pre-Discord admission boundary. No Person GET is
    sufficient to authorize Group-specific redistributions.
    """
    db, group_id = _world(tmp_path / "group-tombstone.sqlite")
    try:
        # CP35 has no mutable Group state: this precise statement fails before
        # CP38 rather than silently treating a processed receipt as a tombstone.
        with db.immediate_session() as session:
            session.connection().exec_driver_sql(
                "UPDATE community_thread_groups SET view_state='deleted', "
                "view_revision=2 WHERE id=?", (group_id,),
            )
        reservation = db.discord_fanout_groups.reserve_remote_target(
            kind="post", group_id=group_id, discord_channel_id=102,
        )
        assert reservation.state != "reserved"
        with db.session() as session:
            assert session.scalars(select(CommunityThreadGroupDelivery)).all() == []
    finally:
        db.close()


def test_lost_ack_keeps_one_unresolved_claim_without_duplicate_post(tmp_path: Path) -> None:
    """A timeout cannot manufacture a Discord ID or permit a second Create."""
    db, group_id = _world(tmp_path / "unknown-claim.sqlite")
    try:
        repo = db.discord_fanout_groups
        first = repo.reserve_remote_target(kind="post", group_id=group_id, discord_channel_id=101)
        assert first.state == "reserved" and first.attempt_id
        # The mocked Discord edge may have accepted a POST; the response was lost.
        repo.mark_remote_target(kind="post", group_id=group_id, discord_channel_id=101,
                                attempt_id=first.attempt_id, status="unknown")
        repeated = repo.reserve_remote_target(kind="post", group_id=group_id, discord_channel_id=101)
        assert repeated.state == "unknown"
        with db.session() as session:
            [claim] = session.scalars(select(CommunityDiscordTargetClaim)).all()
            assert claim.attempt_id == first.attempt_id
            assert session.scalars(select(CommunityThreadGroupDelivery)).all() == []
    finally:
        db.close()
