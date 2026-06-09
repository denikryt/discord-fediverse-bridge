# 109 — Test Assurance Stage 2: Ban Contract Pilot

## Problem / Goal

Ban management behavior is already well covered, but its core authorization and lifecycle dimensions are expressed across many separate tests without one executable vocabulary. This stage introduces a bounded, ban-specific typed case model and a real operation harness so important combinations can be reviewed and extended without creating a generic cross-project testing framework.

## Expected Behavior

- A compact immutable `BanContractCase` vocabulary describes action, caller role, scope, community state, target kind, prior ban state, and expected operation outcome.
- A selected operation-level subset runs through the real `ban_user_operation` and `unban_user_operation` paths.
- The pilot covers owner, super-admin, unauthorized caller, scoped/global actions, enabled/disabled/missing communities, remote/local targets, absent/active/removed states, success/forbidden/validation outcomes, reason codes, and persisted state.
- Existing named runtime-enforcement scenarios remain unchanged and continue to prove banned actors are skipped before Discord/ActivityPub side effects.
- No generic `PolicyCase`, report plugin, automatic combination generation, Hypothesis, or mutation tooling is added.

## Architecture

Create `tests/support/ban_contracts.py` as ban-domain data only. It contains string enums/literals, immutable case and expected-result dataclasses, and the selected case tuple. It must not import production evaluators or calculate expected results from production logic.

Create `tests/operations/test_ban_contract_cases.py` as the pilot harness. The harness:

- builds a real SQLite database;
- seeds the requested community, local user, and prior ban state;
- constructs the real `BridgePolicyService` and operation input;
- executes the real ban or unban operation;
- observes operation output and persisted active/inactive ban rows;
- compares those observations with explicit expected data from the case.

The case IDs are stable machine-readable strings because Stage 4 may later report them. Metadata remains ban-specific and carries no reporting behavior.

Example:

```python
BanContractCase(
    id="owner.scoped.enabled.remote.absent.create",
    action="ban",
    caller_role="owner",
    scope="community",
    community_state="enabled",
    target_kind="remote",
    existing_ban_state="absent",
    expected=BanExpected(applied=True, reason="created", active_rows=1),
)
```

## Touched Files

- docs/development/test-assurance.md

## New Files

- tests/support/ban_contracts.py
- tests/operations/test_ban_contract_cases.py
- plans/109_test_assurance_stage_2_ban_contract_pilot.md

## Implementation Steps

1. Write the immutable ban case vocabulary with explicit expected outcomes and no production imports.
2. Write a parameterized operation test that initially fails until the real harness supports all declared dimensions.
3. Implement ban-specific seeding and observation helpers in the pilot test module.
4. Cover a bounded representative set: owner scoped create, super-admin scoped create, unauthorized scoped rejection, super-admin global create, non-admin global rejection, disabled and missing community rejection, local target resolution, duplicate active rejection, removed-row reactivation, active unban, and absent unban.
5. Keep existing behavior scenarios for inbound enforcement and existing command adapter tests unchanged.
6. Document the pilot vocabulary and its intentional ban-only scope.
7. Run focused pilot tests, all ban-related tests, the complete repository suite, compile checks, gateway checks, and diff checks.

## Tests

The parameterized pilot must verify real operation results and persisted moderation rows. It must prove explicit reason codes and distinguish active from inactive rows. Local-target cases must resolve a real registered user and persist immutable Discord identity. Global cases must not require a community.

Existing tests continue to cover response visibility, audit event detail, command adapters, and runtime enforcement; this stage does not replace those clearer named scenarios.

## Boundary and Handoff

| Changes | Preserves | Later work |
|---|---|---|
| Ban-specific typed cases and operation harness | Production behavior and readable historical scenarios | Complete effect snapshots in Stage 3 and passive reporting in Stage 4 |

The pilot intentionally exposes only fields proven useful in ban operations. It does not define a generic framework.

## Regression and Blind-Spot Analysis

- Expected results must remain explicit and independent from production evaluators.
- Case IDs must be stable and unique.
- Local target resolution must use real persisted user identity rather than a mocked resolver.
- Global and scoped rows must not be conflated.
- Removed rows must be observed as reactivated rather than duplicated.
- The pilot must not claim complete ban coverage; Stage 4 reports only declared requirements.

## Open Questions

None.
