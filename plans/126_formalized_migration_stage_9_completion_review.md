# 126 — Formalized Migration Stage 9: Completion Review

## Problem / Goal

After domain migrations and shared framework extraction, the repository still lacks one deterministic inventory proving that every executable Python and gateway test has been reviewed and assigned a migration classification. This stage produces an honest completion artifact, documents intentional non-migration, and removes no tests unless exact redundancy is proven.

## Expected Behavior

- Every collected project Python test, vendored DiscordOps test, and gateway verification script appears exactly once in the migration report.
- Each item has a domain, classification A–E, migration status, report participation flag, and stable contract identifier.
- Typed, generated, narrative, and technical tests remain distinguishable.
- Unknown/unreviewed count is zero.
- No test is deleted without evidence; this stage expects no deletion unless the inventory proves exact duplication.
- Production behavior is unchanged.

## Architecture

Add a pure classifier under `tests/assurance/migration_inventory.py`. It classifies by stable test ownership boundaries established by the completed stages, not by runtime production logic. A report tool collects pytest node IDs without executing tests, discovers gateway scripts, applies the classifier, validates one-to-one coverage, and emits canonical JSON.

The classifier intentionally treats readable behavior and historical phase tests as named scenarios, generated assurance directories as category C, typed case modules as category A, and remaining unit/integration/gateway checks as category D. Category E remains empty because no exact duplicate has been proven.

## Test Classification

This stage classifies the entire suite; its own tests are category D technical contracts.

## Boundary Table

| Area | This stage | Preserved / deferred |
|---|---|---|
| Inventory | All Python and gateway executable tests | Helpers that are not collected tests |
| Classification | A–E with domain and status | No forced typed migration |
| Deduplication | Evidence review only | No speculative deletion |
| Reporting | Deterministic JSON completeness artifact | No mutation testing or CI threshold |

## Touched Files

- docs/development/test-assurance.md

## New Files

- plans/126_formalized_migration_stage_9_completion_review.md
- tests/assurance/migration_inventory.py
- tests/test_migration_completeness_report.py
- tools/migration_completeness_report.py

## Implementation Steps

1. Define stable path/node classification rules and domain inference for all current test families.
2. Add pure tests for typed, generated, narrative, technical, vendor, and gateway classifications.
3. Collect all Python node IDs from `tests/` and `vendor/discordops/tests/` without executing them.
4. Discover all `fedify-gateway/tests/verify-*.ts` scripts.
5. Validate uniqueness, classify every item, and emit counts plus detailed rows.
6. Assert unknown/unreviewed is zero and category E remains empty unless a proven duplicate is documented.
7. Document interpretation and regeneration.
8. Run the report, full Python and gateway suites, compile, and diff checks.

## Tests

- Unit tests for every classification branch and stable contract ID generation.
- Report-builder tests for duplicate and unknown detection.
- Real collection report with zero unknown/unreviewed.
- Full repository validation.

## Regression and Blind-Spot Analysis

Path-based classification describes test architecture, not semantic completeness. The report must state that category B and D preserve current form intentionally and that zero unknown does not prove all product rules are known. Renaming files requires updating classification tests and regenerating the artifact.

## Open Questions

None.
