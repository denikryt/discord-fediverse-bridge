# 128 — Local-Community Relay Stage 1 Harness

## Problem / Goal

The remote-fanout behavior module mixes payload narratives with relay resilience setup. Extract a relay-specific support harness and move the three resilience scenarios into a focused module without changing production behavior.

## Expected Behavior

- Existing relay behavior remains unchanged.
- Policy-read failure still occurs before persistence and transport.
- Partial failure still retries only failed targets.
- An in-flight action keeps one policy snapshot and the next action sees policy changes.
- Existing payload-shape and update/delete scenarios remain in the original behavior module.

## Architecture

Add `tests/support/local_community_relay.py` with a domain-specific `LocalCommunityRelayHarness`, deterministic gateway outcome planning, event construction, subscriber mutation, and observable source/delivery snapshots. The harness calls the real `LocalCommunityFederationFanout` through the real runtime and SQLite repositories.

Move only the three Stage 9 resilience tests to `tests/behavior/test_local_community_relay_resilience_scenarios.py`. Do not add a model or generic framework yet.

## Touched Files

- tests/behavior/test_local_community_remote_fanout_scenarios.py
- tests/support/fanout_contract_manifest.py
- docs/development/test-assurance.md

## New Files

- tests/support/local_community_relay.py
- tests/behavior/test_local_community_relay_resilience_scenarios.py

## Implementation Steps

1. Add the relay-specific harness and deterministic gateway controller.
2. Add source/delivery observation helpers with explicit dataclasses.
3. Move the three resilience scenarios to the focused module and rewrite setup through the harness.
4. Remove the moved tests and now-unused imports from the original module.
5. Update fanout rule ownership node IDs and developer documentation.
6. Verify before/after behavior and full repository tests.

## Tests

Run the focused resilience module, original remote-fanout module, all required Python groups, compile/diff checks, and gateway tests.

## Regression and Blind-Spot Analysis

The refactor must not alter payload rendering, target selection, repository semantics, or gateway behavior. The gateway controller is deterministic and only replaces repeated AsyncMock side effects. No production code changes are allowed.

## Open Questions

None.
