# 114 — Test Assurance Stage 7: Stateful Ban Lifecycle Model

## Problem / Goal

Named and typed example tests cover individual ban transitions, but they do not systematically explore repeated and reordered create/remove operations on one persistent lifecycle. Stage 7 adds one independent Hypothesis state machine for a bounded community-scoped remote ban lifecycle.

## Scope and Boundary

### Changes

- Add a stateful test that generates sequences of create, repeat-create, remove, repeat-remove, reactivate, query, and enforcement checks.
- Keep independent model state as `ABSENT`, `ACTIVE`, or `REMOVED` plus expected audit history.
- Execute real `ban_user_operation`, `unban_user_operation`, SQLite persistence, and `UserBanService` enforcement.
- Compare model and implementation after every generated rule.
- Document focused execution and runtime cadence.

### Preserves

- Existing named ban scenarios and typed contract cases remain unchanged.
- Production code and APIs remain unchanged unless a minimized failing sequence proves a defect.
- Stage 6 value-space properties remain separate from lifecycle coverage.

### Explicitly excluded

- Global/community scope combinations in one machine.
- Local target resolution variants.
- Community disable/enable transitions.
- Concurrency or async race generation.
- Sequence covering arrays, combinatorial entry-point variation, fault injection, or mutation testing.
- Using repository state as the model oracle.

## Independent Model

Create `tests/stateful/test_ban_lifecycle_state_machine.py`.

The model owns:

```python
class ModelState(Enum):
    ABSENT = "absent"
    ACTIVE = "active"
    REMOVED = "removed"
```

It also tracks the exact expected action-local audit sequence. The model transitions are declared independently:

- `create` from `ABSENT` -> `ACTIVE`, reason `created`, audit `ban.created/success`;
- `create` from `REMOVED` -> `ACTIVE`, reason `reactivated`, audit `ban.reactivated/success`;
- `create` from `ACTIVE` -> `ACTIVE`, reason `duplicate_active_ban`, no audit;
- `remove` from `ACTIVE` -> `REMOVED`, reason `unbanned`, audit `ban.removed/success`;
- `remove` from `ABSENT` or `REMOVED` -> same state, reason `no_active_ban`, no audit.

The expected result must not call production ban helpers.

## System Under Test

Each state-machine example creates:

- one isolated temporary SQLite `Database`;
- one enabled local community owned by Discord user `111` in guild `10`;
- one remote actor handle `alice@example.com`;
- explicit settings and `BridgePolicyService`;
- real `BanUserInput` and `UnbanUserInput` operation calls.

The machine may reuse focused setup helpers from the existing ban pilot only when they do not hide model expectations.

## Rules and Invariants

### Generated rules

- `create_ban` executes the real ban operation and checks the transition-specific public result.
- `remove_ban` executes the real unban operation and checks the transition-specific result.
- `query_effective_ban` calls `UserBanService.check_activitypub_actor` without changing model state.
- `attempt_enforcement` verifies active model state denies and other states allow.

### Invariants after every step

- at most one persisted row exists for the community/actor key;
- row status matches model state (`none`, `active`, or `inactive`);
- effective ban decision matches whether model state is `ACTIVE`;
- persisted actor handle remains canonical and unchanged;
- cumulative management audit action/result sequence exactly matches the independent model list;
- repeated invalid transitions create no extra rows or audits.

## Hypothesis Settings

Use `run_state_machine_as_test` or the standard `TestCase` integration with bounded settings appropriate for SQLite:

- development: approximately 20 examples and 15 steps;
- CI: approximately 50 examples and 25 steps;
- no deadline;
- failures must shrink to a minimal action sequence.

If profile-specific stateful counts cannot be expressed cleanly through existing profiles, define one focused settings helper in the test module rather than changing global property-test semantics.

## TDD and Implementation Steps

1. Write the independent model and one create/remove invariant.
2. Add real setup and operation execution.
3. Add repeated create/remove transition checks.
4. Add persistence/audit invariants.
5. Add effective-enforcement query rules.
6. Run under bounded development and CI settings and inspect generated/shrunk output.
7. Run existing ban pilot, property suite, all repository tests, compile/diff checks, reports, and gateway checks.

## Touched Files

- `docs/development/test-assurance.md`

## New Files

- `plans/114_test_assurance_stage_7_ban_state_machine.md`
- `tests/stateful/__init__.py`
- `tests/stateful/test_ban_lifecycle_state_machine.py`

## Exit State

Stage 7 is complete only when:

- generated sequences exercise all three model states and repeated transitions;
- model state remains independent from repository state;
- persistence, audit, idempotency, and enforcement are checked after every step;
- failures are reproducible and shrinkable;
- named scenario tests remain intact;
- full repository checks pass;
- the plan is committed and a complete verified bundle is produced.

## Handoff

Stage 8 may use the stable typed dimensions and entry-point boundaries from Stages 2–5, but it must not fold this lifecycle state machine into a combinatorial Cartesian product. Stateful and interaction coverage remain separate assurance layers.
