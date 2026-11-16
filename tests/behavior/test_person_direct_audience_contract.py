"""Persistent Person Create audience and original direct-recipient scenarios.

Run without Discord SDK or Hypothesis: ``python -m unittest tests.behavior.test_person_direct_audience_contract``.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from src.content_sync.persistence import PublicationConflictError, persist_publish_artifacts
from src.db import Database
from src.models import User, UserActorOutboundDelivery, UserActorOutboundSource


ACTOR = "https://bridge.example/actors/users/alice"
BOB = "https://remote.example/users/bob"
INBOX = f"{BOB}/inbox"
ROOT = "https://remote.example/notes/root"
OBJECT = f"{ACTOR}/objects/comments/{'a' * 64}"
CREATE = f"{ACTOR}/activities/create/comments/{'a' * 64}"


def activity(*, audience: bool = True) -> dict:
    """Build a canonical renderer response with an optional direct Person."""
    ids = [BOB] if audience else []
    return {
        "type": "Create", "id": CREATE, "actor": ACTOR,
        "to": ["https://www.w3.org/ns/activitystreams#Public"],
        "cc": [ACTOR, f"{ACTOR}/followers", *ids],
        "object": {
            "type": "Note", "id": OBJECT, "attributedTo": ACTOR,
            "inReplyTo": ROOT,
            "to": ["https://www.w3.org/ns/activitystreams#Public"],
            "cc": [f"{ACTOR}/followers", *ids],
        },
    }


class PersonCreateAudienceTests(unittest.TestCase):
    """Verify atomically persisted audience and endpoint proof with real SQLite."""

    def setUp(self) -> None:
        """Create one migrated user database for each scenario."""
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Database(f"sqlite:///{Path(self.temp.name) / 'state.sqlite3'}")
        self.addCleanup(self.database.close)
        self.database.migrate()
        with self.database.session() as session:
            session.add(User(
                discord_user_id="101", activitypub_username="alice", actor_url=ACTOR,
                inbox_url=f"{ACTOR}/inbox", outbox_url=f"{ACTOR}/outbox",
                followers_url=f"{ACTOR}/followers", public_key_pem="public",
                private_key_pem="private",
            ))

    def persist(self, *, recipients=None, rendered=None):
        """Publish the same deterministic local Discord comment into SQLite."""
        return persist_publish_artifacts(
            self.database, source_id="123", actor_username="alice", actor_url=ACTOR,
            community_actor_url=None, activity_id=CREATE, object_id=OBJECT,
            activity_json=activity() if rendered is None else rendered,
            kind="comment", title=None, body_markdown="hello",
            in_reply_to_object_id=ROOT, conversation_root_object_id=ROOT,
            discord_guild_id=1, discord_channel_id=11, discord_message_id=123,
            publication_context="personal_feed",
            published_at=datetime(2026, 11, 16, tzinfo=timezone.utc),
            direct_person_recipients=([{"actorId": BOB, "inboxUrl": INBOX}]
                                      if recipients is None else recipients),
        )

    def test_nonfollower_is_original_audience_and_durable_endpoint(self) -> None:
        """A nonfollower parent's exact identity survives persistence and restart."""
        result = self.persist()
        self.assertTrue(result.created)
        with self.database.session() as session:
            src = session.scalar(select(UserActorOutboundSource).where(UserActorOutboundSource.id == result.source_id))
            delivery = session.scalar(select(UserActorOutboundDelivery).where(UserActorOutboundDelivery.source_id == src.id))
            self.assertEqual(json.loads(src.snapshot_json)["directPersonRecipients"], [{"actorId": BOB, "inboxUrl": INBOX}])
            self.assertEqual(delivery.target_endpoint_url, INBOX)
            self.assertEqual(delivery.status, "pending")
        obj = self.database.activitypub_objects.get_published_activity_object_by_object_id(OBJECT)
        self.assertEqual(self.database.activitypub_objects.get_original_direct_person_actor_ids(obj), [BOB])
        self.assertFalse(self.persist().created)

    def test_unaddressed_direct_target_fails_without_creating_source(self) -> None:
        """A POST endpoint without canonical ActivityStreams audience is rejected."""
        with self.assertRaisesRegex(PublicationConflictError, "audience"):
            self.persist(rendered=activity(audience=False))
        with self.database.session() as session:
            self.assertEqual(session.query(UserActorOutboundSource).count(), 0)

    def test_mismatched_replayed_direct_recipient_fails(self) -> None:
        """A second target cannot replace a committed original recipient proof."""
        self.persist()
        with self.assertRaisesRegex(PublicationConflictError, "incompatible direct Person"):
            self.persist(recipients=[{"actorId": BOB, "inboxUrl": "https://remote.example/another-inbox"}])

    def test_malformed_evidence_is_not_admitted(self) -> None:
        """Direct evidence must be canonical, sorted, and have exact fields."""
        for row in ([{"actorId": BOB}], [{"actorId": BOB, "inboxUrl": "http://remote.example/inbox"}],
                    [{"actorId": BOB, "inboxUrl": INBOX, "untrusted": "x"}]):
            with self.subTest(row=row), self.assertRaises(PublicationConflictError):
                self.persist(recipients=row)


if __name__ == "__main__":
    unittest.main()
