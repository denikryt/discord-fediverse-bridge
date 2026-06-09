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

## Bridge-policy contract pilot

Stage 5 validates the contract pattern in a second bounded domain without creating a universal case or effects hierarchy. The domain-specific files are:

```text
tests/support/bridge_policy_contracts.py
tests/support/bridge_policy_effects.py
tests/operations/test_bridge_policy_contract_cases.py
```

The cases exercise real `BridgePolicyService` and DiscordOps management operation paths against SQLite persistence. Explicit expected effects cover precedence decisions, operation outcomes, dynamic entry state, and management audit events.

Generate the passive second-domain report:

```bash
.venv/bin/python tools/bridge_policy_contract_report.py \
  --output .artifacts/test-assurance/bridge-policy-contract/report.json
```

The report shows only declared contract representation and pytest outcomes. It does not infer missing product rules from production code and it does not make a missing declaration fail the suite.

### Stage 5 framework decision

Ban and bridge-policy case/effect models remain domain-specific because their inputs and observable effects differ materially. Only passive pytest collection and terminal-status accounting are shared in `tools/contract_report_support.py`. Common case metadata, entry-point adapters, and effect fragments remain deferred until another domain proves stable repetition.

## Property-based policy invariants

Hypothesis is confined to focused value-space tests under `tests/property/`. These tests complement named scenarios and typed contract cases; they do not replace them or derive expected results from production evaluators.

Run the normal development profile:

```bash
HYPOTHESIS_PROFILE=dev .venv/bin/python -m pytest -q tests/property
```

Run the larger deterministic CI profile:

```bash
HYPOTHESIS_PROFILE=ci .venv/bin/python -m pytest -q tests/property
```

The `dev` profile uses 50 examples per property and the `ci` profile uses 200. Both disable deadlines so slower shared runners do not create false failures. Hypothesis still reports and shrinks the minimal failing example.

The current properties cover block precedence, unrelated-entry stability, order/duplicate invariance, canonical host variants, malformed hosts, and Discord identifier validation. They intentionally avoid arbitrary full runtime state and operation sequences; lifecycle generation belongs to the stateful assurance stage.

## Stateful ban lifecycle assurance

The bounded state machine in `tests/stateful/test_ban_lifecycle_state_machine.py` generates repeated create, remove, reactivate, duplicate-create, duplicate-remove, query, and enforcement sequences for one community-scoped remote actor. Its independent model tracks only `ABSENT`, `ACTIVE`, and `REMOVED`; the system under test uses real operations, SQLite persistence, audit writes, and `UserBanService` enforcement.

Run the development sequence budget:

```bash
HYPOTHESIS_PROFILE=dev .venv/bin/python -m pytest -q tests/stateful
```

Run the larger CI sequence budget:

```bash
HYPOTHESIS_PROFILE=ci .venv/bin/python -m pytest -q tests/stateful
```

This layer does not replace named lifecycle scenarios and does not model global scope, local target resolution, community status changes, concurrency, or failures.

## Constrained interaction and cross-entry-point assurance

Ban authorization factors are defined in `tests/support/ban_interactions.py`. A deterministic greedy selector covers every valid 2-way factor/value pair while respecting explicit scope and persistence constraints. The selected suite executes 12 cases from 120 valid constrained candidates and covers all 91 valid pairs.

Generate the interaction report:

```bash
.venv/bin/python tools/ban_interaction_report.py \
  --output .artifacts/test-assurance/ban-interactions/report.json
```

The report measures constrained pair coverage only. It does not imply exhaustive semantic, branch, stateful, failure, or concurrency coverage.

`tests/assurance/test_guild_policy_entry_points.py` checks the same explicit guild access contracts through direct `BridgePolicyService` evaluation and DiscordOps command-access policy. Each path is checked against an independent expected boolean, and adding an unrelated blocked guild must not alter the target guild result. Adding the first allow entry is intentionally not treated as invariant because it changes empty-allowlist open mode into restrictive mode.

## Stage 9 failure-ordering and deterministic concurrency checks

Run the local-community federation relay assurance scenarios directly:

```bash
.venv/bin/python -m pytest -q \
  tests/behavior/test_local_community_remote_fanout_scenarios.py \
  -k 'policy_read_failure or partial_relay_failure or policy_change_during'
```

These scenarios prove three bounded contracts through the real relay repositories and renderer with only the gateway/policy repository edges controlled:

- policy-read failure occurs before relay source, delivery, or transport side effects;
- mixed per-target outcomes remain isolated and a retry sends only failed targets;
- an in-flight fanout keeps its action-scoped policy snapshot while the next action observes a concurrent policy change.

The concurrency scenario uses `asyncio.Event` barriers rather than sleeps, so ordering is deterministic. Existing management-audit rollback tests remain the authoritative coverage for mutation-plus-audit atomicity.

## Community-management contract migration

Community creation and edit authorization/status decisions are formalized in:

```text
tests/support/community_management_contracts.py
tests/support/community_management_effects.py
tests/operations/test_community_management_contract_cases.py
```

The typed cases execute real create/edit operations and SQLite persistence, then compare public results, persisted community state, and action-local management audit events with independently declared expectations.

Registration, Discord modal behavior, dashboard flows, and disabled-community runtime behavior remain named scenarios because their multi-step transport narratives are clearer than one shared operation table.

Generate the passive report with:

```bash
.venv/bin/python tools/community_management_contract_report.py \
  --output .artifacts/test-assurance/community-management/report.json
```

The report covers only declared community-management rules. It does not infer unknown product rules or claim that all dashboard/runtime narratives are represented by typed cases.

## Identity and discovery contract migration

Deterministic handle normalization, self-contained community resolution, ambiguity rejection, and relay labels are represented by typed cases in `tests/support/identity_discovery_contracts.py`. Database identity integration, registration, unified discovery, and directory-refresh flows remain named scenarios. Generate the report with `.venv/bin/python tools/identity_discovery_contract_report.py`.

No common case/effects base was extracted after this stage: ban, bridge policy, community management, and identity/discovery still have materially different inputs and observations. Passive pytest collection remains the only proven shared primitive.

## Subscription lifecycle contract migration

Operation-level registration, existing channel/follow state, last-channel cleanup, missing Follow IDs, and remote Undo outcomes are formalized in `tests/support/subscription_contracts.py`. Multi-step Follow/Accept/Undo, retry, duplicate activity, and gateway protocol verification remain named integration scenarios. Generate the report with `.venv/bin/python tools/subscription_contract_report.py`.

## Outbound fanout and routing migration

Outbound fanout remains primarily a named-scenario domain because target-specific mappings, receipts, retries, and mixed outcomes are clearer inline. `tests/support/fanout_contract_manifest.py` assigns stable rule IDs and classifications to the existing routing-metadata cases, remote/local fanout narratives, and deterministic failure-ordering checks. Generate the report with `.venv/bin/python tools/fanout_contract_report.py`.

Shared delivery/mapping/receipt effect fragments remain deferred: subscription operations and multi-target fanout do not yet expose one sufficiently narrow common observation shape.

## Inbound ActivityPub contract migration

Inbound handler outcomes, policy/routing skips, dedup, echo prevention, parent backfill, local-community Follow/Undo/content, mappings/receipts, and internal read APIs are assigned stable rule ownership in `tests/support/inbound_contract_manifest.py`. Protocol-rich payload and out-of-order flows remain named scenarios. Generate the report with `.venv/bin/python tools/inbound_contract_report.py`.

## Content lifecycle contract migration

Publish, reply, parent resolution, edit, delete, bidirectional mirrors, failure isolation, and dedup/out-of-order contracts are assigned stable rule ownership in `tests/support/content_lifecycle_manifest.py`. Rich conversation and payload scenarios remain explicit. Generate the report with `.venv/bin/python tools/content_lifecycle_contract_report.py`.

A shared content/delivery effects record remains deferred because fanout, inbound, and content tests still require different target-specific observations.

## Technical contract reporting

Dashboard, backup, OAuth, public URL, deployment, schema, and Fedify gateway checks remain native technical tests. Generate gateway evidence in resumable chunks with `python tools/gateway_contract_runner.py --start 0 --limit 14` and then the remaining chunk. Generate the combined JSON report with `python tools/technical_contract_report.py`. Artifacts are written under `.artifacts/test-assurance/technical-contracts/` and are never edited manually.

## Shared assurance reporting

`tools/assurance_reporting.py` contains only the two report mechanics proven across multiple domains: typed-case rule representation and named/generated node-prefix ownership. Domain inputs, expected effects, serializers, and output paths remain domain-specific. `tools/aggregate_assurance_report.py` combines already-generated JSON artifacts without rerunning tests or inferring new product rules.

## Migration completeness

Run `python tools/migration_completeness_report.py` to collect every project Python test, vendored DiscordOps test, and native gateway verification script. The generated JSON classifies each executable test as typed contract, named scenario, generated assurance, technical/native contract, or proven duplicate/obsolete. `unknown_unreviewed: 0` means every current test has an architectural ownership decision; it does not claim that every possible product rule is known.

### Local-community relay model exploration

Relay resilience setup now lives in `tests/support/local_community_relay.py`, and
failure/retry/snapshot scenarios live in a focused behavior module. This keeps
payload-projection narratives separate from durable relay lifecycle exploration.

The relay pilot now includes an independent pure transition model and fixed
model-vs-SUT examples before generated stateful exploration. The model excludes
ORM identities, timestamps, and payload rendering.

Generated relay exploration uses a bounded create/retry state machine with a
fixed source action and explicit 20x15 development and 75x30 CI budgets.

Create-only relay exploration now generates subscriber and federation-policy
changes between source actions while keeping per-source delivery history durable.
