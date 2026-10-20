"""Durable comment provenance rejects changed ActivityPub Create identities."""

from __future__ import annotations

from datetime import datetime, timezone
from src.db import Database


def _args():
    """One verified canonical comment event and its stored source payload."""
    return dict(activity_id='https://remote.test/create/1',
                object_id='https://remote.test/comment/1',
                root_object_id='https://remote.test/post/1',
                parent_object_id='https://remote.test/comment/parent',
                actor_id='https://remote.test/u/bob', actor_name='bob',
                content_markdown='Hello', published_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
                source_activity={'id':'https://remote.test/create/1', 'type':'Create',
                                 'actor':'https://remote.test/u/bob',
                                 'object': {'id':'https://remote.test/comment/1','content':'Hello'}})


def test_community_create_duplicate_requires_identical_activity_parent_and_body(tmp_path):
    """Spoofing canonical content on an already persisted ID cannot admit fanout."""
    db = Database(f'sqlite:///{tmp_path / "provenance.db"}')
    db.migrate()
    kwargs = _args()
    first = db.personal_feed_conversations.retain_community_create(**kwargs)
    assert first.status == 'created'
    assert db.personal_feed_conversations.retain_community_create(**kwargs).status == 'duplicate'
    for change in ({'activity_id': 'https://spoof.test/create'},
                   {'parent_object_id': 'https://spoof.test/parent'},
                   {'root_object_id': 'https://spoof.test/post'},
                   {'content_markdown': 'Forged'},
                   {'source_activity': {'id':'https://spoof.test/create','type':'Create'}},
                   {'actor_id': 'https://spoof.test/u/other'}):
        assert db.personal_feed_conversations.retain_community_create(
            **(kwargs | change)).status in ('identity_conflict', 'actor_mismatch')
    db.close()


def test_unknown_historical_comment_source_cannot_be_reclaimed_by_new_event(tmp_path):
    """A legacy row with no verified source cannot attest the next sender."""
    db = Database(f'sqlite:///{tmp_path / "provenance.db"}')
    db.migrate()
    kwargs = _args()
    db.personal_feed_conversations.retain_community_create(**(kwargs | {'source_activity': None}))
    assert db.personal_feed_conversations.retain_community_create(**kwargs).status == 'unverified_duplicate'
    db.close()
