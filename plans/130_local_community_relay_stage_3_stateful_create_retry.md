# 130 — Local-Community Relay Stage 3 Stateful Create/Retry

## Problem / Goal

Generate repeated create/retry sequences for one fixed relay source and discover ordering combinations not covered by named examples.

## Expected Behavior

One source row exists, one row per target exists, delivered targets are never retried, pending/failed targets remain eligible, attempts increment once per returned outcome, and SUT state always matches the independent model.

## Architecture

Add a relay-specific Hypothesis `RuleBasedStateMachine` using the real SQLite harness and pure `RelayModel`. Rules choose one of four deterministic gateway plans and invoke the same source action repeatedly. Async fanout calls are driven with `asyncio.run` inside state-machine rules.

## Touched Files

- docs/development/test-assurance.md

## New Files

- tests/stateful/test_local_community_relay_state_machine.py

## Implementation Steps

1. Build one isolated harness per generated example.
2. Define explicit all-success, mixed, and all-failed plans.
3. Add relay and inspect rules.
4. Compare source count, unique target rows, statuses, attempts, errors, activity IDs, and call eligibility after every step.
5. Use 20x15 dev and 75x30 CI budgets and measure runtime.

## Tests

Run focused stateful/model tests and all required repository gates.

## Regression and Blind-Spot Analysis

The machine keeps subscribers, policy, event identity, and source JSON fixed. Those dimensions remain Stage 4 work. No custom randomness or sleeps are allowed.

## Open Questions

None.
