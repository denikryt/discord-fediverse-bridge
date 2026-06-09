# Local-Community Relay Model Pilot Evaluation

## Scope

The pilot exercises the real `LocalCommunityFederationFanout`, SQLite relay
repositories, policy service, renderer, and gateway boundary. The independent
model tracks only source existence, per-target delivery state, accepted
subscribers, delivered-create history, and operation-specific mutation state.

## Exploration budgets

- Create/retry state machine:
  - development: 20 examples × 15 steps;
  - CI: 30 examples × 20 steps.
- Target churn: 30 generated action lists.
- Update/delete continuity: 3 generated action lists with up to 6 actions.
- Boundary failures: 4 generated examples plus three named regressions.

The original CI state-machine budget of 75 × 30 exceeded the normal execution
window on the real SQLite path. Reducing it to 30 × 20 preserved the same action
set, invariants, shrinking, and deterministic execution while completing in
about 15 seconds.

## Measured runtime

- `tests/model_based`: 12 tests in about 14 seconds, about 16 seconds wall time.
- CI relay create/retry state machine at 30 × 20: about 14 seconds, about
  15.5 seconds wall time.
- Focused resilience plus model suite remains suitable for targeted CI, but is
  intentionally not added to every smallest local edit loop.

## Findings

No confirmed production logic defect was found by generated sequences.

The pilot did find and correct one model-oracle mistake: the origin subscriber
must use the same actor ID as `event.actor_id`; otherwise production correctly
selects it as a relay target.

The pilot also confirmed one operational limitation: gateway outcomes are
persisted one row at a time in separate repository sessions. A persistence
failure can leave earlier outcomes committed and later rows pending. This is
recorded in `notes/known_issues.md` rather than hidden behind an invented atomic
rollback expectation.

## Invariants now protected

- one source row per relay identity;
- one delivery row per source and target actor;
- delivered rows are never resent by the same source action;
- pending and failed rows remain retryable;
- each persisted outcome increments attempts exactly once;
- subscriber and policy changes affect new source actions, not historical rows;
- update/delete require delivered-create history and current accepted
  subscription;
- create, update, and delete keep independent source and delivery histories;
- renderer and gateway exceptions leave pending rows with zero attempts;
- partial outcome persistence follows the actual per-row durability boundary.

## Maintenance cost

The pilot adds approximately 1,150 lines across one domain harness, one pure
model, four generated/fixed model modules, and one state machine. Most of that
code is explicit setup and field-level comparison against real persistence.
The size is acceptable for this bounded domain but does not justify a universal
state-machine framework.

## Relationship to named scenarios

Named resilience and protocol scenarios remain authoritative for readable
product narratives and payload details. Generated tests add transition-order,
repetition, subscriber churn, retry, and boundary exploration; they do not
replace those scenarios.

## Failure readability and determinism

Hypothesis failures retain reproducible seeds and shrink action sequences. The
pilot uses no random sleeps or custom unseeded randomness. Deterministic gateway
plans and isolated SQLite databases make failures reproducible.

## Decision

Retain the relay model pilot. Do not generalize it into a shared model framework
now. The measured value is stronger transition and durability coverage plus one
confirmed operational limitation, but not enough evidence for universal model
infrastructure.

The next justified pilot is `EventReceiptRepository` and inbound
outcome/dedup lifecycle. It should receive a separate plan and may reuse only
three proven concepts:

- observable durable-state snapshots;
- deterministic outcome/failure controllers;
- small independent transition result types.

Community-sync backfill and subscription lifecycle remain later candidates.
