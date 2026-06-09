# 118 — Formalized Migration Stage 1: Community Management

## Scope

Formalize the repeated local-community creation and edit authorization/status contracts without changing production behavior. Preserve registration, dashboard, and disabled-runtime narratives as named scenarios.

## Current Paths

- `src/operations/create_community.py` owns validated creation and persistence.
- `src/operations/edit_community.py` owns guild context, accessibility, owner/super-admin authorization, metadata validation, status changes, persistence, and audit behavior.
- Command adapters retain Discord modal/context behavior.
- Dashboard and disabled-community runtime tests remain narrative because their transport and multi-step effects do not share the operation result shape.

## Classification

- Typed cases: repeated create/edit operation decision tables in registration, edit-operation, and audit tests.
- Named scenarios: registration lifecycle, command modal behavior, dashboard flows, and disabled inbound/runtime behavior.
- Generated assurance: none in this stage.
- Technical contracts: schema/audit repository tests remain unchanged.
- Duplicate removal: none; replacement equivalence will be measured before any later removal.

## Implementation

1. Add `tests/support/community_management_contracts.py` with stable case IDs, independent expected effects, and required rules.
2. Add `tests/support/community_management_effects.py` to snapshot operation result, persisted community state, and action-local audit events.
3. Add `tests/operations/test_community_management_contract_cases.py` executing real create/edit operations and SQLite repositories.
4. Cover creation success/validation, owner and super-admin edits, unauthorized/cross-guild/missing/guildless rejection, status disable/enable, metadata update, no-op, and invalid status.
5. Add `tools/community_management_contract_report.py` using the existing passive collector and canonical JSON output.
6. Update `docs/development/test-assurance.md` with domain scope, retained narratives, and report command.

## Observable Contract

Each case compares independently declared expectations with:

- `applied`, `reason`, and visible message class;
- persisted slug/display name/summary/status/owner/guild/forum state;
- action-local audit `(action, result, reason_code)` tuples.

Forbidden and validation cases must prove no unintended persistence mutation and only the audit rows required by the product contract.

## Boundaries

- No fanout or ActivityPub migration.
- No dashboard adapter unification.
- No generic domain registry or universal effects object.
- No production change unless a new regression test first exposes a defect.

## Tests and Verification

- Focused community contract and report tests.
- Existing community command, operation, behavior, dashboard, and permission tests.
- Full Python and gateway suites, compile, and diff checks.
- Deterministic report with zero missing declared rules.

## Handoff

Stage 1 adds one independent domain model and report. Stage 2 may compare metadata/adapter repetition, but must not rewrite these case inputs or expected effects merely to create a common hierarchy.
