"""Fixed model-vs-SUT sequences for local-community federation relay."""

from __future__ import annotations

from pathlib import Path

import pytest

from support.local_community_relay import LocalCommunityRelayHarness, PlannedOutcome
from model_based.local_community_relay_model import RelayModel

ALICE = "https://lemmy.example/u/alice"
CAROL = "https://lemmy.example/u/carol"
TARGETS = {ALICE, CAROL}


def _model_outcomes(
    outcomes: dict[str, PlannedOutcome],
) -> dict[str, tuple[bool, str | None, str | None]]:
    """Translate explicit gateway plans into independent model outcomes."""
    return {
        actor_id: (
            outcome.ok,
            f"https://bridge.example/relay/{actor_id.rsplit('/', 1)[-1]}",
            outcome.error,
        )
        for actor_id, outcome in outcomes.items()
    }


def _assert_alignment(model: RelayModel, harness: LocalCommunityRelayHarness, event: object) -> None:
    """Compare each durable and transport field with readable diagnostics."""
    observed = harness.observe(event)
    assert observed.source_count == int(model.source_exists)
    observed_by_actor = {row.target_actor_id: row for row in observed.deliveries}
    assert set(observed_by_actor) == set(model.deliveries)
    for actor_id, expected in model.deliveries.items():
        actual = observed_by_actor[actor_id]
        assert actual.status == expected.status
        assert actual.attempt_count == expected.attempt_count
        assert actual.last_error == expected.last_error
        assert bool(actual.relay_activity_id) == bool(expected.relay_activity_id)
    assert tuple(tuple(sorted(call)) for call in observed.gateway_calls) == tuple(
        model.gateway_calls
    )


async def _run_action(
    *,
    model: RelayModel,
    harness: LocalCommunityRelayHarness,
    event: object,
    plan: dict[str, PlannedOutcome],
    allowed_targets: set[str] = TARGETS,
    source_json_present: bool = True,
) -> None:
    """Apply one explicit action to both model and real fanout."""
    harness.gateway.plan_next(plan)
    model.relay_create(
        source_json_present=source_json_present,
        allowed_targets=set(allowed_targets),
        outcomes=_model_outcomes(plan),
    )
    await harness.runtime.federation_fanout.relay_create(
        event=event,
        local_community=harness.local_community,
        object_kind="post",
    )
    _assert_alignment(model, harness, event)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "first_plan,second_plan",
    [
        ({ALICE: PlannedOutcome(True), CAROL: PlannedOutcome(True)}, None),
        (
            {
                ALICE: PlannedOutcome(True),
                CAROL: PlannedOutcome(False, "temporary"),
            },
            {CAROL: PlannedOutcome(True)},
        ),
        (
            {
                ALICE: PlannedOutcome(False, "a"),
                CAROL: PlannedOutcome(False, "c"),
            },
            {ALICE: PlannedOutcome(True), CAROL: PlannedOutcome(True)},
        ),
    ],
)
async def test_fixed_create_retry_sequences_align_with_real_fanout(
    tmp_path: Path,
    first_plan: dict[str, PlannedOutcome],
    second_plan: dict[str, PlannedOutcome] | None,
) -> None:
    """Model and SUT agree for success, mixed failure, and all-failed retry flows."""
    harness = LocalCommunityRelayHarness(tmp_path)
    harness.add_default_subscribers()
    event = harness.post_event(suffix="fixed")
    model = RelayModel()
    await _run_action(model=model, harness=harness, event=event, plan=first_plan)
    if second_plan is not None:
        await _run_action(model=model, harness=harness, event=event, plan=second_plan)


@pytest.mark.asyncio
async def test_duplicate_successful_call_creates_no_new_attempt(tmp_path: Path) -> None:
    """A duplicate call after complete delivery reuses state and sends nothing."""
    harness = LocalCommunityRelayHarness(tmp_path)
    harness.add_default_subscribers()
    event = harness.post_event(suffix="duplicate")
    model = RelayModel()
    plan = {ALICE: PlannedOutcome(True), CAROL: PlannedOutcome(True)}
    await _run_action(model=model, harness=harness, event=event, plan=plan)
    model.relay_create(
        source_json_present=True,
        allowed_targets=TARGETS,
        outcomes={},
    )
    await harness.runtime.federation_fanout.relay_create(
        event=event,
        local_community=harness.local_community,
        object_kind="post",
    )
    _assert_alignment(model, harness, event)


@pytest.mark.asyncio
async def test_policy_denied_target_is_absent_from_model_and_sut(tmp_path: Path) -> None:
    """Only independently allowed targets receive durable delivery rows."""
    harness = LocalCommunityRelayHarness(tmp_path)
    harness.add_default_subscribers()
    harness.database.bridge_policy_entries.create_active(
        policy_type="federation_block",
        normalized_subject="blocked.example",
        actor_discord_user_id="123",
        reason="blocked",
    )
    blocked = harness.add_subscriber("mallory", host="blocked.example")
    event = harness.post_event(suffix="policy")
    model = RelayModel()
    plan = {ALICE: PlannedOutcome(True), CAROL: PlannedOutcome(True)}
    await _run_action(model=model, harness=harness, event=event, plan=plan)
    assert blocked not in model.deliveries


@pytest.mark.asyncio
async def test_missing_source_json_creates_no_state_or_transport(tmp_path: Path) -> None:
    """Model and SUT both remain empty when no safe source activity exists."""
    harness = LocalCommunityRelayHarness(tmp_path)
    harness.add_default_subscribers()
    event = harness.post_event(suffix="missing-source", source_json=False)
    model = RelayModel()
    model.relay_create(
        source_json_present=False,
        allowed_targets=TARGETS,
        outcomes={},
    )
    await harness.runtime.federation_fanout.relay_create(
        event=event,
        local_community=harness.local_community,
        object_kind="post",
    )
    _assert_alignment(model, harness, event)
