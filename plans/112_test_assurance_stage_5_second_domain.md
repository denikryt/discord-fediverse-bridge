# 112 — Test Assurance Stage 5: Close Pilot Gaps and Validate a Second Domain

## Problem / Goal

Stage 4 reports all twelve declared ban rules as represented and passing, so there is no confirmed ban gap to fix. The remaining Stage 5 obligation is to prove whether the typed-case/effect/report pattern scales beyond bans without imposing a premature universal framework.

This stage pilots bridge allowlist/blocklist precedence and dynamic policy management as a second bounded domain. It must preserve the existing ban pilot, exercise real `BridgePolicyService` and DiscordOps operation paths, and extract only reporting mechanics that are demonstrably identical across both domains.

## Current Evidence

- `.artifacts/test-assurance/ban-contract/report.json` reports 12 required rules, 12 represented rules, zero missing rules, and 12 passing cases.
- Existing bridge-policy tests already cover individual precedence, authorization, validation, persistence, and audit behaviors, but those contracts are scattered across `tests/test_bridge_policy_core.py`, command scenarios, and operation behavior.
- The ban pilot proved stable case IDs, explicit inputs/expected effects, real operation execution, domain-specific effect snapshots, and passive JSON reporting.
- The reporting collector/status accounting is domain-neutral; the case dimensions, expected effects, and required rules are not.

## Scope and Boundary

### Changes

- Add a bridge-policy-specific typed case vocabulary and required-rule inventory.
- Add a bounded operation/service harness that executes real production policy evaluation and management paths against SQLite persistence.
- Add bridge-policy-specific observed-effect collection and diagnostic assertions.
- Add a passive deterministic JSON report for the second domain.
- Extract only generic passive pytest collection/status primitives shared by the ban and bridge-policy reports.
- Update the ban report to consume the extracted primitive without changing its JSON schema or test semantics.
- Document the second-domain workflow and the Stage 5 framework decision.

### Preserves

- Existing named behavior, command, and operation tests remain intact.
- Ban case IDs, ban expected effects, and ban report schema remain stable.
- Production bridge-policy behavior and public APIs remain unchanged unless a new failing regression test proves a defect.
- No historical test is required to adopt metadata.

### Explicitly excluded

- Hypothesis/property tests (Stage 6).
- Stateful sequence generation (Stage 7).
- Pairwise/3-way generation or cross-entry-point adapters (Stage 8).
- Fault injection, concurrency, and mutation testing.
- A universal `ContractCase` or universal effects object.
- Full migration of all bridge-policy tests.

## Domain Contract

Create `tests/support/bridge_policy_contracts.py` with immutable domain-specific types:

```python
@dataclass(frozen=True, slots=True)
class BridgePolicyExpected:
    applied: bool | None
    reason: str
    decision_allowed: bool | None
    active_entries: tuple[tuple[str, str], ...]
    inactive_entries: tuple[tuple[str, str], ...]
    audit_events: tuple[tuple[str, str], ...]

@dataclass(frozen=True, slots=True)
class BridgePolicyContractCase:
    id: str
    action: str
    caller_role: str
    policy_type: str
    subject: str
    bootstrap_entries: tuple[tuple[str, str], ...]
    existing_dynamic_state: str
    guild_context: str
    expected: BridgePolicyExpected
```

The exact field names may be refined during implementation, but inputs and expected effects must remain separate and independent from production evaluators.

The bounded pilot must represent at least these rules:

1. federation block overrides allow for the same domain;
2. empty federation allowlist permits an unrelated non-blocked domain;
3. non-empty federation allowlist denies an unrelated domain;
4. guild block overrides guild allow for the same guild;
5. effective super-admin can add a dynamic policy entry;
6. non-super-admin cannot mutate policy and produces the established forbidden audit;
7. bootstrap entries are immutable through dynamic management;
8. duplicate active dynamic entry is rejected without another row or audit;
9. inactive dynamic entry is reactivated rather than duplicated;
10. active dynamic entry can be removed and audited;
11. invalid subjects are rejected without persistence or audit side effects;
12. blocked guild context rejects a management action before mutation.

## Execution Harness

Create `tests/operations/test_bridge_policy_contract_cases.py`.

For each case:

1. create a real SQLite `Database` via `tests/support/db.py`;
2. build settings containing the requested bootstrap entries and caller role;
3. seed dynamic state through the repository where required;
4. construct a real `BridgePolicyService`;
5. execute either:
   - a real `BridgePolicyService` decision for precedence/read cases; or
   - `manage_bridge_policy_operation()` for mutation cases;
6. collect public result, effective decision, persisted entry rows, and action-local audit rows;
7. compare those observed effects field by field with explicit expected effects.

The harness must not call production evaluators to derive expected results.

## Observable Effects

Create `tests/support/bridge_policy_effects.py` with domain-specific immutable observations. It should collect:

- operation `applied` and `reason` where an operation is executed;
- policy decision `allowed` and reason where a decision is evaluated;
- persisted bridge-policy rows including policy type, normalized subject, and status;
- management audit action/result rows created by the action;
- no private operation state.

Assertions must be field-specific so failures identify whether outcome, decision, persistence, or audit diverged.

## Minimal Shared Reporting Primitive

Create `tools/contract_report_support.py` containing only mechanics proven identical in both domains:

- `CollectedCaseResult`;
- a passive pytest collector configured with a parameter name and a case predicate;
- terminal status normalization for pass/fail/skip/xfail;
- deterministic status totals.

It must not know domain dimensions, required rules, report schemas, or expected behavior.

Update `tools/ban_contract_report.py` to import these primitives while preserving its output byte-for-byte for equivalent input.

Create `tools/bridge_policy_contract_report.py` with domain-specific dimension extraction, required-rule comparison, selected represented combinations, CLI invocation, and JSON output under:

```text
.artifacts/test-assurance/bridge-policy-contract/report.json
```

The report must remain passive and must return pytest's exit code. Missing declared rules remain data, not a new CI failure in this stage.

## Tests

### TDD order

1. Add failing pure tests for generic collector/status behavior in `tests/test_contract_report_support.py`.
2. Add failing bridge-policy report builder tests in `tests/test_bridge_policy_contract_report.py`.
3. Add the typed contract cases and operation/service harness; confirm any unexpected production behavior before modifying production code.
4. Add effect collection/assertion tests through the real contract harness.
5. Adapt existing ban report tests to prove schema stability after extraction.

### Required focused checks

```bash
.venv/bin/python -m pytest -q \
  tests/operations/test_bridge_policy_contract_cases.py \
  tests/test_bridge_policy_contract_report.py \
  tests/test_contract_report_support.py \
  tests/test_ban_contract_report.py \
  tests/operations/test_ban_contract_cases.py
```

Generate both real reports and verify:

- all declared cases are present;
- all required rules are represented;
- zero current missing rules;
- all current cases pass;
- the ban report schema and totals remain unchanged.

Then run the full repository Python groups, compile checks, `git diff --check`, TypeScript check, and full gateway test suite.

## Documentation

Update `docs/development/test-assurance.md` with:

- the bridge-policy pilot files and command;
- what the report proves and does not prove;
- the Stage 5 framework decision.

The documented decision is:

- keep ban and bridge-policy case/effect models independent;
- share only passive collection/status mechanics;
- defer common case metadata protocols, adapter protocols, and effect fragments until a third domain or later stage proves stable repetition.

## Touched Files

- `tools/ban_contract_report.py`
- `tests/test_ban_contract_report.py`
- `docs/development/test-assurance.md`

## New Files

- `plans/112_test_assurance_stage_5_second_domain.md`
- `tests/support/bridge_policy_contracts.py`
- `tests/support/bridge_policy_effects.py`
- `tests/operations/test_bridge_policy_contract_cases.py`
- `tools/contract_report_support.py`
- `tools/bridge_policy_contract_report.py`
- `tests/test_contract_report_support.py`
- `tests/test_bridge_policy_contract_report.py`

## Verification and Handoff

Stage 5 is complete only when:

- the zero-gap ban result is explicitly confirmed rather than silently assumed;
- the second domain has executable typed cases, complete effects, required rules, and a passive report;
- only proven reporting mechanics are shared;
- no production behavior changes without a failing regression test;
- all tests and repository checks pass;
- the stage commit includes this plan;
- a complete verified bundle is produced.

Stage 6 may rely on two stable domain-specific contract models and one small shared reporting primitive. It must not assume a universal assurance framework exists.
