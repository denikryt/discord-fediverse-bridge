"""Direct Person Create/Update/Delete source, claim, and ordering scenarios.

Uses migrated SQLite and real admission/claim repositories; only remote Fedify
HTTP is replaced. It can run via unittest with a minimal discord import stub.
"""

from __future__ import annotations

import json
import importlib.util
import sys
import types
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import select

# Offline behavioral tests stub only Discord's outbound SDK type annotation.
if importlib.util.find_spec("discord") is None:
    discord_stub = types.ModuleType("discord")
    discord_stub.MessageReference = type("MessageReference", (), {})
    sys.modules.setdefault("discord", discord_stub)

from src.content_sync.persistence import persist_publish_artifacts
from src.db import Database
from src.models import User, UserActorOutboundDelivery, UserActorOutboundSource
from tests.behavior.test_person_direct_audience_contract import ACTOR, BOB, INBOX, ROOT, OBJECT, CREATE, activity


def canonical_update(request: dict, activity_id: str) -> dict:
    """Return a signed-gateway-shaped canonical Update from its persisted intent."""
    actors = [r["actorId"] for r in request["directPersonRecipients"]]
    return {
        "type": "Update", "id": activity_id, "actor": ACTOR,
        "to": ["https://www.w3.org/ns/activitystreams#Public"],
        "cc": [ACTOR, f"{ACTOR}/followers", *actors],
        "object": {
            "type": "Note", "id": OBJECT, "attributedTo": ACTOR,
            "inReplyTo": ROOT, "cc": [f"{ACTOR}/followers", *actors],
        },
    }


def canonical_delete(request: dict, activity_id: str) -> dict:
    """Return a signed-gateway-shaped canonical Delete targeting the same Note."""
    actors = [r["actorId"] for r in request["directPersonRecipients"]]
    return {
        "type": "Delete", "id": activity_id, "actor": ACTOR,
        "to": ["https://www.w3.org/ns/activitystreams#Public"],
        "cc": [ACTOR, f"{ACTOR}/followers", *actors],
        "object": OBJECT,
    }


class DirectPersonMutationTests(unittest.TestCase):
    """Replay real source and mutation transactions with a nonfollower target."""

    def setUp(self) -> None:
        """Persist one Person-authored reply to a public remote Note."""
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.database = Database(f"sqlite:///{Path(self.tmp.name) / 'source.db'}")
        self.addCleanup(self.database.close)
        self.database.migrate()
        with self.database.session() as session:
            session.add(User(discord_user_id="101", activitypub_username="alice", actor_url=ACTOR,
                             inbox_url=f"{ACTOR}/inbox", outbox_url=f"{ACTOR}/outbox",
                             followers_url=f"{ACTOR}/followers", public_key_pem="public",
                             private_key_pem="private"))
        self.create = persist_publish_artifacts(
            self.database, source_id="555", actor_username="alice", actor_url=ACTOR,
            publication_context="personal_feed", community_actor_url=None,
            discord_guild_id=1, discord_channel_id=11, discord_message_id=555,
            activity_id=CREATE, object_id=OBJECT, activity_json=activity(),
            kind="comment", title=None, body_markdown="original",
            in_reply_to_object_id=ROOT, conversation_root_object_id=ROOT,
            direct_person_recipients=[{"actorId": BOB, "inboxUrl": INBOX}],
            published_at=datetime(2026, 11, 16, tzinfo=timezone.utc),
        )

    def claims(self):
        """Claim due work through the actual same-writer transaction path."""
        return self.database.user_actor_deliveries.claim_due(
            limit=20, per_user_limit=5, stale_after_seconds=60
        )

    def delivered_create(self):
        """Acknowledge the original actor-addressed Create through its claim CAS."""
        claims = self.claims()
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0].direct_person_routes, ((BOB, INBOX),))
        self.database.user_actor_deliveries.record_outcome(
            claims[0].delivery_id, claims[0].claim_token, ok=True
        )

    def mutate(self, *, delete: bool = False):
        """Reserve/claim and commit canonical mutation without transport I/O."""
        if delete:
            self.assertEqual(self.database.content_mutations.record_desired_delete(object_id=OBJECT), "accepted")
        else:
            self.assertEqual(self.database.content_mutations.record_desired_update(
                object_id=OBJECT, title=None, body_markdown="edited",
                edited_timestamp=datetime(2026, 11, 16, 1, tzinfo=timezone.utc),
            ), "accepted")
        self.database.content_mutations.reserve_next(object_id=OBJECT, target_inbox_urls=None)
        claim = self.database.content_mutations.claim_due(limit=2, stale_after_seconds=60)
        self.assertEqual(len(claim), 1)
        claim = claim[0]
        self.assertEqual(claim.request["directPersonRecipients"], [{"actorId": BOB, "inboxUrl": INBOX}])
        self.assertTrue(self.database.content_mutations.mark_send_possible(claim.mutation_id, claim.claim_token))
        json_activity = (canonical_delete if delete else canonical_update)(claim.request, claim.activity_id)
        self.assertTrue(self.database.content_mutations.record_canonical_result(
            claim.mutation_id, claim.claim_token, json_activity
        ))
        result = self.database.content_mutations.commit_canonical(
            mutation_id=claim.mutation_id, claim_token=claim.claim_token,
            canonical_activity_json=json_activity,
            delivery_repository=self.database.user_actor_deliveries,
        )
        self.assertIsNotNone(result)
        return result

    def test_new_update_request_is_timezone_aware_after_sqlite_restart(self):
        """A local edit yields a Temporal-compatible instant before operation hashing."""
        self.delivered_create()
        from datetime import timedelta
        edit_time = datetime(2026, 11, 16, 3, 0, tzinfo=timezone(timedelta(hours=2)))
        self.assertEqual(self.database.content_mutations.record_desired_update(
            object_id=OBJECT, title=None, body_markdown="edited", edited_timestamp=edit_time,
        ), "accepted")
        self.database.content_mutations.reserve_next(object_id=OBJECT, target_inbox_urls=None)
        [claim] = self.database.content_mutations.claim_due(limit=2, stale_after_seconds=60)
        instant = datetime.fromisoformat(claim.request["mutationTimestamp"])
        self.assertIsNotNone(instant.utcoffset())
        self.assertEqual(instant.astimezone(timezone.utc), datetime(2026, 11, 16, 1, tzinfo=timezone.utc))
        # Reconstruct the same durable claim without a second reservation.
        with self.database.session() as session:
            from src.models import UserActorContentMutation
            row = session.get(UserActorContentMutation, claim.mutation_id)
            self.assertEqual(json.loads(row.request_json)["mutationTimestamp"], claim.request["mutationTimestamp"])
            self.assertEqual(row.operation_id, claim.operation_id)

    def test_delete_without_edit_timestamp_keeps_nullable_gateway_field(self):
        """Delete of an unchanged Note needs no synthetic Update instant."""
        self.delivered_create()
        self.assertEqual(self.database.content_mutations.record_desired_delete(object_id=OBJECT), "accepted")
        self.database.content_mutations.reserve_next(object_id=OBJECT, target_inbox_urls=None)
        [claim] = self.database.content_mutations.claim_due(limit=2, stale_after_seconds=60)
        self.assertIsNone(claim.request["mutationTimestamp"])

    def test_cannot_mutate_a_direct_reply_before_original_create_is_delivered(self):
        """Parent author must never receive Update before acknowledging Create."""
        self.assertEqual(self.database.content_mutations.record_desired_update(
            object_id=OBJECT, title=None, body_markdown="edited",
            edited_timestamp=datetime(2026, 11, 16, 1, tzinfo=timezone.utc),
        ), "accepted")
        self.database.content_mutations.reserve_next(object_id=OBJECT, target_inbox_urls=None)
        self.assertEqual(self.database.content_mutations.claim_due(limit=5, stale_after_seconds=60), [])
        # Once the Create is proven delivered the same reservation becomes available.
        self.delivered_create()
        self.assertEqual(len(self.database.content_mutations.claim_due(limit=5, stale_after_seconds=60)), 1)

    def test_delivered_create_followed_by_update_then_delete_targets_original_person(self):
        """Every canonical mutation inherits actor, inbox, and object after restart."""
        self.delivered_create()
        update_src_id, obj = self.mutate()
        self.assertEqual(obj, OBJECT)
        with self.database.session() as session:
            update_src = session.get(UserActorOutboundSource, update_src_id)
            self.assertEqual(json.loads(update_src.snapshot_json)["directPersonRecipients"], [{"actorId": BOB, "inboxUrl": INBOX}])
        update_claims = self.claims()
        self.assertEqual(len(update_claims), 1)
        self.assertEqual(update_claims[0].target_endpoint_url, INBOX)
        self.assertEqual(update_claims[0].direct_person_routes, ((BOB, INBOX),))
        self.database.user_actor_deliveries.record_outcome(update_claims[0].delivery_id, update_claims[0].claim_token, ok=True)
        delete_src_id, _ = self.mutate(delete=True)
        self.assertGreater(delete_src_id, update_src_id)
        delete_claims = self.claims()
        self.assertEqual(len(delete_claims), 1)
        self.assertEqual(delete_claims[0].direct_person_routes, ((BOB, INBOX),))
        self.assertEqual(delete_claims[0].activity_json["object"], OBJECT)

    def test_restart_does_not_blind_retry_ambiguous_direct_create(self):
        """A stale sending attempt may have reached Bob and becomes unknown."""
        claims = self.claims()
        self.assertEqual(len(claims), 1)
        with self.database.session() as session:
            delivery = session.get(UserActorOutboundDelivery, claims[0].delivery_id)
            delivery.claimed_at = datetime.now(timezone.utc) - timedelta(minutes=10)
        self.assertEqual(self.claims(), [])
        with self.database.session() as session:
            delivery = session.get(UserActorOutboundDelivery, claims[0].delivery_id)
            self.assertEqual(delivery.status, "unknown")
            self.assertIsNone(delivery.next_attempt_at)
        self.assertEqual(self.claims(), [])

    def test_gateway_ambiguous_outcome_is_terminal_until_reconciliation(self):
        """No retry duplicates a signed nonfollower Create after an HTTP timeout."""
        claim = self.claims()[0]
        self.database.user_actor_deliveries.record_unresolved(
            claim.delivery_id, claim.claim_token, "transport timeout"
        )
        self.assertEqual(self.claims(), [])
        with self.database.session() as session:
            delivery = session.get(UserActorOutboundDelivery, claim.delivery_id)
            self.assertEqual(delivery.status, "unknown")

    def test_forged_direct_target_snapshot_is_rejected_at_claim(self):
        """A second endpoint cannot be authorized by only rewriting saved JSON."""
        with self.database.session() as session:
            src = session.get(UserActorOutboundSource, self.create.source_id)
            snapshot = json.loads(src.snapshot_json)
            snapshot["directPersonRecipients"] = [{"actorId": "https://remote.example/users/mallory", "inboxUrl": INBOX}]
            src.snapshot_json = json.dumps(snapshot)
        self.assertEqual(self.claims(), [])
        with self.database.session() as session:
            self.assertEqual(session.scalar(select(UserActorOutboundDelivery.status)), "ineligible")


if __name__ == "__main__":
    unittest.main()
