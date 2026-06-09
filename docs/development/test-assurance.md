# Test assurance

This document owns the developer workflow for measuring and extending the test suite without treating coverage or generated reports as product specifications.

## Main commands

Install Python and gateway development dependencies once:

```bash
uv sync --extra dev
cd fedify-gateway && npm ci && cd ..
```

Run the complete Python test suite, including behavior, contract, property,
stateful, and model-based tests:

```bash
.venv/bin/python -m pytest -q
```

Run the complete Fedify gateway TypeScript suite:

```bash
cd fedify-gateway && npm test
```

Run the complete relay exploration layer only:

```bash
HYPOTHESIS_PROFILE=dev .venv/bin/python -m pytest -q \
  tests/model_based \
  tests/stateful/test_local_community_relay_state_machine.py \
  tests/behavior/test_local_community_relay_resilience_scenarios.py
```

Generate the aggregate assurance report after the underlying domain reports
have been generated:

```bash
.venv/bin/python tools/aggregate_assurance_report.py
```

## Extended commands

Run only user-visible behavior scenarios:

```bash
.venv/bin/python -m pytest -q tests/behavior
```

Run only command and operation contracts:

```bash
.venv/bin/python -m pytest -q tests/commands tests/operations
```

Run property-based invariants with the normal local budget:

```bash
HYPOTHESIS_PROFILE=dev .venv/bin/python -m pytest -q tests/property
```

Run all stateful generators with the normal local budget:

```bash
HYPOTHESIS_PROFILE=dev .venv/bin/python -m pytest -q tests/stateful
```

Run the heavier CI budgets for property and stateful tests:

```bash
HYPOTHESIS_PROFILE=ci .venv/bin/python -m pytest -q tests/property tests/stateful
```

Generate branch-coverage and duration artifacts for the main Python groups:

```bash
.venv/bin/python tools/test_assurance_baseline.py \
  --output-dir .artifacts/test-assurance/baseline
```

Generate the migration-completeness inventory for Python and gateway tests:

```bash
.venv/bin/python tools/migration_completeness_report.py
```

Generate one domain contract report while working on that domain:

```bash
.venv/bin/python tools/ban_contract_report.py
.venv/bin/python tools/bridge_policy_contract_report.py
.venv/bin/python tools/community_management_contract_report.py
```

Use the sections below only when working on a specific test layer or report.

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

