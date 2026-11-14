"""Verified local-Group deliveries keep wire IDs but isolate durable receipts.

Each test records a signed-gateway-delivered event in a real SQLite database;
only the already-authenticated gateway/recipient selection is represented by a
small event fixture. Other ActivityPub families retain global receipts.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src.activitypub_models import ActivityPubEvent
from src.db import Database
from src.http_api import _begin_event_attempt, _finish_event_processing
from src.inbound_activity_outcomes import InboundActivityOutcome

WIRE = "https://remote.example/activities/create/shared"
GROUP_A = "https://bridge.example/actors/communities/a"
GROUP_B = "https://bridge.example/actors/communities/b"
OBJECT = "https://remote.example/objects/42"


def _event(group: str, *, wire: str = WIRE) -> ActivityPubEvent:
    """Represent one validated gateway content delivery to a specific Group."""
    return ActivityPubEvent.model_validate({
        "event_type": "post.created", "delivery_id": wire,
        "occurred_at": datetime(2026, 11, 12, tzinfo=timezone.utc).isoformat(),
        "community_actor_id": group,
        "verified_destination_group_actor_id": group,
        "actor_id": "https://remote.example/actors/users/person",
        "object": {
            "ap_id": OBJECT, "kind": "post", "lemmy_id": 0, "published_at": "2026-11-12T00:00:00Z",
            "author_name": "person", "body_markdown": "example",
        },
    })


def test_a_and_b_each_have_an_independent_receipt_and_exact_wire_id(tmp_path):
    """A consumed delivery must not short-circuit the same signed wire ID for B."""
    db = Database(f"sqlite:///{tmp_path / 'scoped.db'}")
    db.create_all()
    runtime = SimpleNamespace(database=db)
    a, b = _event(GROUP_A), _event(GROUP_B)
    assert _begin_event_attempt(runtime, a) == (None, False)
    _finish_event_processing(runtime, WIRE, "processed", InboundActivityOutcome.APPLIED,
                             "A", verified_destination_group_actor_id=GROUP_A)
    assert _begin_event_attempt(runtime, b) == (None, False)
    _finish_event_processing(runtime, WIRE, "skipped", InboundActivityOutcome.IGNORED_BY_BAN,
                             "B", verified_destination_group_actor_id=GROUP_B)
    assert db.event_receipts.get_event_receipt(WIRE, verified_destination_group_actor_id=GROUP_A).detail == "A"
    assert db.event_receipts.get_event_receipt(WIRE, verified_destination_group_actor_id=GROUP_B).detail == "B"
    assert _begin_event_attempt(runtime, a)[0]["status"] == "duplicate"
    assert _begin_event_attempt(runtime, b)[0]["status"] == "duplicate"
    assert db.event_receipts.get_event_receipt(WIRE) is None
    db.close()


def test_legacy_global_receipt_does_not_suppress_new_scoped_group(tmp_path):
    """An opaque historical delivery must not authorize or deny either new tenant."""
    db = Database(f"sqlite:///{tmp_path / 'legacy.db'}")
    db.create_all()
    db.event_receipts.create_event_receipt(
        delivery_id=WIRE, event_type="post.created", object_ap_id=OBJECT,
        status="processed",
    )
    runtime = SimpleNamespace(database=db)
    assert _begin_event_attempt(runtime, _event(GROUP_A)) == (None, False)
    assert db.event_receipts.get_event_receipt(WIRE).status == "processed"
    db.close()


def test_conflicting_group_scope_is_rejected_before_db_effects(tmp_path):
    """The local Group verified by the gateway must equal the routed community."""
    from pydantic import ValidationError
    data = _event(GROUP_A).model_dump(mode="json")
    data["verified_destination_group_actor_id"] = GROUP_B
    with pytest.raises(ValidationError, match="verified destination"):
        ActivityPubEvent.model_validate(data)


def test_two_groups_can_hold_same_wire_id_but_duplicate_in_one_scope_cannot(tmp_path):
    """The two v1 partial unique indexes enforce independent tenant identities."""
    db = Database(f"sqlite:///{tmp_path / 'duplicates.db'}")
    db.create_all()
    for group in (GROUP_A, GROUP_B):
        db.event_receipts.create_event_receipt(
            delivery_id=WIRE, event_type="post.created", object_ap_id=OBJECT,
            status="processed", verified_destination_group_actor_id=group,
        )
    with pytest.raises(Exception):
        db.event_receipts.create_event_receipt(
            delivery_id=WIRE, event_type="post.created", object_ap_id=OBJECT,
            status="processed", verified_destination_group_actor_id=GROUP_A,
        )
    db.close()


def test_concurrent_duplicate_claim_has_one_winner_and_no_cross_scope_effect(tmp_path):
    """Two handlers racing inside one Group cannot start two host deliveries."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, Lock

    db = Database(f"sqlite:///{tmp_path / 'race.db'}")
    db.create_all()
    runtime = SimpleNamespace(database=db)
    barrier = Barrier(2, timeout=5)
    lock = Lock()
    original = db.event_receipts.get_event_receipt
    first_reads = 0

    def concurrent_read(*args, **kwargs):
        nonlocal first_reads
        with lock:
            first_reads += 1
            initial = first_reads <= 2
        if initial:
            result = original(*args, **kwargs)
            barrier.wait()
            return result
        return original(*args, **kwargs)

    db.event_receipts.get_event_receipt = concurrent_read

    def attempt():
        try:
            return _begin_event_attempt(runtime, _event(GROUP_A))[0]
        except HTTPException as exc:
            return exc.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: attempt(), range(2)))
    assert sorted(str(value) for value in results) == ["409", "None"]
    assert original(WIRE, verified_destination_group_actor_id=GROUP_A) is not None
    assert original(WIRE, verified_destination_group_actor_id=GROUP_B) is None
    db.close()


def test_different_announce_ids_remain_distinct_wire_receipts(tmp_path):
    """Inner Create identity cannot replace distinct signed outer Announce IDs."""
    db = Database(f"sqlite:///{tmp_path / 'outer.db'}")
    db.create_all()
    runtime = SimpleNamespace(database=db)
    for wire in ("https://remote.example/announces/first", "https://remote.example/announces/second"):
        event = _event(GROUP_A, wire=wire)
        assert _begin_event_attempt(runtime, event) == (None, False)
    assert db.event_receipts.get_event_receipt(
        "https://remote.example/announces/first",
        verified_destination_group_actor_id=GROUP_A,
    ) is not None
    db.close()


def test_global_follow_receipt_does_not_acquire_local_group_scope(tmp_path):
    """Non-content Follow identity remains one globally unique wire ID."""
    db = Database(f"sqlite:///{tmp_path / 'follow.db'}")
    db.create_all()
    db.event_receipts.create_event_receipt(
        delivery_id=WIRE, event_type="local.follow_requested", object_ap_id=WIRE,
        status="processed",
    )
    assert db.event_receipts.get_event_receipt(WIRE) is not None
    assert db.event_receipts.get_event_receipt(
        WIRE, verified_destination_group_actor_id=GROUP_A) is None
    db.close()


def test_http_admits_only_real_server_verified_local_group_before_receipt(tmp_path):
    """A forged/absent Group destination is rejected without a receipt INSERT."""
    from src.http_api import _validate_verified_local_group_destination

    db = Database(f"sqlite:///{tmp_path / 'admission.db'}")
    db.create_all()
    db.local_communities.create_local_community(
        discord_guild_id=10, discord_forum_channel_id=11,
        slug="a", display_name="a", summary=None, created_by_discord_user_id="12",
        actor_url=GROUP_A, inbox_url=f"{GROUP_A}/inbox",
        outbox_url=f"{GROUP_A}/outbox", followers_url=f"{GROUP_A}/followers",
        public_key_pem="public", private_key_pem="private",
    )
    runtime = SimpleNamespace(database=db)
    _validate_verified_local_group_destination(runtime, _event(GROUP_A))
    # Legacy/local content without trusted recipient is not promoted by its
    # caller-supplied community_actor_id or the existence of a Group row.
    without_proof = _event(GROUP_A).model_copy(
        update={"verified_destination_group_actor_id": None})
    with pytest.raises(HTTPException) as rejected:
        _validate_verified_local_group_destination(runtime, without_proof)
    assert rejected.value.status_code == 400
    with pytest.raises(HTTPException):
        _validate_verified_local_group_destination(runtime, _event(GROUP_B))
    assert db.event_receipts.get_event_receipt(WIRE) is None
    # Remote Group content has no local Group owner and must remain global.
    remote = _event("https://nodebb.example/category/1").model_copy(
        update={"verified_destination_group_actor_id": None})
    _validate_verified_local_group_destination(runtime, remote)
    db.close()
