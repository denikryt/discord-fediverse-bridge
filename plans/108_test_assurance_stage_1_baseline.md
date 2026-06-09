# 108 — Test Assurance Stage 1: Measurable Baseline

## Problem / Goal

The repository has a large passing pytest suite, but there is no reproducible command set that separates the existing broad test groups, records their runtime, measures branch coverage for the policy-critical pilot modules, or inventories collected node IDs as generated data. Maintainers therefore know the total test count but cannot compare group cost, identify unexecuted policy branches, or establish a stable baseline for later assurance stages.

This stage establishes baseline visibility only. It must not change production behavior, reinterpret test semantics, convert tests into contract cases, or impose coverage thresholds.

## Expected Behavior

After this stage:

- development dependencies include branch-capable pytest coverage tooling;
- one tracked CLI can collect and run the four established groups:
  - behavior: `tests/behavior`;
  - command/operation: `tests/commands` and `tests/operations`;
  - project: remaining tests under `tests/`, excluding the three directories above;
  - vendored DiscordOps: `vendor/discordops/tests`;
- each run writes generated artifacts outside tracked source files:
  - collected node IDs per group;
  - pytest output with durations;
  - branch-coverage JSON per group for the selected policy-critical modules;
  - one deterministic summary JSON containing group status, test counts, elapsed time, and artifact paths;
- the CLI exits non-zero when collection or a test group fails;
- the baseline report records facts only. It does not call uncovered branches defects, infer missing behavior, or enforce percentages;
- the initial next-stage pilot is documented as ban management;
- all existing test and production behavior remains unchanged.

Example local invocation:

```bash
.venv/bin/python tools/test_assurance_baseline.py --output-dir .artifacts/test-assurance/baseline
```

Example summary fragment:

```json
{
  "groups": {
    "behavior": {
      "collected": 155,
      "exit_code": 0,
      "elapsed_seconds": 42.1,
      "coverage_json": "behavior/coverage.json"
    }
  },
  "pilot_domain": "ban_management"
}
```

## Architecture

### Baseline CLI

Create `tools/test_assurance_baseline.py` as a small orchestration CLI. It owns static group definitions and subprocess execution only; it is not a pytest plugin and does not interpret test meaning.

The CLI will:

1. resolve the project root from its own location;
2. create the configured output directory;
3. for each group, run `pytest --collect-only -q` and store stdout as `nodeids.txt`;
4. count collected node IDs from collection output using a narrow parser tested against pytest output;
5. run the group with `pytest-cov`, branch measurement, `--durations=0`, and group-specific coverage output;
6. record wall-clock elapsed time and process status;
7. write one sorted, indented `summary.json` after every completed group and again at the end so partial failures remain diagnosable;
8. stop after a failed group and return its non-zero status.

The script must use `.venv/bin/python -m pytest` when run from the repository checkout through the active interpreter, not assume a globally installed `pytest` executable.

### Test-group boundaries

The exact group selectors are:

```text
behavior:
  tests/behavior

command_operation:
  tests/commands
  tests/operations

project:
  tests
  --ignore=tests/behavior
  --ignore=tests/commands
  --ignore=tests/operations

vendor_discordops:
  vendor/discordops/tests
```

These groups reflect the current repository structure. They do not claim semantic completeness and may overlap in imported production modules, but their test node IDs must not overlap.

### Coverage scope

Coverage collection targets the package/module sources exercised by each group while the summary highlights these Stage 1 policy-critical modules:

```text
src/bridge_policy.py
src/federation_policy.py
src/local_community_permissions.py
src/user_bans.py
src/operations/common_preconditions.py
src/operations/ban_user.py
src/operations/unban_user.py
```

The CLI will generate ordinary pytest-cov JSON with branch data. A small summary extractor will copy per-module statement/branch totals for the listed modules into `summary.json` when present. Missing modules are represented as not exercised, not silently omitted.

No minimum percentage is configured.

### Generated artifacts

Use `.artifacts/test-assurance/` as the default generated root and add it to `.gitignore`. Reports are regenerated and never manually edited or committed.

Per group:

```text
.artifacts/test-assurance/baseline/<group>/nodeids.txt
.artifacts/test-assurance/baseline/<group>/collect.log
.artifacts/test-assurance/baseline/<group>/pytest.log
.artifacts/test-assurance/baseline/<group>/coverage.json
```

Shared:

```text
.artifacts/test-assurance/baseline/summary.json
```

### Documentation boundary

Add `docs/development/test-assurance.md`. Its responsibility is developer-facing execution and interpretation of assurance layers. Stage 1 documents:

- dependency installation;
- direct commands for each test group;
- the baseline CLI command;
- artifact locations;
- the distinction between branch coverage evidence and semantic correctness;
- ban management as the Stage 2 pilot.

Update `docs/development/navigation.md` only to link to this new developer document if its existing purpose includes test-navigation links. Do not add assurance details to deployment or federation documentation.

## Touched Files

- pyproject.toml
- uv.lock
- .gitignore
- docs/development/navigation.md

## New Files

- plans/107_test_assurance_umbrella.md
- tools/test_assurance_baseline.py
- tests/test_test_assurance_baseline.py
- docs/development/test-assurance.md

## Implementation Steps

1. Preserve the configured umbrella plan in the first stage commit with `git add -f plans/107_test_assurance_umbrella.md` so later stage plans and bundles contain their governing contract.
2. Add `pytest-cov` to the `dev` optional dependency and regenerate `uv.lock` with `uv sync --extra dev`.
3. Write failing tests for the baseline CLI’s pure contracts before the implementation:
   - exact group selectors;
   - node-ID counting from collection output;
   - policy-critical module extraction from coverage JSON;
   - deterministic summary serialization;
   - non-zero status propagation from a failed subprocess through an injected runner boundary.
4. Implement `tools/test_assurance_baseline.py` with module, class/dataclass, public-function, and non-trivial-method docstrings and intent comments required by `AGENTS.md`.
5. Keep subprocess execution behind a narrow callable so tests verify command construction and failure handling without recursively running the full repository suite.
6. Add `.artifacts/test-assurance/` to `.gitignore`.
7. Add `docs/development/test-assurance.md` with direct commands equivalent to the CLI:
   - behavior;
   - command/operation;
   - remaining project tests;
   - vendored DiscordOps;
   - collection-only inventory;
   - full baseline generation.
8. Add a navigation link from `docs/development/navigation.md` after confirming its document purpose.
9. Run the new tooling tests, then run the baseline CLI to produce real ignored artifacts from the current suite.
10. Inspect `summary.json` and coverage JSON for all four groups; record no subjective gap conclusions in tracked files.
11. Run all repository tests and required checks before commit.

## Tests

### Tooling behavior tests

`tests/test_test_assurance_baseline.py` must verify:

- group selectors exactly match the current directory partition;
- project-group selectors exclude behavior, command, and operation directories;
- collected-node parsing ignores pytest summary lines and retains parameterized node IDs;
- coverage extraction reports each configured critical module as measured or not exercised;
- summary JSON ordering is deterministic;
- a failing collection or test subprocess produces a non-zero CLI result and a partial diagnostic summary;
- no test invokes the full suite recursively.

### Baseline validation

Run:

```bash
.venv/bin/python tools/test_assurance_baseline.py \
  --output-dir .artifacts/test-assurance/baseline
```

Verify:

- all four groups collect successfully;
- node-ID files are present;
- all four groups pass;
- duration output is retained;
- branch-enabled coverage JSON exists per group;
- `summary.json` includes the critical-module measurements and `pilot_domain = "ban_management"`.

### Full repository validation

Run the repository’s complete pytest suite, Python compile checks, and `git diff --check`. No coverage threshold is applied.

## Boundary and Handoff

| Stage 1 changes | Stage 1 preserves | Later work left untouched | Stable handoff |
|---|---|---|---|
| Coverage dependency, group commands, generated baseline artifacts, developer documentation | All production behavior, test semantics, assertions, test organization | Typed ban cases, effect snapshots, contract reports, property/stateful/combinatorial/mutation testing | Stage 2 may rely on stable group names, baseline timings, branch data, and ban management as the selected pilot |

No temporary compatibility adapter or broken caller is handed forward. The baseline CLI is independently useful even if no later stage is implemented.

## Regression and Blind-Spot Analysis

- The project group must exclude the three specialized directories or tests will be double-counted.
- Coverage configuration must enable branches; statement-only reports do not satisfy this stage.
- Collection output parsing must not treat pytest’s final summary as a node ID.
- A baseline report must not label an uncovered branch as a defect or missing product rule.
- The tool must preserve partial artifacts on failure so a failed baseline is diagnosable.
- Generated files must remain ignored to prevent stale reports from becoming source-of-truth documents.
- The CLI must not introduce a custom pytest plugin or metadata model reserved for Stage 4.
- Runtime cost must be measured rather than assumed before future CI cadence decisions.

## Open Questions

None. The umbrella plan, repository layout, and existing development documentation provide the required decisions for this stage.
