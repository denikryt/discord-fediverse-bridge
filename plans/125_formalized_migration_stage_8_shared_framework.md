# 125 — Formalized Migration Stage 8: Minimal Shared Assurance Framework

## Problem / Goal

Completed domains repeat two stable reporting patterns: typed case families collected through pytest parametrization, and named/generated owner manifests collected by node prefix. The duplication now spans more than three domains and makes report behavior inconsistent. This stage extracts only those proven mechanics and adds aggregate reporting while preserving domain case/effect schemas and test execution paths.

## Expected Behavior

- Existing case IDs, rule IDs, report domains, and report semantics remain stable.
- Typed case reports share one rule-representation builder and passive collector wiring.
- Owner-manifest reports share one node-prefix collector and report builder.
- Domain scripts retain responsibility for domain-specific dimensions and output locations.
- An aggregate JSON report summarizes generated domain reports without becoming a test runtime dependency.
- No production behavior or test oracle changes.

## Architecture

Create `tools/assurance_reporting.py` with:

- `build_case_report(...)` for typed case families;
- `OwnerPrefixCollector` and `build_owner_report(...)` for named/generated owners;
- stable status aggregation and validation.

Migrate at least six existing report providers: community management, identity/discovery, subscription, fanout, inbound, and content lifecycle. Domain scripts supply case serializers or manifests; the shared layer knows nothing about domain inputs.

Create `tools/aggregate_assurance_report.py` to read existing JSON artifacts and emit a high-level registry-style summary. It does not run tests and does not require domains to inherit from common classes.

## Test Classification

- Existing domain tests remain unchanged.
- New tests are category D framework/reporting contract tests.
- No typed cases become generic base classes.

## Boundary Table

| Area | This stage | Preserved / deferred |
|---|---|---|
| Shared reporting | Proven collection/build mechanics | Domain schemas and effects |
| Domain scripts | Thin provider/wiring role | Existing output paths and IDs |
| Aggregate report | Artifact aggregation | Test execution orchestration |
| Metadata | Structural protocols only | Narrative metadata hooks deferred |

## Touched Files

- tools/community_management_contract_report.py
- tools/identity_discovery_contract_report.py
- tools/subscription_contract_report.py
- tools/fanout_contract_report.py
- tools/inbound_contract_report.py
- tools/content_lifecycle_contract_report.py
- docs/development/test-assurance.md

## New Files

- plans/125_formalized_migration_stage_8_shared_framework.md
- tools/assurance_reporting.py
- tools/aggregate_assurance_report.py
- tests/test_assurance_reporting.py

## Implementation Steps

1. Add pure shared builders and collectors with readable validation errors.
2. Write tests for typed-case rule gaps, owner-prefix gaps, duplicate IDs, and aggregate summaries.
3. Migrate community, identity, and subscription reports to `build_case_report` while preserving domain-specific dimensions.
4. Migrate fanout, inbound, and content reports to `OwnerPrefixCollector` and `build_owner_report`.
5. Add aggregate artifact reporting over all known generated domain reports.
6. Regenerate migrated reports and compare domains, rule counts, missing IDs, and case/owner statuses.
7. Document extension rules and run full repository validation.

## Tests

- Pure unit tests for all shared functions.
- Existing report tests remain green.
- Direct execution of all six migrated report tools.
- Aggregate report generation from their artifacts.
- Full Python and gateway suites, compile, and diff checks.

## Regression and Blind-Spot Analysis

A shared builder may accidentally normalize away domain fields or reorder stable IDs. Domain serializers remain explicit and report comparisons verify semantic equivalence. Aggregation reports facts from existing artifacts and must not infer completeness beyond each provider's declared rules.

## Open Questions

None.
