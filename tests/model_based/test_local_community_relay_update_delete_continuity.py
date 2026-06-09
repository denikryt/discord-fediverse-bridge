"""Generated update/delete continuity exploration for local-community relay."""

from __future__ import annotations

import asyncio
from enum import StrEnum
from pathlib import Path
from tempfile import TemporaryDirectory

from hypothesis import given, settings
from hypothesis.strategies import lists, sampled_from

from model_based.local_community_relay_model import RelayContinuityModel
from support.local_community_relay import LocalCommunityRelayHarness, PlannedOutcome

ALICE = "https://lemmy.example/u/alice"
CAROL = "https://lemmy.example/u/carol"


class ContinuityAction(StrEnum):
    """Subscription and mutation actions for one logical remote post."""

    REMOVE_ALICE = "remove_alice"
    READMIT_ALICE = "readmit_alice"
    UPDATE_SUCCESS = "update_success"
    UPDATE_CAROL_FAILS = "update_carol_fails"
    RETRY_UPDATE = "retry_update"
    DELETE_SUCCESS = "delete_success"
    DELETE_ALICE_FAILS = "delete_alice_fails"
    RETRY_DELETE = "retry_delete"


def _eligible_for_attempt(model: object, allowed: set[str]) -> set[str]:
    """Return pending or failed rows, or all allowed actors for a new source."""
    if not model.source_exists:
        return set(allowed)
    return {
        actor_id
        for actor_id, delivery in model.deliveries.items()
        if delivery.status in {"pending", "failed"}
    }


@given(actions=lists(sampled_from(tuple(ContinuityAction)), min_size=1, max_size=6))
@settings(max_examples=3, deadline=None)
def test_generated_update_delete_continuity(actions: list[ContinuityAction]) -> None:
    """Mutation relay follows delivered-create history and current subscription."""
    temporary_directory = TemporaryDirectory()
    harness = LocalCommunityRelayHarness(
        Path(temporary_directory.name),
        database_name="relay-continuity.db",
    )
    harness.add_default_subscribers()
    create_event = harness.post_event(suffix="continuity")
    harness.gateway.plan_next(
        {ALICE: PlannedOutcome(True), CAROL: PlannedOutcome(True)}
    )
    asyncio.run(
        harness.runtime.federation_fanout.relay_create(
            event=create_event,
            local_community=harness.local_community,
            object_kind="post",
        )
    )
    model = RelayContinuityModel(
        delivered_create_targets={ALICE, CAROL},
        accepted_subscribers={ALICE, CAROL},
    )
    events = {
        "update": harness.post_mutation_event(
            operation="update", object_suffix="continuity"
        ),
        "delete": harness.post_mutation_event(
            operation="delete", object_suffix="continuity"
        ),
    }

    async def relay(operation: str, failures: set[str]) -> None:
        operation_model = model.model_for(operation)
        allowed = model.eligible_targets()
        eligible = _eligible_for_attempt(operation_model, allowed)
        plan = {
            actor_id: PlannedOutcome(
                ok=actor_id not in failures,
                error=("planned mutation failure" if actor_id in failures else None),
            )
            for actor_id in eligible
        }
        harness.gateway.plan_next(plan)
        operation_model.relay_create(
            source_json_present=True,
            allowed_targets=allowed,
            outcomes={
                actor_id: (
                    outcome.ok,
                    f"activity:{operation}:{actor_id}",
                    outcome.error,
                )
                for actor_id, outcome in plan.items()
            },
        )
        await harness.runtime.federation_fanout.relay_update_or_delete(
            event=events[operation],
            local_community=harness.local_community,
            object_kind="post",
            operation=operation,
        )

    for action in actions:
        if action is ContinuityAction.REMOVE_ALICE:
            harness.remove_subscriber(ALICE)
            model.accepted_subscribers.discard(ALICE)
        elif action is ContinuityAction.READMIT_ALICE:
            harness.ensure_subscriber("alice", host="lemmy.example")
            model.accepted_subscribers.add(ALICE)
        elif action is ContinuityAction.UPDATE_SUCCESS:
            asyncio.run(relay("update", set()))
        elif action is ContinuityAction.UPDATE_CAROL_FAILS:
            asyncio.run(relay("update", {CAROL}))
        elif action is ContinuityAction.RETRY_UPDATE:
            asyncio.run(relay("update", set()))
        elif action is ContinuityAction.DELETE_SUCCESS:
            asyncio.run(relay("delete", set()))
        elif action is ContinuityAction.DELETE_ALICE_FAILS:
            asyncio.run(relay("delete", {ALICE}))
        elif action is ContinuityAction.RETRY_DELETE:
            asyncio.run(relay("delete", set()))

        for operation in ("update", "delete"):
            operation_model = model.model_for(operation)
            observed = harness.observe(events[operation], operation=operation)
            actual = {row.target_actor_id: row for row in observed.deliveries}
            assert set(actual) == set(operation_model.deliveries)
            for actor_id, expected in operation_model.deliveries.items():
                row = actual[actor_id]
                assert row.status == expected.status
                assert row.attempt_count == expected.attempt_count
                assert row.last_error == expected.last_error

    create_observed = harness.observe(create_event)
    assert {row.target_actor_id for row in create_observed.deliveries} == {
        ALICE,
        CAROL,
    }
    harness.database.engine.dispose()
    temporary_directory.cleanup()
