# 110 — Test Assurance Stage 3: Ban Observable Effects

## Problem / Goal

The typed ban pilot verifies operation status and row counts, but critical cases do not consistently assert audit effects, row identity fields, or the absence of prohibited mutations. This stage introduces a ban-specific observable-effects snapshot and readable assertion helper.

## Expected Behavior

Each typed pilot case verifies operation outcome, reason, persisted ban rows, target Discord identity, and audit actions/results. Rejections explicitly prove that no unexpected active/inactive rows or success audits were created. Successful mutations prove the corresponding success audit.

## Architecture

Create `tests/support/ban_effects.py` with immutable row/audit/observed dataclasses, a collector over public repository APIs, and an assertion function with field-specific assertions. The helper remains ban-specific and does not inspect private implementation state.

Extend `BanExpected` with expected audit action/result tuples. The operation test collects effects after the real action and delegates comparisons to the readable helper.

## Touched Files

- tests/support/ban_contracts.py
- tests/operations/test_ban_contract_cases.py
- docs/development/test-assurance.md

## New Files

- tests/support/ban_effects.py
- tests/test_ban_effect_assertions.py
- plans/110_test_assurance_stage_3_ban_observed_effects.md

## Implementation Steps

1. Add failing focused tests for effect collection and readable mismatch behavior.
2. Implement ban row, audit row, and operation outcome snapshots.
3. Add explicit expected audit actions/results to every pilot case.
4. Replace partial inline pilot assertions with one ban-specific assertion helper that still reports each field separately.
5. Preserve existing named audit tests and runtime scenarios.
6. Document the observable-effects layer and its domain boundary.
7. Run focused, ban, full Python, compile, gateway, and diff checks.

## Tests

Verify successful create/reactivate/remove cases require the matching audit action; authorization and disabled-community failures require forbidden audits; validation, duplicate, and no-active-ban cases require no audit. Local-target success must retain Discord identity. Assertion-helper failures must identify the mismatched field.

## Boundary and Handoff

This stage standardizes pilot observations only. It does not add report hooks, metadata collection, generated combinations, or a universal bridge effects object. Stage 4 may consume the case declarations and pytest results without changing effect semantics.

## Regression and Blind-Spot Analysis

The collector must use persisted public repository data after the action. Setup audit rows must not be confused with action audit rows. Negative cases must assert absence rather than merely checking `applied=False`. Generic equality must not hide which effect differed.

## Open Questions

None.
