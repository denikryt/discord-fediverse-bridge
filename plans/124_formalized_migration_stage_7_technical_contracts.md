# 124 — Formalized Migration Stage 7: Technical Contracts

## Problem / Goal

Dashboard, configuration, deployment, backup, schema-cleanup, OAuth, and Fedify gateway tests are correctness-critical technical contracts, but they are not product decision tables and should not be forced into typed authorization cases. This stage assigns stable machine-readable rule ownership, preserves native Python and TypeScript execution, and produces one aggregate technical-contract report.

## Expected Behavior

- Existing Python technical tests keep their current bodies and assertions.
- Existing TypeScript gateway verification scripts keep running under `tsx`.
- Every declared technical rule has one or more executable owners.
- A resumable gateway runner records TypeScript check and per-script pass/fail status in JSON without changing test semantics.
- A unified report combines Python and gateway evidence and reports missing rule IDs.
- No production behavior changes.

## Architecture

Add a domain-specific technical-contract manifest with stable rule IDs and native owner identifiers. Python owners are pytest node prefixes. Gateway owners are verification script paths plus the TypeScript compiler check. A Python report tool executes Python owners through pytest, reads the gateway status artifact, and renders one canonical JSON report.

The gateway runner remains a developer tool, not a framework dependency. It supports chunking so long native suites can be resumed without treating command time limits as test failures.

## Test Classification

- Dashboard, backup, OAuth, public URL, deployment, and schema tests: category D, infrastructure/technical contracts.
- Gateway verification scripts: category D, native TypeScript technical contracts.
- No typed product cases are introduced.
- No existing tests are removed or rewritten.

## Boundary Table

| Area | This stage | Preserved / deferred |
|---|---|---|
| Python technical tests | Stable rule ownership and report participation | Existing assertions and fixtures |
| Gateway tests | Native status collection and report participation | TypeScript execution and payload assertions |
| Reporting | Unified high-level JSON | No shared execution framework |
| Framework | Domain-specific manifest/tools only | Generic registry deferred to Stage 8 |

## Touched Files

- docs/development/test-assurance.md

## New Files

- plans/124_formalized_migration_stage_7_technical_contracts.md
- tests/support/technical_contract_manifest.py
- tests/test_technical_contract_report.py
- tools/gateway_contract_runner.py
- tools/technical_contract_report.py

## Implementation Steps

1. Declare stable technical rule IDs for dashboard, backup, OAuth, public URL, deployment, schema cleanup, TypeScript compilation, and gateway verification families.
2. Add pure report-builder tests proving missing rules and mixed Python/gateway statuses are represented correctly.
3. Implement a gateway runner that discovers `verify-*.ts`, optionally runs a deterministic chunk, merges results into an ignored JSON artifact, and preserves prior successful results.
4. Implement a technical report tool that runs Python owner files, reads gateway status, and emits `.artifacts/test-assurance/technical-contracts/report.json`.
5. Document native execution and report regeneration commands.
6. Run focused tests, all Python groups, TypeScript and all gateway verification scripts, compile, and diff checks.

## Tests

- Unit test unique rule IDs and non-empty owners.
- Unit test missing-rule detection.
- Unit test gateway artifact merge semantics.
- Run all selected Python technical owners through the report tool.
- Run all gateway scripts and TypeScript check through the native runner.
- Run the full repository suite.

## Regression and Blind-Spot Analysis

The report proves representation only for declared rules. It does not infer unknown technical requirements. Chunked gateway execution must never convert an unrun script into a pass, and stale artifact entries must be invalidated when the discovered script set changes.

## Open Questions

None.
