"""Repeated signed remote Group events resume only safe missing Discord targets."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient

from src.activitypub_models import ActivityPubEvent, RemoteGroupAnnounceEvent
from src.db import Database
from src.http_api import _begin_event_attempt, create_http_app
from src.inbound_activity_outcomes import InboundActivityOutcome

GROUP = "https://nodebb.example/category/42"
OBJECT = "https://nodebb.example/post/101"
WIRE = "https://nodebb.example/activities/announce/101"


def _announce(*, actor: str = GROUP, object_id: str = OBJECT) -> RemoteGroupAnnounceEvent:
    """Model one signature-verified NodeBB Group announcement from gateway."""
    return RemoteGroupAnnounceEvent.model_validate({
        "event_type": "remote.group_announced", "delivery_id": WIRE,
        "occurred_at": datetime(2026, 11, 12, tzinfo=timezone.utc).isoformat(),
        "actor_id": actor, "community_actor_id": actor,
        "source_activity_json": {"id": WIRE, "type": "Announce", "actor": actor,
                                 "object": object_id},
        "object": {"ap_id": object_id},
    })


def _root() -> ActivityPubEvent:
    """Model normalized remote post that still uses the original wire ID."""
    return ActivityPubEvent.model_validate({
        "event_type": "post.created", "delivery_id": WIRE,
        "occurred_at": datetime(2026, 11, 12, tzinfo=timezone.utc).isoformat(),
        "actor_id": "https://author.example/users/alice", "community_actor_id": GROUP,
        "object": {"ap_id": OBJECT, "kind": "post", "lemmy_id": 0,
                   "published_at": "2026-11-12T00:00:00Z", "author_name": "alice", "body_markdown": "first"},
    })


def test_terminal_nodebb_announcement_reaches_handler_again_for_safe_fanout(tmp_path):
    """The wire receipt must not stop a valid replay before target CAS claims."""
    db = Database(f"sqlite:///{tmp_path / 'nodebb-resume.db'}")
    db.create_all()
    runtime = SimpleNamespace(database=db)
    try:
        assert _begin_event_attempt(runtime, _announce()) == (None, False)
        db.event_receipts.update_event_receipt(
            delivery_id=WIRE, status="processed", outcome=InboundActivityOutcome.APPLIED,
            detail="post created",
        )
        assert _begin_event_attempt(runtime, _announce()) == (None, False)
        # Conflicting reuse of a wire ID cannot gain the old receipt's authority.
        assert _begin_event_attempt(runtime, _announce(object_id=OBJECT + "-other"))[0] is not None
    finally:
        db.close()


def test_duplicate_completed_remote_content_keeps_duplicate_http_contract(tmp_path, monkeypatch):
    """A completed event must not change its response to skipped on a no-op replay."""
    import src.http_api as api
    from src.activitypub_handlers import HandlerResult

    db = Database(f"sqlite:///{tmp_path / 'http-repeat.db'}")
    db.create_all()
    runtime = SimpleNamespace(database=db,
                              settings=SimpleNamespace(fedify_shared_secret="private-test-token"))
    monkeypatch.setattr(api, "create_internal_fedify_router", lambda _runtime: APIRouter())
    result = AsyncMock(side_effect=[
        HandlerResult("processed", InboundActivityOutcome.APPLIED, "post created"),
        HandlerResult("skipped", InboundActivityOutcome.IGNORED_ALREADY_APPLIED,
                      "no safe new post deliveries"),
    ])
    monkeypatch.setattr(api, "dispatch_activitypub_event", result)
    client = TestClient(create_http_app(runtime))
    headers = {"Authorization": "Bearer private-test-token", "X-Bridge-Delivery-Id": WIRE}
    try:
        first = client.post("/internal/activitypub/events", json=_root().model_dump(mode="json"), headers=headers)
        second = client.post("/internal/activitypub/events", json=_root().model_dump(mode="json"), headers=headers)
        assert first.status_code == second.status_code == 200
        assert first.json()["status"] == "processed"
        assert second.json()["status"] == "duplicate"
        assert result.await_count == 2  # revalidation/fanout still runs behind receipt.
        assert db.event_receipts.get_event_receipt(WIRE).status == "processed"
    finally:
        client.close()
        db.close()


def test_actual_nodebb_http_replay_completes_only_failed_forum(tmp_path, monkeypatch):
    """One signed wire ID delivered twice reaches canonical NodeBB fanout both times.

    This exercises the HTTP receipt, authenticated-Group reader, canonical
    SQLite reservations and two Discord channels, not a synthetic post.created
    dispatcher bypass. Only the HTTP boundary and Discord SDK are substituted.
    """
    import json
    from pathlib import Path
    import src.http_api as api
    import src.community_sync.nodebb_inbound as nodebb
    from src.community_sync.runtime import CommunityRuntime
    from src.content_publish_service import ContentPublishService
    from support.runtime import build_test_policy_service

    fixture_dir = Path(__file__).resolve().parents[1] / "fixtures" / "nodebb"
    docs = {
        GROUP: json.loads((fixture_dir / "category_actor.json").read_text()),
        OBJECT: json.loads((fixture_dir / "root_note.json").read_text()),
    }

    class VerifiedNetworkBoundary:
        """Serve fixed public NodeBB Group/Note fixtures at the HTTPS boundary."""
        def __init__(self, _origin, _policy):
            pass

        async def __call__(self, url, _accept):
            return docs[url]

    monkeypatch.setattr(nodebb, "SecureNodeBBJSON", VerifiedNetworkBoundary)
    monkeypatch.setattr(api, "create_internal_fedify_router", lambda _runtime: APIRouter())

    db = Database(f"sqlite:///{tmp_path / 'full-nodebb-replay.db'}")
    db.create_all()
    for channel in (100, 101):
        db.remote_subscriptions.create_subscription(
            discord_channel_id=channel, lemmy_community_actor_id=GROUP,
            lemmy_community_name="Discussion", lemmy_community_id=None,
            community_inbox_url=GROUP + "/inbox", status="accepted",
        )
    db.bridge_actor_follows.create_bridge_actor_follow(
        community_actor_id=GROUP, follow_activity_id=GROUP + "/follows/one",
        community_inbox_url=GROUP + "/inbox", status="accepted",
    )
    with db.session() as session:
        from src.models import BridgeActorFollow
        follow = session.query(BridgeActorFollow).first()
        follow.remote_software_profile = "nodebb"

    forum_a = SimpleNamespace(create_thread=AsyncMock(
        return_value=(SimpleNamespace(id=201), SimpleNamespace(id=301))))
    forum_b = SimpleNamespace(create_thread=AsyncMock(
        return_value=(SimpleNamespace(id=202), SimpleNamespace(id=302))))
    fetch_calls = {100: 0, 101: 0}

    async def fetch_forum_channel(channel):
        """Channel B is definitely unavailable on the first attempt, before REST."""
        fetch_calls[channel] += 1
        if channel == 101 and fetch_calls[channel] == 1:
            raise ConnectionError("forum not available before Discord REST")
        return {100: forum_a, 101: forum_b}[channel]

    bot = SimpleNamespace(
        fetch_forum_channel=fetch_forum_channel,
        wait_until_bridge_ready=AsyncMock(return_value=None),
    )
    policy = build_test_policy_service(db)
    publisher = ContentPublishService(database=db, fedify_gateway=AsyncMock(),
                                      bridge_prefix="[bridge]", bridge_policy_service=policy)
    community = CommunityRuntime(database=db, content_publish_service=publisher, bot=bot)
    runtime = SimpleNamespace(
        database=db, bot=bot, community_runtime=community,
        bridge_policy_service=policy,
        settings=SimpleNamespace(fedify_shared_secret="test-secret"),
    )
    client = TestClient(create_http_app(runtime))
    headers = {"Authorization": "Bearer test-secret", "X-Bridge-Delivery-Id": WIRE}
    body = _announce().model_dump(mode="json")
    try:
        first = client.post("/internal/activitypub/events", headers=headers, json=body)
        assert first.status_code == 200, first.text
        assert first.json()["status"] == "processed"
        assert forum_a.create_thread.await_count == 1
        assert forum_b.create_thread.await_count == 0

        second = client.post("/internal/activitypub/events", headers=headers, json=body)
        assert second.status_code == 200, second.text
        assert second.json()["status"] == "processed"
        assert forum_a.create_thread.await_count == 1
        assert forum_b.create_thread.await_count == 1

        third = client.post("/internal/activitypub/events", headers=headers, json=body)
        assert third.status_code == 200, third.text
        assert third.json()["status"] == "duplicate"
        assert forum_a.create_thread.await_count == forum_b.create_thread.await_count == 1
        assert db.event_receipts.get_event_receipt(WIRE).status == "processed"
    finally:
        client.close()
        db.close()


def test_resume_exception_does_not_erase_original_applied_receipt(tmp_path, monkeypatch):
    """A failed recheck must not change the already durable first success."""
    import src.http_api as api

    db = Database(f"sqlite:///{tmp_path / 'stable-receipt.db'}")
    db.create_all()
    db.event_receipts.create_event_receipt(
        delivery_id=WIRE, event_type="remote.group_announced", object_ap_id=OBJECT,
        status="processed", outcome=InboundActivityOutcome.APPLIED,
        detail="first forum confirmed",
    )
    runtime = SimpleNamespace(database=db,
                              settings=SimpleNamespace(fedify_shared_secret="test-secret"))
    monkeypatch.setattr(api, "create_internal_fedify_router", lambda _runtime: APIRouter())
    monkeypatch.setattr(api, "dispatch_activitypub_event", AsyncMock(
        side_effect=RuntimeError("temporary Group fetch failure")))
    client = TestClient(create_http_app(runtime), raise_server_exceptions=False)
    try:
        response = client.post("/internal/activitypub/events",
            headers={"Authorization": "Bearer test-secret", "X-Bridge-Delivery-Id": WIRE},
            json=_announce().model_dump(mode="json"))
        assert response.status_code == 500
        receipt = db.event_receipts.get_event_receipt(WIRE)
        assert (receipt.status, receipt.outcome, receipt.detail) == (
            "processed", "applied", "first forum confirmed")
    finally:
        client.close()
        db.close()
