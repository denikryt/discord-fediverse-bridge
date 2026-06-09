# 132 — Local-Community Relay Stage 5 Update/Delete Continuity

## Problem / Goal

Explore the real update/delete continuity rule separately from create target discovery.

## Expected Behavior

Update/delete targets require both delivered create history and current accepted subscription. Unfollowed actors are skipped, re-followed actors are eligible again, failed operation deliveries retry independently, and create/update/delete keep separate source and delivery rows.

## Architecture

Extend the harness with mutation event construction. Add a `RelayContinuityModel` that owns delivered-create history, accepted subscribers, and independent `RelayModel` instances for update and delete source actions. A generated action list mutates subscription state and invokes/retries update or delete through the real `relay_update_or_delete` boundary.

## Touched Files

- tests/support/local_community_relay.py
- tests/model_based/local_community_relay_model.py
- docs/development/test-assurance.md

## New Files

- tests/model_based/test_local_community_relay_update_delete_continuity.py

## Implementation Steps

1. Add update/delete event builders sharing one object identity with the delivered create.
2. Add the independent continuity model.
3. Seed delivered create history through the real fanout.
4. Generate unfollow, re-follow, update, delete, and retry actions.
5. Compare operation-specific source rows, target rows, attempts, and gateway batches.
6. Verify create/update/delete histories remain separate.
7. Run focused and full repository gates.

## Tests

Run model-based, stateful, relay behavior, all required Python groups, compile/diff checks, and gateway tests.

## Regression and Blind-Spot Analysis

This model follows the current explicit repository contract: re-follow restores eligibility because historical delivered create rows remain durable and current subscription is accepted. Payload shape remains covered by existing narrative tests.

## Open Questions

None.
