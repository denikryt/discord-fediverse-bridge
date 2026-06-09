# Test assurance

This document owns the developer workflow for measuring and extending the test suite without treating coverage or generated reports as product specifications.

## Install development dependencies

```bash
uv sync --extra dev
```

## Existing test groups

Run the user-visible behavior scenarios:

```bash
.venv/bin/python -m pytest -q --durations=0 tests/behavior
```

Run Discord command and DiscordOps operation tests:

```bash
.venv/bin/python -m pytest -q --durations=0 tests/commands tests/operations
```

Run the remaining project tests without double-counting the specialized groups:

```bash
.venv/bin/python -m pytest -q --durations=0 \
  tests \
  --ignore=tests/behavior \
  --ignore=tests/commands \
  --ignore=tests/operations
```

Run the vendored DiscordOps framework tests:

```bash
.venv/bin/python -m pytest -q --durations=0 vendor/discordops/tests
```

## Generate the measurable baseline

```bash
.venv/bin/python tools/test_assurance_baseline.py \
  --output-dir .artifacts/test-assurance/baseline
```

The command first records collected pytest node IDs, then executes each group with branch coverage and complete duration reporting. Generated files are ignored by Git:

```text
.artifacts/test-assurance/baseline/summary.json
.artifacts/test-assurance/baseline/<group>/nodeids.txt
.artifacts/test-assurance/baseline/<group>/collect.log
.artifacts/test-assurance/baseline/<group>/pytest.log
.artifacts/test-assurance/baseline/<group>/coverage.json
```

Run one group while developing the tooling:

```bash
.venv/bin/python tools/test_assurance_baseline.py --group behavior
```

## Collection-only inventory

The baseline tool stores this automatically. For a direct inspection:

```bash
.venv/bin/python -m pytest --collect-only -q tests/behavior
```

Collection output is an inventory of executable node IDs. It does not explain the product rule represented by each test.

## Interpreting the baseline

Branch coverage and duration are diagnostic evidence:

- an uncovered branch is a measured fact, not proof of a missing product scenario;
- a covered branch is not proof that assertions would detect incorrect behavior;
- group runtime helps decide which later assurance layers belong in the fast local loop;
- generated reports must be regenerated rather than edited manually.

No project-wide coverage threshold is defined from this baseline.

## Initial pilot domain

Ban management is the first bounded contract-formalization pilot. It has explicit role, scope, community-state, target-resolution, persistence, audit, and runtime-enforcement behavior across existing behavior, command, and operation tests.

## Ban contract pilot

The first typed contract pilot lives in:

```text
tests/support/ban_contracts.py
tests/operations/test_ban_contract_cases.py
```

`BanContractCase` is intentionally domain-specific. Each case declares stable machine-readable dimensions and an explicit expected result, while the harness executes the real ban or unban operation against SQLite persistence. The expected result never calls production policy code.

The pilot covers a bounded authorization and lifecycle subset. Existing named command, audit, and runtime-enforcement scenarios remain the source of clearer transport-specific behavior and are not required to migrate into the typed model.

## Ban observable effects

The pilot collects operation output, persisted active/inactive ban rows, resolved local Discord identity, and management audit actions after each real operation. `tests/support/ban_effects.py` keeps these assertions ban-specific and compares fields separately so failures identify the missing effect. Rejected and validation cases explicitly require the absence of unplanned rows and audit events.

## Generate the ban contract report

```bash
.venv/bin/python tools/ban_contract_report.py \
  --output .artifacts/test-assurance/ban-contract/report.json
```

The CLI runs only the typed ban pilot and passively records each case ID, declared dimensions, pytest status, represented values, selected combinations, and separately declared required rules. The JSON report is generated and ignored by Git. Missing rules are visible facts but do not change pytest's exit code in this stage.
