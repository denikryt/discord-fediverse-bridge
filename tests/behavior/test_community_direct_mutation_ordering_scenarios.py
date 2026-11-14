"""Discord edits and deletions must inherit Create's immutable direct Group route.

Tests exercise the actual SQLite mutation and User Actor delivery repositories;
only the external gateway and Discord transports are kept outside the test.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from src.db import Database
from src.models import (
    BridgeActorFollow,
    ChannelCommunitySubscription,
    UserActorContentMutation,
    UserActorOutboundDelivery,
    UserActorOutboundSource,
    PublishedActivityObject,
    User,
)

ACTOR = "https://bridge.example/actors/users/alice"
GROUP = "https://nodebb.example/category/42"
INBOX = "https://nodebb.example/category/42/inbox"
FOLLOW = "https://bridge.example/actors/services/bridge/activities/follow/f42"
OBJECT = f"{ACTOR}/objects/posts/seed"
CREATE = f"{ACTOR}/activities/create/posts/seed"
RECIPIENT = {
    "actorId": GROUP,
    "inboxUrl": INBOX,
    "followActivityId": FOLLOW,
    "discordChannelId": 100,
    "discordGuildId": 200,
    "softwareProfile": "nodebb",
}


def _published_group(tmp_path):
    """Create the exact committed Discord-originated Create/Follow graph."""
    db = Database(f"sqlite:///{tmp_path / 'direct-mutation.sqlite'}")
    db.create_all()
    db.users.create_user(
        discord_user_id="10", activitypub_username="alice", actor_url=ACTOR,
        inbox_url=f"{ACTOR}/inbox", outbox_url=f"{ACTOR}/outbox",
        followers_url=f"{ACTOR}/followers", public_key_pem="pub", private_key_pem="private",
    )
    with db.immediate_session() as session:
        session.add(BridgeActorFollow(
            community_actor_id=GROUP, community_inbox_url=INBOX,
            follow_activity_id=FOLLOW, remote_software_profile="nodebb",
            follow_dispatch_state="sent", cancel_requested=False, status="accepted",
        ))
        session.add(ChannelCommunitySubscription(
            discord_channel_id=100, discord_guild_id=200,
            lemmy_community_actor_id=GROUP, community_inbox_url=INBOX,
            follow_activity_id=FOLLOW, status="accepted",
        ))
    with db.immediate_session() as session:
        published = PublishedActivityObject(
            actor_username="alice", actor_url=ACTOR,
            publication_context="community", community_actor_url=GROUP,
            activity_id=CREATE, object_id=OBJECT,
            kind="post", title="Original", body_markdown="Original body",
            conversation_root_object_id=OBJECT,
            discord_guild_id=200, discord_channel_id=100,
            discord_message_id=500, content_revision=1,
        )
        session.add(published)
        session.flush()
        user = session.scalar(select(User))
        create = db.user_actor_deliveries.insert_ready_content_source(
            session, user_id=user.id, source_kind="content_create", logical_version=1,
            activity_id=CREATE, published_object_id=published.id, object_id=OBJECT,
            activity_json={"type": "Create", "id": CREATE, "actor": ACTOR,
                           "object": {"id": OBJECT, "type": "Note"}},
            snapshot={"schemaVersion": 1, "objectId": OBJECT,
                      "activityType": "Create", "contentRevision": 1,
                      "directGroupRecipients": [RECIPIENT]},
            followers=[], direct_group_recipients=[RECIPIENT],
        )
        source_id = create.id
    return db, source_id


def _admit_mutation(db, operation):
    """Persist the Discord edit/delete before the gateway is involved."""
    if operation == "update":
        outcome = db.content_mutations.record_desired_update(
            object_id=OBJECT, title="Renamed", body_markdown="Updated",
            edited_timestamp=datetime(2026, 11, 12, tzinfo=timezone.utc),
        )
    else:
        outcome = db.content_mutations.record_desired_delete(object_id=OBJECT)
    assert outcome == "accepted"
    return db.content_mutations.reserve_next(object_id=OBJECT, target_inbox_urls=None)


def _confirm_create(db):
    """Represent a proven Fedify delivery result, not an uncertain timeout."""
    with db.immediate_session() as session:
        source = session.scalar(select(UserActorOutboundSource).where(
            UserActorOutboundSource.source_kind == "content_create"))
        delivery = session.scalar(select(UserActorOutboundDelivery).where(
            UserActorOutboundDelivery.source_id == source.id,
            UserActorOutboundDelivery.target_endpoint_url == INBOX,
        ))
        assert delivery is not None
        delivery.status = "delivered"


@pytest.mark.parametrize("operation", ["update", "delete"])
def test_mutation_inherits_exact_group_target_and_waits_for_confirmed_create(tmp_path, operation):
    """Queued or unknown Create must not be overtaken by Update nor Delete."""
    db, _ = _published_group(tmp_path)
    reservation_id = _admit_mutation(db, operation)
    with db.session() as session:
        mutation = session.get(UserActorContentMutation, reservation_id)
        assert json.loads(mutation.request_json)["directGroupRecipients"] == [RECIPIENT]
    # No rendering or delivery may begin while Create is still pending.
    assert db.content_mutations.claim_due(limit=10, stale_after_seconds=300) == []
    with db.immediate_session() as session:
        delivery = session.scalar(select(UserActorOutboundDelivery))
        delivery.status = "unknown"
    assert db.content_mutations.claim_due(limit=10, stale_after_seconds=300) == []

    _confirm_create(db)
    [claim] = db.content_mutations.claim_due(limit=10, stale_after_seconds=300)
    assert claim.request["directGroupRecipients"] == [RECIPIENT]
    canonical = {"id": claim.activity_id, "type": operation.title(), "actor": ACTOR,
                 "object": OBJECT if operation == "delete" else {"id": OBJECT}}
    assert db.content_mutations.record_canonical_result(claim.mutation_id, claim.claim_token, canonical)
    committed = db.content_mutations.commit_canonical(
        mutation_id=claim.mutation_id, claim_token=claim.claim_token,
        canonical_activity_json=canonical, delivery_repository=db.user_actor_deliveries,
    )
    assert committed is not None
    with db.session() as session:
        source = session.get(UserActorOutboundSource, committed[0])
        assert json.loads(source.snapshot_json)["directGroupRecipients"] == [RECIPIENT]
        target = session.scalar(select(UserActorOutboundDelivery).where(
            UserActorOutboundDelivery.source_id == source.id))
        assert target.target_endpoint_url == INBOX
    [delivery_claim] = db.user_actor_deliveries.claim_due(
        limit=10, per_user_limit=10, stale_after_seconds=300)
    assert delivery_claim.source_id == committed[0]
    assert delivery_claim.direct_group_routes == ((GROUP, INBOX),)
    db.close()


def test_mutation_never_becomes_follower_authorized_after_follow_replacement(tmp_path):
    """A changed Follow generation cannot authorize the original Group mutation."""
    db, _ = _published_group(tmp_path)
    _confirm_create(db)
    _admit_mutation(db, "update")
    [claim] = db.content_mutations.claim_due(limit=10, stale_after_seconds=300)
    assert db.content_mutations.record_canonical_result(
        claim.mutation_id, claim.claim_token,
        {"id": claim.activity_id, "type": "Update", "actor": ACTOR, "object": {"id": OBJECT}},
    )
    committed = db.content_mutations.commit_canonical(
        mutation_id=claim.mutation_id, claim_token=claim.claim_token,
        canonical_activity_json={"id": claim.activity_id, "type": "Update", "actor": ACTOR, "object": {"id": OBJECT}},
        delivery_repository=db.user_actor_deliveries,
    )
    with db.immediate_session() as session:
        session.scalar(select(BridgeActorFollow)).follow_activity_id = "https://bridge.example/new-follow"
        session.scalar(select(ChannelCommunitySubscription)).follow_activity_id = "https://bridge.example/new-follow"
    assert db.user_actor_deliveries.claim_due(
        limit=10, per_user_limit=10, stale_after_seconds=300) == []
    with db.session() as session:
        delivery = session.scalar(select(UserActorOutboundDelivery).where(
            UserActorOutboundDelivery.source_id == committed[0]))
        assert delivery.status == "ineligible"
    db.close()

@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["update", "delete"])
async def test_mutation_worker_renders_only_after_create_and_commits_before_delivery(tmp_path, operation):
    """Discord edit/delete triggers gateway rendering, never pre-commit network delivery."""
    from types import SimpleNamespace
    from src.user_actor_delivery import UserActorDeliveryService

    db, _ = _published_group(tmp_path)
    _admit_mutation(db, operation)

    class Renderer:
        """A render-only test gateway that rejects any missing safety flag."""

        def __init__(self):
            """Collect requests that cross the external renderer boundary."""
            self.requests = []

        async def update_content(self, request):
            """Render an Update from the immutable original Group evidence."""
            self.requests.append(request)
            assert request.render_only and request.software_profile == "nodebb"
            assert request.conversation_root_object_id == OBJECT
            return SimpleNamespace(activity_id=request.activity_id, object_id=OBJECT,
                activity_json={"id": request.activity_id, "type": "Update", "actor": ACTOR,
                               "to": [GROUP], "object": {"id": OBJECT, "type": "Note",
                               "attributedTo": ACTOR, "audience": GROUP,
                               "to": [GROUP], "context": OBJECT}})

        async def delete_content(self, request):
            """Render a Delete without invoking the remote inbox."""
            self.requests.append(request)
            assert request.render_only and request.software_profile == "nodebb"
            return SimpleNamespace(activity_id=request.activity_id, object_id=OBJECT,
                activity_json={"id": request.activity_id, "type": "Delete", "actor": ACTOR,
                               "to": [GROUP], "object": OBJECT})

    gateway = Renderer()
    service = UserActorDeliveryService(
        db, gateway, SimpleNamespace(federation_decision=lambda _:
            SimpleNamespace(allowed=True)),
    )
    assert await service.drain_mutations(limit=10) == 0
    assert gateway.requests == []
    _confirm_create(db)
    assert await service.drain_mutations(limit=10) == 1
    assert len(gateway.requests) == 1
    with db.session() as session:
        mutation = session.scalar(select(UserActorContentMutation))
        assert mutation.state == "committed"
        source = session.scalar(select(UserActorOutboundSource).where(
            UserActorOutboundSource.source_kind == f"content_{operation}"))
        assert source is not None
        assert json.loads(source.snapshot_json)["directGroupRecipients"] == [RECIPIENT]
    db.close()


def test_corrupt_original_group_provenance_is_rejected_before_reservation(tmp_path):
    """A historical/inconsistent snapshot cannot become a guessed current Follow."""
    db, source_id = _published_group(tmp_path)
    with db.immediate_session() as session:
        source = session.get(UserActorOutboundSource, source_id)
        snapshot = json.loads(source.snapshot_json)
        snapshot["directGroupRecipients"][0]["actorId"] = "https://foreign.example/category/1"
        source.snapshot_json = json.dumps(snapshot)
    assert db.content_mutations.record_desired_update(
        object_id=OBJECT, title="Changed", body_markdown="edited",
        edited_timestamp=datetime(2026, 11, 12, tzinfo=timezone.utc),
    ) == "accepted"
    with pytest.raises(ValueError, match="recipient contradicts Create"):
        db.content_mutations.reserve_next(object_id=OBJECT, target_inbox_urls=None)
    with db.session() as session:
        assert session.scalar(select(UserActorContentMutation)) is None
    db.close()


def test_legacy_remote_create_without_original_route_never_guesses_new_target(tmp_path):
    """An unproven old remote source cannot inherit a newly accepted Follow."""
    db, source_id = _published_group(tmp_path)
    with db.immediate_session() as session:
        source = session.get(UserActorOutboundSource, source_id)
        source.snapshot_json = json.dumps({"schemaVersion": 1, "objectId": OBJECT})
    assert db.content_mutations.record_desired_delete(object_id=OBJECT) == "accepted"
    with pytest.raises(ValueError, match="no provable original Group recipient"):
        db.content_mutations.reserve_next(object_id=OBJECT, target_inbox_urls=None)
    with db.session() as session:
        assert session.scalar(select(UserActorContentMutation)) is None
    db.close()


def _commit_reserved_mutation(db, operation):
    """Render a deterministic successful ActivityPub mutation after a DB claim."""
    [claim] = db.content_mutations.claim_due(limit=10, stale_after_seconds=300)
    canonical = {
        "id": claim.activity_id, "type": operation.title(), "actor": ACTOR,
        "to": [GROUP],
        "object": OBJECT if operation == "delete" else {
            "id": OBJECT, "type": "Note", "attributedTo": ACTOR,
            "audience": GROUP, "to": [GROUP], "context": OBJECT,
        },
    }
    assert db.content_mutations.record_canonical_result(claim.mutation_id, claim.claim_token, canonical)
    result = db.content_mutations.commit_canonical(
        mutation_id=claim.mutation_id, claim_token=claim.claim_token,
        canonical_activity_json=canonical, delivery_repository=db.user_actor_deliveries,
    )
    assert result is not None
    return result[0]


def test_direct_mutation_revisions_never_overtake_unknown_prior_mutation(tmp_path):
    """Even Delete stays behind an unresolved earlier Update for one Group endpoint."""
    db, _ = _published_group(tmp_path)
    _confirm_create(db)
    _admit_mutation(db, "update")
    update_id = _commit_reserved_mutation(db, "update")
    _admit_mutation(db, "delete")
    delete_id = _commit_reserved_mutation(db, "delete")
    [first] = db.user_actor_deliveries.claim_due(
        limit=10, per_user_limit=10, stale_after_seconds=300)
    assert first.source_id == update_id
    # A send could have succeeded but its HTTP response was lost.
    with db.immediate_session() as session:
        source = session.get(UserActorOutboundSource, update_id)
        delivery = session.scalar(select(UserActorOutboundDelivery).where(
            UserActorOutboundDelivery.source_id == source.id))
        delivery.status = "unknown"
    assert db.user_actor_deliveries.claim_due(
        limit=10, per_user_limit=10, stale_after_seconds=300) == []
    with db.session() as session:
        delete_delivery = session.scalar(select(UserActorOutboundDelivery).where(
            UserActorOutboundDelivery.source_id == delete_id))
        assert delete_delivery.status == "pending"
    db.close()


def test_restart_reuses_durable_original_group_target_in_mutation(tmp_path):
    """A fresh worker process reads the initial Create provenance from SQLite."""
    db, _ = _published_group(tmp_path)
    _admit_mutation(db, "update")
    path = db.engine.url.render_as_string(hide_password=False)
    db.close()
    restored = Database(path)
    # No runtime cache or fresh discovery is involved after reopening SQLite.
    assert restored.content_mutations.claim_due(limit=5, stale_after_seconds=300) == []
    _confirm_create(restored)
    source_id = _commit_reserved_mutation(restored, "update")
    with restored.session() as session:
        source = session.get(UserActorOutboundSource, source_id)
        assert json.loads(source.snapshot_json)["directGroupRecipients"] == [RECIPIENT]
    restored.close()
