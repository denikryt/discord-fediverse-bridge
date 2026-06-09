# 134 — Local-Community Relay Stage 7 Evaluation

## Problem / Goal

Measure the completed relay model pilot and decide whether it should be retained, expanded, or stopped.

## Scope

- Record generated budgets and measured runtimes.
- Record production defects, model/oracle defects, and operational limitations found.
- Record protected invariants, overlap with named scenarios, maintenance size, and failure readability.
- Adjust only exploration budgets that proved unsuitable for normal CI.
- Decide the next expansion domain without creating a generic model framework.

## Touched Files

- tests/stateful/test_local_community_relay_state_machine.py
- docs/development/test-assurance.md

## New Files

- docs/development/local-community-relay-model-evaluation.md

## Implementation Steps

1. Measure focused model-based and stateful runtime.
2. Reduce the CI stateful budget if the real SQLite path exceeds the execution window.
3. Document budgets, findings, invariants, maintenance cost, overlap, and determinism.
4. Record a retain/expand/stop decision.
5. Run focused and full repository gates.

## Decision Boundary

No shared model framework is authorized. A future EventReceiptRepository pilot may reuse only the proven ideas of observable snapshots, deterministic outcome controllers, and small pure transition types.
