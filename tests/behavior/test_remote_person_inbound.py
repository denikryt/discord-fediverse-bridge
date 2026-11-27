"""Remote Person public Note reservation through a real v2 SQLite database."""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select

from src.activitypub_models import RemotePersonContentEvent
from src.db import Database
from src.models import RemotePersonChannelAssignment, RemotePersonFollow, RemotePersonPost, RemotePersonPostDelivery, User
from src.remote_person_inbound import PUBLIC, RemotePersonInboundService

ACTOR = 'https://remote.example/users/bob'
OBJECT = 'https://remote.example/users/bob/statuses/7'


class RemotePersonInboundScenarios(unittest.TestCase):
    """Test subscriber dedup, actor authenticity, and source lifecycle."""

    def setUp(self):
        """Provision two accepted users mapping to the same and distinct channels."""
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db = Database(f'sqlite:///{Path(temp.name) / "feed.sqlite"}')
        self.addCleanup(self.db.close)
        self.db.migrate()
        self.service = RemotePersonInboundService(self.db)
        self.when = datetime(2026, 11, 16, 7, 0, tzinfo=timezone.utc)
        with self.db.session() as session:
            for n, username in enumerate(('alice', 'charlie')):
                actor_url = f'https://bridge.test/actors/users/{username}'
                user = User(discord_user_id=username, activitypub_username=username,
                    actor_url=actor_url, inbox_url=f'{actor_url}/inbox', outbox_url=f'{actor_url}/outbox',
                    followers_url=f'{actor_url}/followers', public_key_pem='pub', private_key_pem='priv')
                session.add(user)
                session.flush()
                follow = RemotePersonFollow(user_id=user.id, remote_actor_id=ACTOR,
                    remote_inbox_url=f'{ACTOR}/inbox', follow_activity_id=f'{actor_url}/activities/follow/{n}',
                    generation=1, relationship_status='accepted', follow_dispatch_state='sent')
                session.add(follow)
                session.flush()
                for cid in ((100, 101) if n == 0 else (100,)):
                    session.add(RemotePersonChannelAssignment(follow_id=follow.id,
                        discord_channel_id=cid, discord_guild_id=(1 if cid == 100 else 2)))

    def event(self, operation='created', *, actor=ACTOR, attribution=ACTOR,
              public=True, object_id=OBJECT, when=None):
        """Create one signed-looking gateway record for Python authorization tests."""
        event_type = {'created': 'Create', 'updated': 'Update', 'deleted': 'Delete'}[operation]
        note = {'id': object_id, 'type': 'Note', 'attributedTo': attribution,
            'content': 'Updated text' if operation == 'updated' else 'Initial text',
            'to': [PUBLIC] if public else [f'{ACTOR}/followers'],
            'published': self.when.isoformat()}
        source = {'id': f'https://remote.example/activities/{operation}-{int((when or self.when).timestamp())}',
            'type': event_type, 'actor': actor,
            'to': [PUBLIC] if public else [f'{ACTOR}/followers'],
            'object': object_id if operation == 'deleted' else note}
        return RemotePersonContentEvent(event_type=f'remote.person.{operation}',
            delivery_id=source['id'], occurred_at=when or self.when,
            actor_id=actor, verified_sender_actor_id=actor,
            source_activity_id=source['id'], source_activity_json=source,
            object={'ap_id': object_id, 'body_markdown': note['content'],
                    'published_at': self.when, 'author_name': 'bob'})

    def test_create_reserves_once_per_distinct_channel_and_duplicate_is_inert(self):
        """Two accepted local followers into X create one X job, plus independent Y."""
        event = self.event()
        self.assertEqual(self.service.reserve(event).status, 'reserved')
        self.assertEqual(self.service.reserve(event).status, 'duplicate')
        with self.db.session() as session:
            self.assertEqual(session.scalar(select(RemotePersonPost.id).where(
                RemotePersonPost.object_id == OBJECT)), 1)
            jobs = session.scalars(select(RemotePersonPostDelivery)).all()
            self.assertEqual({x.discord_channel_id for x in jobs}, {100, 101})
            self.assertEqual(len(jobs), 2)

    def test_private_spoofed_and_wrong_author_do_not_reserve(self):
        """Public audience and exact signed actor->attribution are necessary."""
        for event in (self.event(public=False), self.event(attribution='https://evil.example/user'),
                      self.event(actor='https://evil.example/user')):
            self.assertEqual(self.service.reserve(event).status, 'invalid_identity_or_audience')
        with self.db.session() as session:
            self.assertIsNone(session.scalar(select(RemotePersonPost.id)))

    def test_no_accepted_follow_or_disallowed_guild_never_reserves(self):
        """A pending Follow or denied destination cannot authorize fanout."""
        with self.db.immediate_session() as session:
            for follow in session.scalars(select(RemotePersonFollow)):
                follow.relationship_status = 'pending'
        self.assertEqual(self.service.reserve(self.event()).status, 'no_accepted_assignment')
        with self.db.immediate_session() as session:
            for follow in session.scalars(select(RemotePersonFollow)):
                follow.relationship_status = 'accepted'
        self.assertEqual(self.service.reserve(self.event(), guild_allowed=lambda _guild: False).status,
            'no_accepted_assignment')
        with self.db.session() as session:
            self.assertIsNone(session.scalar(select(RemotePersonPost.id)))

    def test_authenticated_update_cannot_rewind_and_delete_suppresses_pending(self):
        """Mutations keep canonical object identity and cannot revive pending jobs."""
        self.service.reserve(self.event())
        newer = self.event('updated', when=self.when + timedelta(minutes=1))
        self.assertEqual(self.service.reserve(newer).status, 'updated')
        self.assertEqual(self.service.reserve(self.event('updated')).status, 'stale')
        self.assertEqual(self.service.reserve(self.event('deleted')).status, 'deleted')
        self.assertEqual(self.service.reserve(self.event()).status, 'deleted')
        with self.db.session() as session:
            post = session.scalar(select(RemotePersonPost))
            self.assertEqual(post.content_markdown, 'Updated text')
            self.assertIsNotNone(post.deleted_at)
            self.assertEqual({d.state for d in session.scalars(select(RemotePersonPostDelivery))}, {'suppressed'})

    def test_delete_before_create_is_a_durable_tombstone(self):
        """An authenticated out-of-order Delete must not spawn a Discord root."""
        self.assertEqual(self.service.reserve(self.event('deleted')).status, 'deleted')
        self.assertEqual(self.service.reserve(self.event()).status, 'deleted')
        with self.db.session() as session:
            self.assertIsNotNone(session.scalar(select(RemotePersonPost.deleted_at)))
            self.assertEqual(session.scalars(select(RemotePersonPostDelivery)).all(), [])

    def test_timezone_offset_is_persisted_as_utc_and_ordered_after_restart(self):
        """A later true instant wins despite SQLite losing timezone offsets."""
        self.when = datetime(2026, 11, 16, 5, tzinfo=timezone.utc)
        self.assertEqual(self.service.reserve(self.event()).status, 'reserved')
        first = datetime(2026, 11, 16, 8, 10, tzinfo=timezone(timedelta(hours=2)))
        later = datetime(2026, 11, 16, 6, 20, tzinfo=timezone.utc)
        earlier = datetime(2026, 11, 16, 6, 15, tzinfo=timezone.utc)
        self.assertEqual(self.service.reserve(self.event('updated', when=first)).status, 'updated')
        # Each service invocation opens a new SQLite session (equivalent to restart).
        self.assertEqual(self.service.reserve(self.event('updated', when=later)).status, 'updated')
        self.assertEqual(self.service.reserve(self.event('updated', when=earlier)).status, 'stale')
        with self.db.session() as session:
            self.assertEqual(session.scalar(select(RemotePersonPost.updated_at_remote)),
                             later.replace(tzinfo=None))

    def test_a_precreate_comment_tombstone_also_blocks_a_later_root_from_same_actor(self):
        """Object-only Delete has no Note parent and must protect both shapes."""
        from src.remote_person_comments import RemotePersonCommentService
        from src.activitypub_models import RemotePersonCommentEvent
        removed = self.event('deleted')
        comment_delete = RemotePersonCommentEvent(
            event_type='remote.person.comment.deleted',
            delivery_id=removed.delivery_id, occurred_at=removed.occurred_at,
            actor_id=removed.actor_id, verified_sender_actor_id=removed.actor_id,
            source_activity_id=removed.source_activity_id,
            source_activity_json=removed.source_activity_json,
            object={'ap_id': OBJECT, 'parent_ap_id': None, 'author_name': 'bob'},
        )
        self.assertEqual(RemotePersonCommentService(self.db).reserve(comment_delete).status,
                         'deleted_before_create')
        self.assertEqual(self.service.reserve(self.event()).status, 'deleted')
        with self.db.session() as session:
            self.assertEqual(session.scalars(select(RemotePersonPostDelivery)).all(), [])


    def test_foreign_provisional_delete_cannot_suppress_real_root_author(self):
        """A signed Delete by a different actor cannot claim someone else's Note."""
        from src.remote_person_comments import RemotePersonCommentService
        from src.activitypub_models import RemotePersonCommentEvent
        removed = self.event('deleted', actor='https://evil.example/users/mallory')
        provisional = RemotePersonCommentEvent(
            event_type='remote.person.comment.deleted',
            delivery_id=removed.delivery_id, occurred_at=removed.occurred_at,
            actor_id=removed.actor_id, verified_sender_actor_id=removed.actor_id,
            source_activity_id=removed.source_activity_id,
            source_activity_json=removed.source_activity_json,
            object={'ap_id': OBJECT, 'parent_ap_id': None, 'author_name': 'mallory'},
        )
        self.assertEqual(RemotePersonCommentService(self.db).reserve(provisional).status,
                         'deleted_before_create')
        self.assertEqual(self.service.reserve(self.event()).status, 'reserved')
        with self.db.session() as session:
            self.assertEqual(len(session.scalars(select(RemotePersonPostDelivery)).all()), 2)


    def test_unknown_update_never_creates_root(self):
        """A standalone Update cannot invent a previously unseen publication."""
        self.assertEqual(self.service.reserve(self.event('updated')).status, 'unknown_object')
        with self.db.session() as session:
            self.assertIsNone(session.scalar(select(RemotePersonPost.id)))


if __name__ == '__main__':
    unittest.main()
