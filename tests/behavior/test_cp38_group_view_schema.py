"""Updated SQLite v2 Group view is durable without separate view/cursor tables.

Use a literal frozen v1 schema on disk; assertions exercise migration and the
same production target-admission writer that precedes Discord POST.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import select

from src.db import Database
from src.db.migrations import read_database_state
from src.models import (
    ActivityPubEventReceipt, CommunityDiscordTargetClaim, CommunityThreadGroup,
)
from src.formatting import feed_comment_content, render_feed_discord_message, has_feed_comment_identity

GROUP = "https://g.example/c/5"
OTHER = "https://g.example/c/6"
POST = "https://author.example/n/42"
AUTHOR = "https://author.example/users/alice"


def _post_json() -> dict:
    return dict(ap_id=POST, kind="post", published_at="2026-01-01T01:00:00Z",
                author_name="alice", body_markdown="Original")


def _v1(path: Path) -> None:
    """Create an immutable v2 input without relying on current ORM."""
    schema = Path(__file__).parents[2] / "src/db/schema_versions/v1.sql"
    with sqlite3.connect(path) as conn:
        conn.executescript(schema.read_text())
        conn.execute("PRAGMA user_version=1")
        conn.execute("INSERT INTO community_thread_groups"
                     "(id,community_actor_id,ap_object_id,source_owner_actor_id,"
                     "inbound_create_snapshot_json,created_at) VALUES(1,?,?,?,?,?)",
                     (GROUP, POST, AUTHOR, json.dumps(_post_json()), "2026-01-01"))
        conn.execute("INSERT INTO community_thread_group_deliveries"
                     "(thread_group_id,discord_channel_id,discord_thread_id,discord_starter_message_id,role)"
                     " VALUES(1,10,100,100,'inbound')")
        conn.execute("INSERT INTO community_thread_group_deliveries"
                     "(thread_group_id,discord_channel_id,discord_thread_id,discord_starter_message_id,role)"
                     " VALUES(1,11,110,110,'mirror')")
        conn.execute("INSERT INTO community_discord_target_claims"
                     "(thread_group_id,discord_channel_id,kind,status,attempt_id)"
                     " VALUES(1,12,'post','unknown','old-attempt')")
        conn.execute("INSERT INTO community_discord_target_claims"
                     "(thread_group_id,discord_channel_id,kind,status,attempt_id)"
                     " VALUES(1,13,'post','claimed','old-inflight')")


def test_populated_v1_to_updated_v2_retains_proof_and_unknown_claims(tmp_path: Path) -> None:
    """Never convert markerless unknown or local mirror into completed proof."""
    path = tmp_path / "populated.db"
    _v1(path)
    db = Database(f"sqlite:///{path}")
    try:
        assert read_database_state(db.engine).profile == 'ready_v1'
        db.migrate()
        assert read_database_state(db.engine).profile == 'ready_v2'
        with db.session() as sess:
            group = sess.get(CommunityThreadGroup, 1)
            assert group.view_revision == 0 and group.view_state == 'live'
            assert group.view_source_updated_at is None
            assert group.inbound_create_snapshot_json == _post_json()
            assert group.display_object_json == _post_json()
            claims = {c.discord_channel_id: c for c in sess.scalars(select(CommunityDiscordTargetClaim))}
            assert sorted(claims) == [10, 12, 13]
            assert claims[10].status == 'completed' and claims[10].applied_revision == 0
            assert claims[12].status == 'unknown' and claims[12].attempt_id == 'old-attempt'
            assert claims[12].applied_revision is None
            assert claims[13].status == 'claimed' and claims[13].applied_revision is None
        with db.engine.connect() as c:
            assert c.exec_driver_sql('PRAGMA integrity_check').scalar_one() == 'ok'
            assert c.exec_driver_sql('PRAGMA foreign_key_check').all() == []
            for table in ('community_group_views', 'community_group_view_deliveries'):
                assert c.exec_driver_sql('SELECT COUNT(*) FROM sqlite_master WHERE name=?', (table,)).scalar_one() == 0
        previous = path.read_bytes()
        db.migrate()
        assert previous == path.read_bytes()
    finally:
        db.close()


def test_current_view_is_reserved_without_changing_original_note(tmp_path: Path) -> None:
    """New B sees Group U1, not a stale Note or a later replay of first Create."""
    db = Database(f"sqlite:///{tmp_path / 'new.db'}")
    try:
        db.migrate()
        db.remote_subscriptions.create_subscription(discord_channel_id=20, lemmy_community_actor_id=GROUP,
            lemmy_community_name='G', lemmy_community_id=None, status='accepted')
        repo = db.discord_fanout_groups
        original = _post_json()
        group = repo.get_or_create_remote_thread_group(community_actor_id=GROUP,
            ap_object_id=POST, source_owner_actor_id=AUTHOR, ap_activity_id=POST+'/create',
            first_object_json=original)
        updated = {**original, 'body_markdown': 'Group replacement'}
        with db.immediate_session() as sess:
            owner = sess.get(CommunityThreadGroup, group.id)
            owner.view_revision = 1
            owner.display_object_json = updated
        reservation = repo.reserve_remote_target(kind='post', group_id=group.id, discord_channel_id=20)
        assert reservation.state == 'reserved'
        assert reservation.view_revision == 1
        assert reservation.display_object_json == updated
        # Even a newer Group Delete cannot be undone by a slow Create ACK.
        with db.immediate_session() as sess:
            owner = sess.get(CommunityThreadGroup, group.id)
            owner.view_revision = 2
            owner.view_state = 'deleted'
        repo.finish_remote_target(kind='post', group_id=group.id, discord_channel_id=20,
            attempt_id=reservation.attempt_id, discord_thread_id=200, discord_object_id=200)
        with db.session() as sess:
            owner = sess.get(CommunityThreadGroup, group.id)
            assert owner.inbound_create_snapshot_json == original
            [claim] = sess.scalars(select(CommunityDiscordTargetClaim)).all()
            assert claim.applied_revision == 1 and owner.view_revision == 2
        assert repo.reserve_remote_target(kind='post', group_id=group.id, discord_channel_id=20).state == 'deleted'
    finally:
        db.close()


def test_group_tombstones_are_per_group_and_ap_id_is_exact(tmp_path: Path) -> None:
    """G's tombstone does not block H's permitted distribution of the same Note."""
    db = Database(f"sqlite:///{tmp_path / 'groups.db'}")
    try:
        db.migrate()
        for channel, group in ((20, GROUP), (21, OTHER)):
            db.remote_subscriptions.create_subscription(discord_channel_id=channel,
                lemmy_community_actor_id=group, lemmy_community_name='G', lemmy_community_id=None, status='accepted')
        r = db.discord_fanout_groups
        g = r.get_or_create_remote_thread_group(community_actor_id=GROUP, ap_object_id=POST,
            source_owner_actor_id=AUTHOR, ap_activity_id=POST+'/create', first_object_json=_post_json())
        h = r.get_or_create_remote_thread_group(community_actor_id=OTHER, ap_object_id=POST,
            source_owner_actor_id=AUTHOR, ap_activity_id=POST+'/create', first_object_json=_post_json())
        with db.immediate_session() as sess:
            sess.get(CommunityThreadGroup, g.id).view_state = 'deleted'
        assert r.reserve_remote_target(kind='post', group_id=g.id, discord_channel_id=20).state == 'deleted'
        assert r.reserve_remote_target(kind='post', group_id=h.id, discord_channel_id=21).state == 'reserved'
        assert r.get_thread_group_by_ap_object(POST, community_actor_id=OTHER).id == h.id
    finally:
        db.close()


def test_community_identity_markers_survive_truncation_and_do_not_match_prefix() -> None:
    """A complete last-line root/footer or first-paragraph marker is evidence."""
    long_text = 'x' * 6000
    root = render_feed_discord_message(header='Title\\nAuthor: `alice`', body=long_text, footer=POST)
    comment = feed_comment_content(actor_name='alice', actor_id=AUTHOR, object_id=POST, body=long_text)
    assert len(root.encode('utf-16-le')) // 2 <= 2000
    assert root.splitlines()[-1] == POST
    assert len(comment.encode('utf-16-le')) // 2 <= 2000
    assert has_feed_comment_identity(comment, POST)
    assert not has_feed_comment_identity(comment, POST+'0')
    with pytest.raises(ValueError):
        render_feed_discord_message(header='H', body='X', footer='https://x.example/'+'x'*2100)


def test_feed_middle_omission_does_not_append_ellipsis_to_raw_url() -> None:
    """The visible URL token must remain byte-for-byte equal to its source."""
    from src.formatting import render_remote_community_root, _feed_protected_spans
    url = 'https://reference.example/story'
    content = render_remote_community_root(
        actor_name='bob', actor_id=AUTHOR, object_id=POST,
        title='Title', body='x' * 2300 + ' ' + url + ' ' + 'y' * 100,
        external_url=None)
    assert len(content.encode('utf-16-le')) // 2 <= 2000
    assert url in content
    assert url + '…' not in content
    assert content.splitlines()[-1] == POST


def test_old_physical_v2_without_trusted_clock_is_refused_read_only(tmp_path: Path) -> None:
    """A v2 label never silently upgrades CP49's ambiguous receive-time column."""
    path = tmp_path / 'old-clock-v2.db'
    snapshot = (Path(__file__).parents[2] / 'src/db/schema_versions/v2.sql').read_text()
    # Model an existing CP49 physical v2: its view_updated_at held a
    # bridge-local receipt time, which cannot be trusted as source ordering.
    old_snapshot = snapshot.replace('\tview_source_updated_at DATETIME,\n', '')
    with sqlite3.connect(path) as conn:
        conn.executescript(old_snapshot)
        conn.execute('PRAGMA user_version=2')
    before = path.read_bytes()
    db = Database(f'sqlite:///{path}')
    try:
        with pytest.raises(RuntimeError, match='physical schema differs'):
            db.migrate()
        assert path.read_bytes() == before
    finally:
        db.close()
