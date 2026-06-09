"""Generated subscriber and policy churn between relay create actions."""

from __future__ import annotations

import asyncio
from enum import StrEnum
from pathlib import Path
from tempfile import TemporaryDirectory

from hypothesis import given, settings
from hypothesis.strategies import lists, sampled_from

from model_based.local_community_relay_model import RelayModel
from support.local_community_relay import LocalCommunityRelayHarness, PlannedOutcome


class ChurnAction(StrEnum):
    """Actions that change target discovery or invoke a source action."""

    ACCEPT_ALICE = "accept_alice"
    REMOVE_ALICE = "remove_alice"
    ACCEPT_CAROL = "accept_carol"
    REMOVE_CAROL = "remove_carol"
    BLOCK_ALICE_HOST = "block_alice_host"
    UNBLOCK_ALICE_HOST = "unblock_alice_host"
    BLOCK_CAROL_HOST = "block_carol_host"
    UNBLOCK_CAROL_HOST = "unblock_carol_host"
    NEW_SOURCE = "new_source"
    RETRY_LAST_SOURCE = "retry_last_source"


@given(actions=lists(sampled_from(tuple(ChurnAction)), min_size=1, max_size=20))
@settings(max_examples=30, deadline=None)
def test_generated_subscriber_and_policy_churn(
    actions: list[ChurnAction],
) -> None:
    """New actions use current targets while historical rows remain durable."""
    temporary_directory = TemporaryDirectory()
    harness = LocalCommunityRelayHarness(
        Path(temporary_directory.name), database_name="relay-churn.db"
    )
    harness.add_subscriber("bob", host="lemmy.example")
    actor_by_name = {
        "alice": "https://alice.example/u/alice",
        "carol": "https://carol.example/u/carol",
    }
    accepted: set[str] = set()
    blocked_hosts: set[str] = set()
    sources: list[tuple[object, RelayModel]] = []
    expected_calls: list[tuple[str, ...]] = []

    def set_subscriber(name: str, present: bool) -> None:
        actor_id = actor_by_name[name]
        if present:
            harness.ensure_subscriber(name, host=f"{name}.example")
            accepted.add(actor_id)
        else:
            harness.remove_subscriber(actor_id)
            accepted.discard(actor_id)

    def set_blocked(name: str, blocked: bool) -> None:
        host = f"{name}.example"
        harness.set_host_blocked(host, blocked=blocked)
        if blocked:
            blocked_hosts.add(host)
        else:
            blocked_hosts.discard(host)

    async def relay(event: object, model: RelayModel) -> None:
        allowed = {
            actor_id
            for actor_id in accepted
            if actor_id.split("/", 3)[2] not in blocked_hosts
        }
        eligible = {
            actor_id
            for actor_id, delivery in model.deliveries.items()
            if delivery.status in {"pending", "failed"}
        }
        if not model.source_exists:
            eligible = set(allowed)
        plan = {actor_id: PlannedOutcome(ok=True) for actor_id in eligible}
        harness.gateway.plan_next(plan)
        model.relay_create(
            source_json_present=True,
            allowed_targets=allowed,
            outcomes={
                actor_id: (True, f"activity:{actor_id}", None)
                for actor_id in eligible
            },
        )
        if eligible:
            expected_calls.append(tuple(sorted(eligible)))
        await harness.runtime.federation_fanout.relay_create(
            event=event,
            local_community=harness.local_community,
            object_kind="post",
        )

    for action in actions:
        if action is ChurnAction.ACCEPT_ALICE:
            set_subscriber("alice", True)
        elif action is ChurnAction.REMOVE_ALICE:
            set_subscriber("alice", False)
        elif action is ChurnAction.ACCEPT_CAROL:
            set_subscriber("carol", True)
        elif action is ChurnAction.REMOVE_CAROL:
            set_subscriber("carol", False)
        elif action is ChurnAction.BLOCK_ALICE_HOST:
            set_blocked("alice", True)
        elif action is ChurnAction.UNBLOCK_ALICE_HOST:
            set_blocked("alice", False)
        elif action is ChurnAction.BLOCK_CAROL_HOST:
            set_blocked("carol", True)
        elif action is ChurnAction.UNBLOCK_CAROL_HOST:
            set_blocked("carol", False)
        elif action is ChurnAction.NEW_SOURCE:
            event = harness.post_event(suffix=f"churn-{len(sources)}")
            model = RelayModel()
            sources.append((event, model))
            asyncio.run(relay(event, model))
        elif action is ChurnAction.RETRY_LAST_SOURCE and sources:
            event, model = sources[-1]
            asyncio.run(relay(event, model))

        for event, model in sources:
            observed = harness.observe(event)
            actual = {row.target_actor_id: row for row in observed.deliveries}
            assert set(actual) == set(model.deliveries)
            for actor_id, expected in model.deliveries.items():
                assert actual[actor_id].status == expected.status
                assert actual[actor_id].attempt_count == expected.attempt_count
        assert tuple(tuple(sorted(call)) for call in harness.gateway.calls) == tuple(
            expected_calls
        )

    harness.database.engine.dispose()
    temporary_directory.cleanup()
