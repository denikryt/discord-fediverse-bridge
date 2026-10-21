"""Discord adapter integration relay admission, identity and stale-response regression scenarios.

These cases use a file-backed SQLite database: no Discord or Node transport is
involved in the ownership and delivery-result transaction contracts.
"""

from __future__ import annotations

from types import SimpleNamespace
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

from src.bridge_policy import BridgePolicyService
from src.db import Database
# Loading this pure renderer directly isolates the SQLite behavior tests from the
# existing Discord-dependent package initializer until the SDK adapter cutover.
_renderer_spec = spec_from_file_location(
    "local_community_relay_renderer", Path(__file__).resolve().parents[1]
    / "src/local_communities/activitypub_renderers.py"
)
_renderer = module_from_spec(_renderer_spec)
_renderer_spec.loader.exec_module(_renderer)
render_local_community_relay_activity = _renderer.render_local_community_relay_activity


ACTOR_URL = "https://bridge.example/actors/communities/hackers"
ORIGIN = "https://remote.example/u/bob"


def _world(tmp_path):
    """Prepare one canonical Create with two accepted follower targets."""
    db = Database(f"sqlite:///{tmp_path / 'relay.db'}")
    db.migrate()
    for name in ("alice", "carol"):
        db.remote_subscribers.create_remote_subscriber(
            local_community_id=12,
            remote_actor_id=f"https://{name}.example/users/{name}",
            remote_inbox_url=f"https://{name}.example/inbox",
            follow_activity_id=f"https://{name}.example/follow/12",
        )
    source_json = {
        "id": "https://remote.example/activities/1", "type": "Create",
        "actor": ORIGIN,
        "object": {"id": "https://remote.example/posts/1", "type": "Page", "attributedTo": ORIGIN},
    }
    repo = db.local_community_relay
    source = repo.get_or_create_local_community_relay_source_activity(
        local_community_id=12, object_kind="post", operation="create",
        source_object_ap_id="https://remote.example/posts/1",
        source_activity_id=source_json["id"], source_announce_id=None,
        origin_remote_actor_id=ORIGIN, source_activity_json=source_json,
    )
    deliveries = repo.create_missing_local_community_relay_deliveries(
        source_activity=source,
        targets=[
            {"remote_actor_id": f"https://{name}.example/users/{name}",
             "remote_inbox_url": f"https://{name}.example/inbox",
             "delivery_profile": "generic_activitypub"}
            for name in ("alice", "carol")
        ],
    )
    policy = BridgePolicyService(settings=SimpleNamespace(), repository=db.bridge_policy_entries)
    return db, source, deliveries, policy


def _prepare(db, delivery, policy):
    """Admit one attempted send against current subscriber state and policy."""
    return db.local_community_relay.prepare_local_community_relay_attempt(
        delivery_id=delivery.id, community_actor_url=ACTOR_URL,
        community_slug="hackers", policy_service=policy,
    )


def test_source_shared_announce_identity_and_renderer_requires_reservation(tmp_path):
    """Two followers share one public Announce id; a renderer cannot invent it."""
    db, source, deliveries, policy = _world(tmp_path)
    first = _prepare(db, deliveries[0], policy)
    second = _prepare(db, deliveries[1], policy)
    assert first.relay_activity_id == second.relay_activity_id
    assert "/activities/announce/posts/" in first.relay_activity_id
    event = SimpleNamespace(event_type="post.created", object=SimpleNamespace(kind="post"))
    payloads = [
        render_local_community_relay_activity(
            source_activity_json=source.source_activity_json, normalized_event=event,
            community_actor_url=ACTOR_URL, community_slug="hackers",
            object_kind="post", delivery_profile="generic_activitypub",
            relay_activity_id=attempt.relay_activity_id,
        )
        for attempt in (first, second)
    ]
    assert payloads[0]["id"] == payloads[1]["id"]
    with pytest.raises((TypeError, ValueError)):
        render_local_community_relay_activity(
            source_activity_json=source.source_activity_json, normalized_event=event,
            community_actor_url=ACTOR_URL, community_slug="hackers",
            object_kind="post", delivery_profile="generic_activitypub",
        )
    db.close()


def test_new_attempt_refreshes_inbox_and_rejects_obsolete_result(tmp_path):
    """Follow A->B retargets failed delivery but cannot apply A's late success."""
    db, source, deliveries, policy = _world(tmp_path)
    delivery = deliveries[0]
    old = _prepare(db, delivery, policy)
    with db.immediate_session() as session:
        subscriber = session.query(__import__("src.models", fromlist=["RemoteSubscriber"]).RemoteSubscriber).filter_by(
            local_community_id=12, remote_actor_id=delivery.target_remote_actor_id
        ).one()
        subscriber.remote_inbox_url = "https://alice.example/new-inbox"
    current = _prepare(db, delivery, policy)
    assert current.attempted_inbox_url.endswith("/new-inbox")
    assert current.relay_activity_id == old.relay_activity_id
    assert current.claimed_attempt_count == old.claimed_attempt_count + 1
    stale = db.local_community_relay.mark_local_community_relay_delivery_result(
        delivery_id=delivery.id, status="delivered", relay_activity_id=old.relay_activity_id,
        attempted_inbox_url=old.attempted_inbox_url,
        claimed_attempt_count=old.claimed_attempt_count,
    )
    assert stale == "ignored_stale"
    result = db.local_community_relay.mark_local_community_relay_delivery_result(
        delivery_id=delivery.id, status="delivered", relay_activity_id=current.relay_activity_id,
        attempted_inbox_url=current.attempted_inbox_url,
        claimed_attempt_count=current.claimed_attempt_count,
    )
    assert result == "delivered"
    assert _prepare(db, delivery, policy) is None
    db.close()


def test_undo_and_invalid_route_refuse_send_without_reassigning_identity(tmp_path):
    """Revoked Follow and localhost routes cannot produce an outbound POST."""
    db, source, deliveries, policy = _world(tmp_path)
    delivery = deliveries[0]
    original = _prepare(db, delivery, policy)
    with db.immediate_session() as session:
        subscriber = session.query(__import__("src.models", fromlist=["RemoteSubscriber"]).RemoteSubscriber).filter_by(
            local_community_id=12, remote_actor_id=delivery.target_remote_actor_id
        ).one()
        subscriber.remote_inbox_url = "https://127.0.0.1/inbox"
    assert _prepare(db, delivery, policy) is None
    with db.immediate_session() as session:
        subscriber = session.query(__import__("src.models", fromlist=["RemoteSubscriber"]).RemoteSubscriber).filter_by(
            local_community_id=12, remote_actor_id=delivery.target_remote_actor_id
        ).one()
        subscriber.status = "removed"
    assert _prepare(db, delivery, policy) is None
    assert db.local_community_relay.list_local_community_relay_deliveries_for_source(source.id)[0].relay_activity_id == original.relay_activity_id
    db.close()


def test_legacy_conflicting_ids_are_not_overwritten(tmp_path):
    """Previously published distinct Announce IDs block new logical sends."""
    db, source, deliveries, policy = _world(tmp_path)
    with db.immediate_session() as session:
        for number, row in enumerate(deliveries):
            instance = session.get(type(row), row.id)
            instance.relay_activity_id = f"{ACTOR_URL}/activities/announce/posts/legacy-{number}"
    assert _prepare(db, deliveries[0], policy) is None
    assert _prepare(db, deliveries[1], policy) is None
    assert {r.relay_activity_id for r in db.local_community_relay.list_local_community_relay_deliveries_for_source(source.id)} == {
        f"{ACTOR_URL}/activities/announce/posts/legacy-0",
        f"{ACTOR_URL}/activities/announce/posts/legacy-1",
    }
    db.close()


def test_inflight_route_change_is_pending_not_delivery_success(tmp_path):
    """A successful HTTP response for A cannot acknowledge newly selected B."""
    from src.models import RemoteSubscriber

    db, source, deliveries, policy = _world(tmp_path)
    first = _prepare(db, deliveries[0], policy)
    with db.immediate_session() as session:
        subscriber = session.query(RemoteSubscriber).filter_by(
            local_community_id=12, remote_actor_id=first.target_remote_actor_id
        ).one()
        subscriber.remote_inbox_url = "https://alice.example/inbox-b"
    result = db.local_community_relay.mark_local_community_relay_delivery_result(
        delivery_id=first.delivery_id, status="delivered",
        relay_activity_id=first.relay_activity_id,
        attempted_inbox_url=first.attempted_inbox_url,
        claimed_attempt_count=first.claimed_attempt_count,
        policy_service=policy, community_actor_url=ACTOR_URL,
    )
    assert result == "pending"
    current = _prepare(db, deliveries[0], policy)
    assert current.attempted_inbox_url.endswith("inbox-b")
    assert current.relay_activity_id == first.relay_activity_id
    db.close()


def test_aba_and_reversed_callbacks_never_overwrite_newest_result(tmp_path):
    """Both callbacks to A use the same URL but only newest attempt can win."""
    db, _, deliveries, policy = _world(tmp_path)
    older = _prepare(db, deliveries[0], policy)
    newer = _prepare(db, deliveries[0], policy)
    assert older.attempted_inbox_url == newer.attempted_inbox_url
    assert newer.claimed_attempt_count > older.claimed_attempt_count
    ok = db.local_community_relay.mark_local_community_relay_delivery_result(
        delivery_id=newer.delivery_id, status="delivered",
        relay_activity_id=newer.relay_activity_id,
        attempted_inbox_url=newer.attempted_inbox_url,
        claimed_attempt_count=newer.claimed_attempt_count,
        policy_service=policy, community_actor_url=ACTOR_URL,
    )
    late_error = db.local_community_relay.mark_local_community_relay_delivery_result(
        delivery_id=older.delivery_id, status="failed",
        relay_activity_id=older.relay_activity_id,
        attempted_inbox_url=older.attempted_inbox_url,
        claimed_attempt_count=older.claimed_attempt_count,
        policy_service=policy, community_actor_url=ACTOR_URL, error="timeout",
    )
    assert (ok, late_error) == ("delivered", "ignored_already_delivered")
    assert db.local_community_relay.list_local_community_relay_deliveries_for_source(
        deliveries[0].source_activity_row_id
    )[0].attempt_count == newer.claimed_attempt_count
    db.close()


def test_canonical_source_identity_conflict_rejected_before_side_effects(tmp_path):
    """A repeated source key cannot inject a forged author or changed payload."""
    db, source, deliveries, policy = _world(tmp_path)
    with pytest.raises(ValueError, match="source identity conflict"):
        db.local_community_relay.get_or_create_local_community_relay_source_activity(
            local_community_id=12, object_kind="post", operation="create",
            source_object_ap_id=source.source_object_ap_id,
            source_activity_id=source.source_activity_id,
            source_announce_id=source.source_announce_id,
            origin_remote_actor_id="https://attacker.example/u/mallory",
            source_activity_json=source.source_activity_json,
        )
    assert _prepare(db, deliveries[0], policy) is not None
    db.close()


def test_policy_revocation_rejects_delivery_before_http(tmp_path):
    """Current dynamic block rules override an earlier eligible target list."""
    from src.bridge_policy import PolicyType

    db, _, deliveries, policy = _world(tmp_path)
    db.bridge_policy_entries.create_active(
        policy_type=PolicyType.FEDERATION_BLOCK.value,
        normalized_subject="alice.example",
        actor_discord_user_id="123", reason="banned",
    )
    assert _prepare(db, deliveries[0], policy) is None
    assert _prepare(db, deliveries[1], policy) is not None
    db.close()
