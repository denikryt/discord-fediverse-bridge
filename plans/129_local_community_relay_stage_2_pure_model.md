# 129 — Local-Community Relay Stage 2 Pure Model

## Problem / Goal

Add an independent relay transition model and prove it against the real fanout with fixed readable sequences before generated exploration.

## Expected Behavior

The model and SUT must agree on source creation, target selection, pending/failed retry eligibility, attempt counts, errors, relay activity IDs, policy-denied targets, duplicate successful calls, and missing source JSON.

## Architecture

Create `tests/model_based/local_community_relay_model.py` with relay-specific immutable action/outcome inputs and a mutable `RelayModel`. The model receives allowed targets and gateway outcomes directly; it does not call production repositories, policy evaluators, renderers, or fanout code.

Create fixed model-vs-SUT tests using `LocalCommunityRelayHarness`. Assertions compare each observable field separately.

## Touched Files

- tests/support/local_community_relay.py
- docs/development/test-assurance.md

## New Files

- tests/model_based/__init__.py
- tests/model_based/local_community_relay_model.py
- tests/model_based/test_local_community_relay_sequences.py

## Implementation Steps

1. Define model delivery/source state and pure create/retry transition.
2. Unit-test all-success, mixed, retry, duplicate delivered, denied target, and missing source transitions.
3. Add at least five fixed sequences against the real SUT.
4. Add field-level snapshot comparison helpers.
5. Run focused and full repository gates.

## Tests

Run `tests/model_based`, relay behavior modules, all required Python groups, compile/diff checks, and gateway tests.

## Regression and Blind-Spot Analysis

The model intentionally excludes ORM IDs, timestamps, payload rendering, and internal repository calls. Existing renderer and narrative tests remain authoritative. Model expectations must not call production code.

## Open Questions

None.
