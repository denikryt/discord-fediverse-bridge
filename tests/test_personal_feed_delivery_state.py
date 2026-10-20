"""Personal Feed in-flight revision and uncertain-create SQLite regressions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.db import Database
from src.models import InboundDiscordDelivery, PublishedActivityObject


NOW = datetime(2026, 10, 1, 13, 0, tzinfo=timezone.utc)
ACTOR = "https://remote.example/users/bob"
OBJECT = "https://remote.example/comments/123"
ROOT = "https://bridge.example/objects/root"


def _world(tmp_path):
    """Persist one Personal Feed root and one authorized inbound comment."""
    db = Database(f"sqlite:///{tmp_path / 'personal.db'}")
    db.migrate()
    with db.session() as session:
        session.add(PublishedActivityObject(
            actor_username="alice", actor_url="https://bridge.example/actors/users/alice",
            publication_context="personal_feed", community_actor_url=None,
            activity_id="https://bridge.example/activities/root", object_id=ROOT,
            kind="post", title=None, body_markdown="root", in_reply_to_object_id=None,
            conversation_root_object_id=ROOT, discord_guild_id=1,
            discord_channel_id=10, discord_message_id=100,
        ))
    repo = db.personal_feed_conversations
    admission = repo.admit_create(
        activity_id="https://remote.example/create/1", object_id=OBJECT,
        root_object_id=ROOT, parent_object_id=ROOT,
        activity_actor_id=ACTOR, attributed_to_actor_id=ACTOR,
        actor_name="Bob", content_markdown="v1", published_at=NOW,
        source_activity={"type": "Create"}, destination_channel_id=10,
        parent_known=True,
    )
    assert admission.status == "created"
    return db, repo, admission.delivery_id


def _state(db, delivery_id):
    """Inspect durable delivery state after a separate SQLite read."""
    with db.session() as session:
        return session.get(InboundDiscordDelivery, delivery_id)


def _update(repo, body, offset):
    """Admit a canonical Update with a strictly later remote instant."""
    return repo.admit_update(
        object_id=OBJECT, activity_actor_id=ACTOR, attributed_to_actor_id=ACTOR,
        content_markdown=body, updated_at=NOW + timedelta(seconds=offset),
        source_activity={"type": "Update"},
    )


def _complete(repo, claim, *, success, message_id=None):
    """Complete exactly the immutable claimed revision, not the latest object."""
    return repo.record_discord_attempt(
        claim.delivery_id, success=success, discord_message_id=message_id,
        claimed_revision_fingerprint=claim.claimed_revision_fingerprint,
        claimed_deleted=claim.deleted,
    )


def test_create_lost_response_is_sticky_across_update_delete_and_restart(tmp_path):
    """Once first POST might have succeeded, no later AP mutation may retry it."""
    db, repo, delivery_id = _world(tmp_path)
    claim, = repo.claim_due()
    assert repo.mark_create_started(delivery_id)
    repo.mark_uncertain_create(delivery_id)
    assert _update(repo, "v2", 20).status == "accepted"
    assert repo.admit_delete(object_id=OBJECT, activity_actor_id=ACTOR).status == "accepted"
    repo.suppress_delivery(delivery_id, "inactive binding")
    repo.recover_dispatching()
    row = _state(db, delivery_id)
    assert row.state == "failed"
    assert row.discord_message_id is None
    assert row.next_attempt_at is None
    assert row.last_error == "uncertain Discord create; physical receipt reconciliation required"
    assert repo.claim_due() == []
    db.close()


def test_restart_during_first_create_parks_instead_of_resending(tmp_path):
    """Dispatching with a pre-send marker must not recover to ready."""
    db, repo, delivery_id = _world(tmp_path)
    repo.claim_due()
    repo.mark_create_started(delivery_id)
    repo.recover_dispatching()
    assert _state(db, delivery_id).state == "failed"
    assert repo.claim_due() == []
    db.close()


def test_create_receipt_after_concurrent_delete_schedules_known_id_delete(tmp_path):
    """Late successful first-create receipt must be saved before processing Delete."""
    db, repo, delivery_id = _world(tmp_path)
    claim, = repo.claim_due()
    repo.mark_create_started(delivery_id)
    assert repo.admit_delete(object_id=OBJECT, activity_actor_id=ACTOR).status == "accepted"
    assert repo.claim_due() == []
    assert _complete(repo, claim, success=True, message_id=500) == "ready"
    deletion, = repo.claim_due()
    assert deletion.deleted is True
    assert deletion.discord_message_id == 500
    assert _complete(repo, deletion, success=True) == "delivered"
    db.close()


def test_old_edit_completion_cannot_erase_later_update(tmp_path):
    """v1 edit in flight and v2 Update cannot become falsely delivered as v1."""
    db, repo, delivery_id = _world(tmp_path)
    first, = repo.claim_due()
    repo.mark_create_started(delivery_id)
    assert _complete(repo, first, success=True, message_id=500) == "delivered"
    assert _update(repo, "edit-v1", 10).status == "accepted"
    edit, = repo.claim_due()
    assert edit.discord_message_id == 500
    assert _update(repo, "edit-v2", 20).status == "accepted"
    assert repo.claim_due() == []
    assert _complete(repo, edit, success=True) == "ready"
    latest, = repo.claim_due()
    assert latest.content_markdown == "edit-v2"
    assert latest.discord_message_id == 500
    assert _complete(repo, latest, success=True) == "delivered"
    db.close()


def test_failed_old_edit_does_not_exhaust_new_revision_budget(tmp_path):
    """A late edit error must not consume the new canonical update's retries."""
    db, repo, delivery_id = _world(tmp_path)
    initial, = repo.claim_due()
    repo.mark_create_started(delivery_id)
    _complete(repo, initial, success=True, message_id=500)
    _update(repo, "edit-v1", 10)
    old, = repo.claim_due()
    _update(repo, "edit-v2", 20)
    assert _complete(repo, old, success=False) == "ready"
    assert _state(db, delivery_id).attempt_count == 0
    assert repo.claim_due()[0].content_markdown == "edit-v2"
    db.close()


def test_previous_unknown_create_diagnostic_remains_parked(tmp_path):
    """Legacy parked first sends must not re-enter the delivery queue on mutation."""
    db, repo, delivery_id = _world(tmp_path)
    repo.claim_due()
    repo.mark_create_started(delivery_id)
    repo.mark_uncertain_create(delivery_id)
    # An earlier release persisted a different diagnostic middle phrase.
    with db.session() as session:
        row = session.get(InboundDiscordDelivery, delivery_id)
        row.last_error = "uncertain Discord create; historical reconciliation required"
    assert _update(repo, "v2", 20).status == "accepted"
    assert repo.admit_delete(object_id=OBJECT, activity_actor_id=ACTOR).status == "accepted"
    repo.suppress_delivery(delivery_id, "inactive binding")
    assert repo.claim_due() == []
    row = _state(db, delivery_id)
    assert row.last_error == "uncertain Discord create; historical reconciliation required"
    assert row.discord_message_id is None
    assert row.state == "failed"
    db.close()


def test_definitive_create_failure_can_retry_after_revision_change(tmp_path):
    """Only uncertain outcomes are parked; a definite pre-send failure may retry."""
    db, repo, delivery_id = _world(tmp_path)
    first, = repo.claim_due()
    repo.record_discord_attempt(
        delivery_id,
        success=False,
        error="unavailable",
        claimed_revision_fingerprint=first.claimed_revision_fingerprint,
        claimed_deleted=first.deleted,
    )
    assert _state(db, delivery_id).last_error != "uncertain Discord create; physical receipt reconciliation required"
    assert _update(repo, "v2", 20).status == "accepted"
    row = _state(db, delivery_id)
    assert row.state == "ready"
    assert row.next_attempt_at is not None
    db.close()
