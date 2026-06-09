# 131 — Local-Community Relay Stage 4 Target Churn

## Problem / Goal

Explore subscriber and policy changes between create actions while preserving durable per-source history.

## Expected Behavior

New source actions use the current accepted-subscriber and policy state. Existing source rows and delivery rows are never rewritten by later subscriber or policy changes. Duplicate calls for a delivered source create no new rows or transport calls.

## Architecture

Extend the relay harness with explicit subscriber and host-policy mutation helpers. Add a generated action-sequence test with an independent model containing accepted targets, blocked hosts, and one `RelayModel` per source identity. Use only successful gateway outcomes so this stage isolates target discovery from failure behavior.

## Touched Files

- tests/support/local_community_relay.py
- docs/development/test-assurance.md

## New Files

- tests/model_based/test_local_community_relay_target_churn.py

## Implementation Steps

1. Add idempotent accept/remove subscriber helpers.
2. Add explicit block/unblock host helpers through real policy persistence.
3. Generate short sequences of subscriber changes, policy changes, new source actions, and duplicate retry calls.
4. Compute allowed targets independently from model sets.
5. Compare every source's durable rows and all gateway batches after every action.
6. Run focused and full repository gates.

## Tests

Run model-based, relay behavior, all required Python groups, compile/diff checks, and gateway tests.

## Regression and Blind-Spot Analysis

This stage uses only delivered outcomes. It does not test update/delete continuity or failures. The model does not call production policy code.

## Open Questions

None.
