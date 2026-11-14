"""CP23: immutable per-tenant relay source and fenced per-target attempts.

Each scenario exercises SQLite and the real relay fanout with a fake HTTP edge.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import select

from src.fedify_gateway_client import SendLocalCommunityRelayOutcome, SendLocalCommunityRelayResult
from src.models import LocalCommunityRelaySourceActivity, LocalCommunityRelayDelivery
from support.local_community_relay import LocalCommunityRelayHarness


@pytest.mark.asyncio
async def test_create_excludes_original_service_and_preserves_canonical_create(tmp_path: Path):
    """A second valid Announce cannot change the original Create or echo to Service."""
    harness = LocalCommunityRelayHarness(tmp_path)
    service = harness.add_subscriber("bridge", host="origin.example")
    downstream = harness.add_subscriber("target", host="other.example")
    event = harness.post_event(suffix="same-object")
    original_source = event.source_activity_json
    first = await harness.runtime.federation_fanout.relay_create(
        event=event, local_community=harness.local_community,
        object_kind="post", origin_subscriber_actor_id=service,
    )
    assert first.delivered == 1
    assert harness.gateway.calls == [(downstream,)]
    changed = event.model_copy(update={
        "source_activity_id": "https://origin.example/activities/create/other",
        "source_announce_id": "https://origin.example/activities/announce/other",
        "source_activity_json": {**original_source, "id": "https://origin.example/activities/create/other"},
    })
    second = await harness.runtime.federation_fanout.relay_create(
        event=changed, local_community=harness.local_community,
        object_kind="post", origin_subscriber_actor_id=service,
    )
    assert second.attempted == 0
    with harness.database.session() as session:
        rows = list(session.scalars(select(LocalCommunityRelaySourceActivity)))
        assert len(rows) == 1
        assert rows[0].source_activity_json == original_source
        assert rows[0].origin_remote_actor_id == service


@pytest.mark.asyncio
async def test_concurrent_duplicate_has_one_send_and_stable_relay_id(tmp_path: Path):
    """Two deliveries for one source cannot simultaneously call the gateway."""
    harness = LocalCommunityRelayHarness(tmp_path)
    harness.add_default_subscribers()
    event = harness.post_event(suffix="race")
    entered, release = asyncio.Event(), asyncio.Event()
    original_send = harness.gateway.send_local_community_relay

    async def delayed(**kwargs):
        entered.set()
        await release.wait()
        return await original_send(**kwargs)

    harness.gateway_boundary.send_local_community_relay.side_effect = delayed
    one = asyncio.create_task(harness.runtime.federation_fanout.relay_create(
        event=event, local_community=harness.local_community, object_kind="post",
    ))
    await entered.wait()
    other = await harness.runtime.federation_fanout.relay_create(
        event=event, local_community=harness.local_community, object_kind="post",
    )
    assert other.attempted == 0
    release.set()
    await one
    assert len(harness.gateway.calls) == 1
    with harness.database.session() as session:
        rows = list(session.scalars(select(LocalCommunityRelayDelivery)))
        assert len(rows) == 2
        assert all(row.status == "delivered" and row.relay_activity_id for row in rows)
        assert all(row.attempt_count == 1 for row in rows)


@pytest.mark.asyncio
async def test_possible_network_send_failure_remains_unknown_without_blind_retry(tmp_path: Path):
    """A timeout may follow remote acceptance and cannot be retried as a known failure."""
    harness = LocalCommunityRelayHarness(tmp_path)
    harness.add_default_subscribers()
    event = harness.post_event(suffix="ambiguous")
    async def timeout(**kwargs):
        raise TimeoutError("response missing after remote send")
    harness.gateway_boundary.send_local_community_relay.side_effect = timeout
    first = await harness.runtime.federation_fanout.relay_create(
        event=event, local_community=harness.local_community, object_kind="post",
    )
    second = await harness.runtime.federation_fanout.relay_create(
        event=event, local_community=harness.local_community, object_kind="post",
    )
    assert first.attempted == 2
    assert second.attempted == 0
    with harness.database.session() as session:
        rows = list(session.scalars(select(LocalCommunityRelayDelivery)))
        assert len(rows) == 2
        assert all(row.status == "unknown" and row.relay_activity_id for row in rows)


@pytest.mark.asyncio
async def test_known_pre_send_denial_can_retry_same_activity(tmp_path: Path):
    """Only an explicit gateway assertion that nothing was sent permits replay."""
    harness = LocalCommunityRelayHarness(tmp_path)
    harness.add_default_subscribers()
    event = harness.post_event(suffix="safe-retry")
    recorded = []
    async def fake(**kwargs):
        payload = kwargs['deliveries']
        recorded.append([(item.target_remote_actor_id, item.activity_json['id']) for item in payload])
        safe = len(recorded) == 1
        return SendLocalCommunityRelayResult(outcomes=[
            SendLocalCommunityRelayOutcome(
                delivery_id=item.delivery_id,
                target_remote_actor_id=item.target_remote_actor_id,
                ok=not safe, activity_id=item.activity_json['id'],
                error='pre-send rejection' if safe else None,
                safe_to_retry=safe,
            ) for item in payload
        ])
    harness.gateway_boundary.send_local_community_relay.side_effect = fake
    first = await harness.runtime.federation_fanout.relay_create(
        event=event, local_community=harness.local_community, object_kind="post",
    )
    second = await harness.runtime.federation_fanout.relay_create(
        event=event, local_community=harness.local_community, object_kind="post",
    )
    assert first.failed == 2
    assert second.delivered == 2
    assert recorded[0] == recorded[1]


@pytest.mark.asyncio
async def test_later_body_drift_cannot_rewrite_first_create_sent_to_new_target(tmp_path: Path):
    """A later Announce cannot replace first body when a new follower appears."""
    harness = LocalCommunityRelayHarness(tmp_path)
    harness.add_subscriber("bob")
    first_target = harness.add_subscriber("alice")
    event = harness.post_event(suffix="drift")
    await harness.runtime.federation_fanout.relay_create(
        event=event, local_community=harness.local_community, object_kind="post",
    )
    later_target = harness.add_subscriber("newcomer")
    first_json = event.source_activity_json
    changed = event.model_copy(update={
        "source_activity_id": "https://lemmy.example/activities/create/alternate",
        "source_activity_json": {
            **first_json, "id": "https://lemmy.example/activities/create/alternate",
            "object": {**first_json["object"], "content": "<p>forged later body</p>"},
        },
    })
    await harness.runtime.federation_fanout.relay_create(
        event=changed, local_community=harness.local_community, object_kind="post",
    )
    assert harness.gateway.calls == [(first_target,), (later_target,)]
    last_request = harness.gateway_boundary.send_local_community_relay.await_args.kwargs
    assert last_request['deliveries'][0].activity_json['object'] == first_json


@pytest.mark.asyncio
async def test_original_service_origin_remains_on_separate_mutation_revision(tmp_path: Path):
    """An owner mutation is forwarded only to the original Create's confirmed targets."""
    harness = LocalCommunityRelayHarness(tmp_path)
    service = harness.add_subscriber("bridge", host="origin.example")
    target = harness.add_subscriber("target", host="other.example")
    event = harness.post_event(suffix="revision")
    await harness.runtime.federation_fanout.relay_create(
        event=event, local_community=harness.local_community,
        object_kind="post", origin_subscriber_actor_id=service,
    )
    update = harness.post_mutation_event(operation="update", object_suffix="revision")
    first = await harness.runtime.federation_fanout.relay_update_or_delete(
        event=update, local_community=harness.local_community,
        object_kind="post", operation="update", origin_subscriber_actor_id=service,
    )
    second = await harness.runtime.federation_fanout.relay_update_or_delete(
        event=update, local_community=harness.local_community,
        object_kind="post", operation="update", origin_subscriber_actor_id=service,
    )
    assert first.attempted == 1
    assert second.attempted == 0
    assert harness.gateway.calls == [(target,), (target,)]
    with harness.database.session() as session:
        rows = list(session.scalars(select(LocalCommunityRelaySourceActivity)))
        assert len(rows) == 2
        assert all(row.origin_remote_actor_id == service for row in rows)


@pytest.mark.asyncio
async def test_conflicting_replayed_author_does_not_claim_new_relay_targets(tmp_path: Path):
    """A forged owner behind a reused AP object cannot change original provenance."""
    from src.db.repositories.local_community_relay import RelayIdentityConflict
    harness = LocalCommunityRelayHarness(tmp_path)
    harness.add_default_subscribers()
    event = harness.post_event(suffix="forged-author")
    await harness.runtime.federation_fanout.relay_create(
        event=event, local_community=harness.local_community, object_kind="post",
    )
    first_calls = list(harness.gateway.calls)
    harness.add_subscriber("new-target")
    changed = event.model_copy(update={"source_activity_json": {
        **event.source_activity_json, "actor": "https://attacker.example/actors/users/evil",
    }})
    with pytest.raises(RelayIdentityConflict):
        await harness.runtime.federation_fanout.relay_create(
            event=changed, local_community=harness.local_community, object_kind="post",
        )
    assert harness.gateway.calls == first_calls


@pytest.mark.asyncio
async def test_gateway_returns_wrong_activity_id_is_unresolved(tmp_path: Path):
    """A success for a different AP identity is not proof this attempt delivered."""
    harness = LocalCommunityRelayHarness(tmp_path)
    harness.add_default_subscribers()
    event = harness.post_event(suffix="wrong-outcome-id")

    async def wrong_id(**kwargs):
        return SendLocalCommunityRelayResult(outcomes=[
            SendLocalCommunityRelayOutcome(
                delivery_id=d.delivery_id, target_remote_actor_id=d.target_remote_actor_id,
                ok=True, activity_id="https://attacker.example/another-activity",
            ) for d in kwargs["deliveries"]
        ])

    harness.gateway_boundary.send_local_community_relay.side_effect = wrong_id
    await harness.runtime.federation_fanout.relay_create(
        event=event, local_community=harness.local_community, object_kind="post",
    )
    with harness.database.session() as session:
        rows = list(session.scalars(select(LocalCommunityRelayDelivery)))
        assert len(rows) == 2
        assert all(row.status == "unknown" for row in rows)


@pytest.mark.asyncio
async def test_source_claiming_another_object_is_denied_before_network(tmp_path: Path):
    """A signed-looking envelope must not claim a different canonical AP object."""
    from src.db.repositories.local_community_relay import RelayIdentityConflict
    harness = LocalCommunityRelayHarness(tmp_path)
    harness.add_default_subscribers()
    event = harness.post_event(suffix="source-id")
    wrong = event.model_copy(update={"source_activity_json": {
        **event.source_activity_json,
        "object": {**event.source_activity_json["object"], "id": "https://evil.example/wrong"},
    }})
    with pytest.raises(RelayIdentityConflict):
        await harness.runtime.federation_fanout.relay_create(
            event=wrong, local_community=harness.local_community, object_kind="post",
        )
    harness.gateway_boundary.send_local_community_relay.assert_not_awaited()
    with harness.database.session() as session:
        assert not list(session.scalars(select(LocalCommunityRelaySourceActivity)))


@pytest.mark.asyncio
async def test_signed_update_and_delete_keep_separate_revision_ids(tmp_path: Path):
    """A Delete is a distinct revision, not a duplicate of an earlier Update."""
    harness = LocalCommunityRelayHarness(tmp_path)
    harness.add_default_subscribers()
    post = harness.post_event(suffix="revisions")
    await harness.runtime.federation_fanout.relay_create(
        event=post, local_community=harness.local_community, object_kind="post",
    )
    for verb in ("update", "delete"):
        event = harness.post_mutation_event(operation=verb, object_suffix="revisions")
        result = await harness.runtime.federation_fanout.relay_update_or_delete(
            event=event, local_community=harness.local_community,
            object_kind="post", operation=verb,
        )
        assert result.delivered == 2
    with harness.database.session() as session:
        sources = list(session.scalars(select(LocalCommunityRelaySourceActivity)))
        assert {source.operation for source in sources} == {"create", "update", "delete"}
        deliveries = list(session.scalars(select(LocalCommunityRelayDelivery)))
        assert len(deliveries) == 6
        ids_by_operation = {}
        for row in deliveries:
            ids_by_operation.setdefault(row.operation, set()).add(row.relay_activity_id)
        assert all(len(ids) == 2 for ids in ids_by_operation.values())
        assert ids_by_operation["create"].isdisjoint(ids_by_operation["update"])
        assert ids_by_operation["update"].isdisjoint(ids_by_operation["delete"])
