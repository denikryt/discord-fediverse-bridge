"""Generated create/retry exploration for local-community relay deliveries."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from hypothesis import HealthCheck, settings
from hypothesis.strategies import sampled_from
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule, run_state_machine_as_test

from model_based.local_community_relay_model import RelayModel
from support.local_community_relay import LocalCommunityRelayHarness, PlannedOutcome

ALICE = "https://lemmy.example/u/alice"
CAROL = "https://lemmy.example/u/carol"
TARGETS = {ALICE, CAROL}
PLAN_NAMES = ("all_delivered", "alice_delivered", "carol_delivered", "all_failed")


def _plan(name: str, eligible: set[str]) -> dict[str, PlannedOutcome]:
    """Build one deterministic outcome plan for currently eligible targets."""
    outcomes: dict[str, PlannedOutcome] = {}
    for actor_id in eligible:
        ok = (
            name == "all_delivered"
            or (name == "alice_delivered" and actor_id == ALICE)
            or (name == "carol_delivered" and actor_id == CAROL)
        )
        outcomes[actor_id] = PlannedOutcome(
            ok=ok,
            error=None if ok else f"planned failure for {actor_id}",
        )
    return outcomes


class RelayCreateRetryMachine(RuleBasedStateMachine):
    """Generate repeated relay calls against one real source action."""

    def __init__(self) -> None:
        """Create isolated real persistence and independent model state."""
        super().__init__()
        self._temporary_directory = TemporaryDirectory()
        self.harness = LocalCommunityRelayHarness(
            Path(self._temporary_directory.name),
            database_name="relay-stateful.db",
        )
        self.harness.add_default_subscribers()
        self.event = self.harness.post_event(suffix="stateful")
        self.model = RelayModel()

    def teardown(self) -> None:
        """Release SQLite resources after each generated example."""
        self.harness.database.engine.dispose()
        self._temporary_directory.cleanup()

    @rule(plan_name=sampled_from(PLAN_NAMES))
    def relay_create(self, plan_name: str) -> None:
        """Apply one generated gateway plan to a create or retry call."""
        eligible = {
            actor_id
            for actor_id, delivery in self.model.deliveries.items()
            if delivery.status in {"pending", "failed"}
        }
        if not self.model.source_exists:
            eligible = set(TARGETS)
        plan = _plan(plan_name, eligible)
        self.harness.gateway.plan_next(plan)
        model_outcomes = {
            actor_id: (
                outcome.ok,
                f"https://bridge.example/relay/{actor_id.rsplit('/', 1)[-1]}",
                outcome.error,
            )
            for actor_id, outcome in plan.items()
        }
        self.model.relay_create(
            source_json_present=True,
            allowed_targets=TARGETS,
            outcomes=model_outcomes,
        )
        asyncio.run(
            self.harness.runtime.federation_fanout.relay_create(
                event=self.event,
                local_community=self.harness.local_community,
                object_kind="post",
            )
        )

    @rule()
    def inspect_state(self) -> None:
        """Exercise an observation-only step without changing either system."""
        self._assert_alignment()

    @invariant()
    def model_and_sut_remain_aligned(self) -> None:
        """Compare durable relay and transport state after every generated step."""
        self._assert_alignment()

    def _assert_alignment(self) -> None:
        """Assert target uniqueness, retry eligibility, and field-level state."""
        observed = self.harness.observe(self.event)
        assert observed.source_count == int(self.model.source_exists)
        actual = {row.target_actor_id: row for row in observed.deliveries}
        assert len(actual) == len(observed.deliveries)
        assert set(actual) == set(self.model.deliveries)
        for actor_id, expected in self.model.deliveries.items():
            row = actual[actor_id]
            assert row.status == expected.status
            assert row.attempt_count == expected.attempt_count
            assert row.last_error == expected.last_error
            assert bool(row.relay_activity_id) == bool(expected.relay_activity_id)
        assert tuple(tuple(sorted(call)) for call in observed.gateway_calls) == tuple(
            self.model.gateway_calls
        )


def test_generated_local_community_relay_create_retry_sequences() -> None:
    """Run bounded stateful relay examples with Hypothesis shrinking."""
    ci = os.environ.get("HYPOTHESIS_PROFILE") == "ci"
    run_state_machine_as_test(
        RelayCreateRetryMachine,
        settings=settings(
            max_examples=30 if ci else 20,
            stateful_step_count=20 if ci else 15,
            deadline=None,
            suppress_health_check=(HealthCheck.too_slow,),
        ),
    )
