"""Read original remote-person comment audience from durable SQLite provenance."""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from src.db import Database
from src.models import PublishedActivityObject, User, UserActorOutboundSource

ACTOR = 'https://bridge.example/actors/users/alice'
BOB = 'https://remote.example/users/bob'
ROOT = 'https://remote.example/users/bob/statuses/1'


class RemotePersonPublicReadScenarios(unittest.TestCase):
    """Ensure public reads never reconstruct Person recipients from current follows."""

    def setUp(self):
        """Create one local canonical comment and source from the SQLite v2 model."""
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db = Database(f'sqlite:///{Path(temp.name) / "feed.sqlite"}')
        self.addCleanup(self.db.close)
        self.db.migrate()
        with self.db.immediate_session() as session:
            user = User(discord_user_id='11', activitypub_username='alice', actor_url=ACTOR,
                inbox_url=f'{ACTOR}/inbox', outbox_url=f'{ACTOR}/outbox',
                followers_url=f'{ACTOR}/followers', public_key_pem='pub', private_key_pem='priv')
            session.add(user)
            session.flush()
            root = PublishedActivityObject(actor_username='alice', actor_url=ACTOR,
                publication_context='remote_person_feed', community_actor_url=None,
                activity_id=f'{ACTOR}/activities/create/comments/123',
                object_id=f'{ACTOR}/objects/comments/123', kind='comment', title=None,
                body_markdown='Hello', in_reply_to_object_id=ROOT,
                conversation_root_object_id=ROOT, discord_guild_id=1,
                discord_channel_id=100, discord_message_id=123,
                published_at=datetime(2026, 11, 16, tzinfo=timezone.utc))
            session.add(root)
            session.flush()
            self.object_id = root.object_id
            self.activity_id = root.activity_id
            self.source_id = None
            activity = {
                'type': 'Create', 'id': self.activity_id, 'actor': ACTOR,
                'to': ['https://www.w3.org/ns/activitystreams#Public'],
                'cc': [ACTOR, f'{ACTOR}/followers', BOB],
                'object': {'type': 'Note', 'id': self.object_id,
                    'attributedTo': ACTOR, 'inReplyTo': ROOT,
                    'to': ['https://www.w3.org/ns/activitystreams#Public'],
                    'cc': [f'{ACTOR}/followers', BOB]},
            }
            source = UserActorOutboundSource(user_id=user.id, source_kind='content_create',
                ordering_key=self.object_id, logical_version=1,
                activity_id=self.activity_id, published_object_id=root.id,
                snapshot_json=json.dumps({'directPersonRecipients': [
                    {'actorId': BOB, 'inboxUrl': f'{BOB}/inbox'},
                ]}), activity_json=json.dumps(activity), state='ready')
            session.add(source)
            session.flush()
            self.source_id = source.id

    def test_original_audience_and_person_note_are_available_after_restart(self):
        """An accepted Follow is never required for public canonical dereference."""
        row = self.db.activitypub_objects.get_published_activity_object_by_object_id(self.object_id)
        self.assertEqual(row.publication_context, 'remote_person_feed')
        self.assertEqual(row.in_reply_to_object_id, ROOT)
        self.assertEqual(self.db.activitypub_objects.get_original_create_object_shape(row), ('Note', None))
        self.assertEqual(self.db.activitypub_objects.get_original_direct_person_actor_ids(row), [BOB])

    def test_tampered_source_audience_is_not_silently_published(self):
        """A mismatch between claimed recipient and original Create blocks a read."""
        with self.db.immediate_session() as session:
            source = session.get(UserActorOutboundSource, self.source_id)
            payload = json.loads(source.activity_json)
            payload['object']['cc'] = [f'{ACTOR}/followers']
            source.activity_json = json.dumps(payload)
        row = self.db.activitypub_objects.get_published_activity_object_by_object_id(self.object_id)
        with self.assertRaisesRegex(ValueError, 'direct Person audience conflicts'):
            self.db.activitypub_objects.get_original_direct_person_actor_ids(row)


if __name__ == '__main__':
    unittest.main()
