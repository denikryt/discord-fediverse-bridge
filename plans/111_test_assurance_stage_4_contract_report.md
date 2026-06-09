# 111 — Test Assurance Stage 4: Ban Contract Coverage Report

## Problem / Goal

Typed ban cases are executable and complete-effect assertions are standardized, but maintainers still cannot mechanically see which declared ban rules and dimensions are represented or how each case finished. A handwritten matrix would duplicate and drift from tests.

This stage adds a passive deterministic report generated from the typed cases and pytest outcomes. It must not change test semantics, infer expectations from production logic, generate combinations, or fail CI merely because a declared rule is missing.

## Expected Behavior

A developer command runs only the typed ban pilot and writes canonical JSON containing:

- every collected case ID and declared dimensions;
- pass, fail, skip, or xfail status;
- represented values and selected dimension combinations;
- a separately declared required-rule inventory;
- represented and missing required rules;
- concise totals.

The command returns pytest's exit status. Missing contract rules are reported as data and do not alter the exit code in this stage.

## Architecture

### Required rules

Extend `tests/support/ban_contracts.py` with immutable `BanRequiredRule` declarations. Required rules refer to stable case IDs but remain separate from executable case definitions, allowing the report to expose a missing declaration without production introspection.

### Passive collector

Create `tools/ban_contract_report.py`. It invokes `pytest.main()` for `tests/operations/test_ban_contract_cases.py` with a small in-process collector object. The collector uses standard pytest hooks to read `item.callspec.params["case"]` during collection and record call-phase status. Tests do not write files and do not know the output path.

After pytest returns, the CLI builds and writes sorted JSON to `.artifacts/test-assurance/ban-contract/report.json` by default. Reporting is separate from assertions and cannot turn a passing test into a failure.

### Report shape

```json
{
  "domain": "ban_management",
  "summary": {
    "required_rules": 12,
    "represented_rules": 12,
    "missing_rules": 0,
    "statuses": {"passed": 12, "failed": 0, "skipped": 0, "xfailed": 0}
  },
  "represented_values": {...},
  "represented_combinations": {...},
  "required_rules": [...],
  "cases": [...]
}
```

Selected combinations are `caller_role × scope`, `action × existing_ban_state`, and `community_state × target_kind`. This is factual representation only, not combinatorial completeness.

## Touched Files

- tests/support/ban_contracts.py
- docs/development/test-assurance.md

## New Files

- tools/ban_contract_report.py
- tests/test_ban_contract_report.py
- plans/111_test_assurance_stage_4_contract_report.md

## Implementation Steps

1. Add failing pure tests for deterministic report construction, status totals, represented values/combinations, and missing-rule detection.
2. Declare required ban rules separately from executable cases.
3. Implement the small pytest collector and pure report builder.
4. Add the CLI and deterministic JSON writer under the existing ignored assurance-artifact root.
5. Generate a real report from the current typed pilot and verify all declared rules are represented and passing.
6. Document invocation, report semantics, and the fact that missing rules do not fail tests yet.
7. Run focused report tests, typed pilot, full Python suite, compile checks, gateway checks, and diff checks.

## Tests

Pure tests must create synthetic case results including pass/fail/skip/xfail and an intentionally absent required rule. They must verify stable ordering and no reliance on production policy code.

A real command must run successfully:

```bash
.venv/bin/python tools/ban_contract_report.py \
  --output .artifacts/test-assurance/ban-contract/report.json
```

The generated report must contain every `BAN_CONTRACT_CASES` ID, all required rules, zero current missing rules, and passed status for every current pilot case.

## Boundary and Handoff

This stage reports only the typed ban pilot. It does not require metadata on historical tests, create Cartesian products, infer expected behavior, fail CI for gaps, or generalize a cross-domain framework. Stage 5 may review factual gaps and test a second domain.

## Regression and Blind-Spot Analysis

- Collection metadata must come from typed case objects, not parsing display names.
- Setup failures and skips must still produce a report entry.
- Xfail status must not be misreported as a normal pass.
- Required rules must be separate from collected cases or gaps can never appear.
- Generated JSON must be deterministic and ignored by Git.
- Report generation must preserve pytest's original exit code.

## Open Questions

None.
