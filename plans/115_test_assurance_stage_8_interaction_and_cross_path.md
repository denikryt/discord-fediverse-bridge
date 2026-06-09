# 115 — Test Assurance Stage 8: Constrained Interactions and Cross-Path Policy

## Problem / Goal

Ban authorization combines action, role, scope, community state, target type, and existing lifecycle state. Exhaustive Cartesian testing would be large and redundant, while the named pilot does not measure interaction coverage. Separately, Discord guild policy is consumed directly by `BridgePolicyService` and through DiscordOps command-access policy; those paths need one independent cross-entry-point contract.

Stage 8 adds deterministic constrained pairwise coverage for ban authorization and a bounded cross-entry-point/metamorphic suite for guild access. It does not add failures, concurrency, or mutation testing.

## Part A — Constrained Ban Interaction Coverage

### Factors

Declare finite values in `tests/support/ban_interactions.py`:

- `action`: `ban`, `unban`;
- `caller_role`: `owner`, `super_admin`, `unauthorized`;
- `scope`: `community`, `global`;
- `community_state`: `enabled`, `disabled`, `missing`;
- `target_kind`: `remote`, `local`;
- `existing_ban_state`: `absent`, `active`, `removed`.

### Constraints

- global scope requires `community_state=missing`;
- scoped missing communities cannot have a pre-existing scoped ban, so `existing_ban_state=absent`;
- all generated actor handles are syntactically valid and local targets are seeded before execution;
- the generated set is not treated as exhaustive product coverage.

### Generator

Create `tests/support/pairwise.py` with a small deterministic greedy covering-array helper:

1. enumerate only valid constrained candidates;
2. calculate all valid 2-way value pairs observable in those candidates;
3. select candidates by maximum uncovered-pair gain with stable tie-breaking;
4. include explicit must-test high-risk candidates;
5. expose required/covered pair counts and missing pairs.

The helper is test infrastructure, not a generalized production framework. Add pure tests proving determinism, constraint preservation, must-test inclusion, and complete 2-way coverage for a synthetic model.

### Independent Expected Result

`tests/support/ban_interactions.py` owns a deliberately simple declarative reference function. It must encode the documented precondition order without calling ban operations, repositories, or permission helpers:

1. global non-super-admin -> `global_scope_requires_super_admin`;
2. scoped missing community -> `unknown_or_inaccessible_community`;
3. scoped unauthorized caller -> `cannot_manage_community`;
4. scoped disabled community -> `community_disabled`;
5. valid ban with active state -> `duplicate_active_ban`;
6. valid ban with removed/absent state -> `reactivated`/`created`;
7. valid unban with active state -> `unbanned`;
8. valid unban otherwise -> `no_active_ban`.

The expected row/audit effects may reuse explicit transition mappings, not production evaluators.

### Execution

Create `tests/operations/test_ban_pairwise_interactions.py` using real SQLite, local-community setup, local/remote target resolution, ban/unban operations, persistence, and audits. Every selected case runs through the real operation path and compares normalized observable effects with the independent expectation.

### Coverage report

Create `tools/ban_interaction_report.py` that writes deterministic JSON containing:

- factor values;
- constraints description;
- total valid Cartesian candidates;
- selected pairwise case count;
- required, covered, and missing valid 2-way pairs;
- explicit must-test cases;
- selected case dimensions.

The report is combination coverage, not branch or semantic completeness.

## Part B — Cross-Entry-Point and Metamorphic Guild Policy

Create `tests/assurance/test_guild_policy_entry_points.py`.

Define explicit cases for:

- empty allowlist/open guild;
- listed guild allowed;
- unlisted guild denied under a non-empty allowlist;
- blocklisted guild denied;
- blocklist overriding allowlist.

Execute each case through two bounded adapters:

1. real `BridgePolicyService.is_discord_guild_allowed`;
2. real DiscordOps `GUILD_COMMAND_ACCESS` evaluation using `CommandAccessInput` and the same effective policy state.

Each adapter is compared separately with the case’s explicit `expected_allowed` value. The test must not merely compare adapters to each other.

Add a metamorphic relation: adding allow/block entries for a distinct unrelated guild must not change the explicit expected result for the target guild through either adapter.

## Boundary

### Preserves

- named ban and guild-policy scenarios;
- typed ban/bridge-policy pilots;
- stateful lifecycle model;
- production behavior and APIs unless a generated failing case proves a defect.

### Excludes

- exhaustive Cartesian execution;
- claiming pairwise completeness proves all interactions;
- fault injection or partial failure;
- concurrency;
- mutation testing;
- a universal adapter hierarchy.

## TDD and Implementation Steps

1. Add pure failing tests for the pairwise helper.
2. Define constrained ban factors, constraints, independent oracle, and high-risk must-test cases.
3. Generate cases and verify 100% coverage of valid 2-way pairs.
4. Execute generated cases through real ban/unban operations and complete effects.
5. Add the deterministic interaction report and tests.
6. Add explicit guild cross-entry-point cases and adapters.
7. Add unrelated-entry metamorphic checks.
8. Run focused pairwise, cross-path, typed, property, and stateful suites.
9. Generate and inspect the interaction report.
10. Run all repository and gateway checks.

## Touched Files

- `docs/development/test-assurance.md`

## New Files

- `plans/115_test_assurance_stage_8_interaction_and_cross_path.md`
- `tests/support/pairwise.py`
- `tests/support/ban_interactions.py`
- `tests/test_pairwise.py`
- `tests/operations/test_ban_pairwise_interactions.py`
- `tools/ban_interaction_report.py`
- `tests/test_ban_interaction_report.py`
- `tests/assurance/__init__.py`
- `tests/assurance/test_guild_policy_entry_points.py`

## Exit State

Stage 8 is complete only when:

- valid ban factor pairs have measured 100% 2-way coverage;
- constraints and must-test cases are explicit;
- selected case volume is reviewable and materially below the valid Cartesian set;
- all generated expected results come from the independent declarative table;
- both guild entry points independently match explicit expected behavior;
- the unrelated-entry metamorphic relation holds through both paths;
- all full repository checks pass;
- the stage plan is committed and a complete verified bundle is produced.

## Handoff

The configured orchestrator stops after Stage 8. Failure ordering/concurrency (Stage 9) and mutation testing (Stage 10) remain untouched.
