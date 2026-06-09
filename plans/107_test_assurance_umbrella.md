# 107 — Test Assurance and Behavioral Contract Umbrella Plan

## Purpose of This Plan

This is an umbrella plan, not a single implementation plan.

It defines an incremental path for strengthening the bridge test suite from its current scenario-test foundation into a systematic behavioral assurance system. The stages deliberately move from low-cost, low-abstraction improvements to more advanced techniques. Every stage must be independently useful, releasable, and understandable without requiring later stages.

The plan avoids two failure modes:

1. maintaining a large handwritten decision matrix that duplicates tests and quickly drifts;
2. building a large custom testing framework before the project has proved which abstractions are actually reusable.

The existing runtime/scenario tests remain the primary source of behavioral confidence. New techniques are added around them only where they reveal or prevent classes of defects that the existing suite cannot measure reliably.

Before implementing any stage:

1. inspect the current production paths, tests, support builders, documentation, and earlier completed stages;
2. write a new detailed plan under `plans/` for only that stage;
3. preserve the boundaries defined here;
4. use TDD for every new observable behavior or confirmed defect;
5. leave the full project working, tested, documented, committed, and bundle-ready.

## Problem / Goal

The project already has a large scenario-oriented pytest suite that exercises real runtime paths and observable effects. That is a strong base, but it does not yet provide a systematic answer to these questions:

- Which policy, authorization, lifecycle, routing, retry, and failure contracts exist?
- Which contracts are represented by tests, and which are only incidentally exercised?
- Do tests assert the complete observable result or only one symptom?
- Can the same rule be bypassed through another entry point?
- Are important input combinations, state transitions, and failure sequences absent?
- Would the suite detect a small but critical change to a permission or policy condition?
- Which advanced techniques add real value, and which would merely add maintenance cost?

The goal is to create a progressive assurance model in which:

- test cases, not a separate handwritten table, are the executable behavioral source of truth;
- machine-readable metadata and reports are generated from tests where useful;
- manual work is concentrated on defining independent expected behavior, not duplicating test contents in prose;
- advanced generation techniques are introduced only after the underlying contracts are stable;
- every added layer has measurable value and can remain useful even if later stages are never implemented.

## Current Baseline

The current repository already has several favorable properties:

- pytest is the common test runner;
- behavior tests execute real handlers and runtimes where practical;
- tests assert database state, mappings, dedup decisions, outbound delivery, and other observable effects;
- shared builders exist under `tests/support/`;
- command and DiscordOps operation tests cover narrower command contracts;
- policy, ban, community, fanout, retry, and federation behavior already have dedicated test modules;
- project rules prefer fakes and runtime scenarios over isolated implementation-detail mocks.

The initial weakness is therefore not “too few tests” in the abstract. It is the lack of a small, explicit, machine-readable model that connects product contracts to scenarios and makes missing dimensions or weak assertions visible.

## Expected End State

After all justified stages are complete:

- critical policy and authorization domains have typed executable contract cases;
- existing scenario tests remain readable and are not all forced into one abstraction;
- test metadata can be collected without tests writing reports themselves;
- reports show tested contracts, outcomes, entry points, and known gaps;
- important observable effects are asserted consistently through reusable effect snapshots where appropriate;
- independent invariants are covered by property-based tests;
- lifecycle-heavy domains are covered by stateful model tests;
- large interaction spaces use constrained combinatorial generation instead of full Cartesian products;
- equivalent rules exposed through multiple entry points are checked for consistent enforcement;
- dependency failures, partial failures, retries, and selected concurrency hazards are exercised deliberately;
- targeted mutation testing measures whether the suite detects changes to critical policy logic;
- CI runs fast deterministic layers on every change and reserves expensive assurance layers for targeted or scheduled execution;
- generated reports are disposable artifacts, not manually maintained documentation;
- no production logic is reused as the expected-result oracle for its own tests.

## Architecture Direction

### 1. Tests remain the executable source of truth

A separate manually maintained matrix must not become a second specification that drifts from code and tests. Where a domain benefits from a decision table, its rows should be represented as typed test-case data or generated from a constrained model.

Example shape:

```python
@dataclass(frozen=True)
class BanContractCase:
    id: str
    actor_role: ActorRole
    scope: BanScope
    community_state: CommunityState
    target_kind: TargetKind
    existing_ban_state: BanState
    expected: ExpectedEffects
```

The case supplies inputs and independently declared expected effects. A shared harness executes the real command or operation path.

### 2. Expected behavior must remain independent from production logic

The test oracle must not call the same production evaluator that the action under test calls.

Bad pattern:

```python
expected = production_policy.allows(input)
actual = await runtime.execute(input)
assert actual.allowed == expected
```

Acceptable oracle sources include:

- explicit expected effects in a named case;
- a small declarative invariant;
- a deliberately simpler reference model written independently from production control flow;
- comparison between entry points only when both are also checked against an independent expected result.

### 3. Reporting is passive

Tests must not write rows into CSV, Markdown, or databases during normal execution. Reporting should collect case metadata and pytest outcomes through a collector, pytest hooks, or post-processing of machine-readable test results.

Generated reports are CI or local artifacts. They are not required to be committed unless a later detailed plan identifies a stable summary that belongs in documentation.

### 4. Abstractions are earned by repetition

The first pilot may use domain-specific dataclasses and helpers. A generic `ContractCase`, report plugin, effects collector, or generation engine may be introduced only after at least two domains demonstrate the same stable need.

### 5. Layers have different jobs

- scenario tests protect named product behavior;
- branch coverage locates unexecuted control-flow paths but does not prove assertion quality;
- property tests cover general invariants over generated values;
- stateful tests cover generated operation sequences;
- combinatorial tests cover interactions without exhaustive products;
- differential and metamorphic tests detect inconsistent or relation-breaking behavior;
- fault and concurrency tests exercise failure ordering and isolation;
- mutation tests measure whether existing assertions detect seeded logic faults.

No layer replaces the others.

## Scalability and Future Framework Direction

The early stages must remain domain-specific, but they must also preserve a deliberate path toward a reusable assurance framework. The framework is a likely future outcome, not an immediate prerequisite. It may be introduced only after at least two domains prove which concepts are genuinely shared.

The scalable architecture should separate five roles:

1. **Domain contract cases**
   - Each domain owns its typed inputs and independently declared expected effects.
   - Ban, community, bridge-policy, fanout, and federation cases may expose different fields.
   - A future common base may provide only stable metadata such as `id`, `domain`, `rule`, `entry_point`, and `risk`; it must not force unrelated domains into one giant schema.

2. **Entry-point adapters**
   - An adapter executes one real production entry point for a contract case.
   - Adapters normalize transport-specific invocation details while preserving the real command, operation, handler, runtime, repository, and side-effect paths.
   - A future framework should allow one contract to be executed through several adapters without embedding Discord-, dashboard-, ActivityPub-, or background-runtime details into the case definition.

3. **Observed-effect collectors**
   - Collectors capture public outcomes and externally observable side effects: result status, reason code, persistence changes, audit events, Discord calls, federation calls, mappings, receipts, retries, and dedup state.
   - Domain-specific observed-effect types may compose small shared effect fragments rather than inherit from one universal object.
   - Collectors must not expose private implementation state merely to simplify assertions.

4. **Independent oracles and constraints**
   - Expected behavior remains explicit in named cases, small invariants, or deliberately simpler reference models.
   - Constraint definitions identify impossible or intentionally equivalent combinations.
   - Production evaluators must never be reused as their own oracle.

5. **Passive collection and reporting**
   - Pytest collection and execution hooks may collect case metadata, outcome, duration, and coverage identifiers.
   - JSON should remain the canonical generated artifact; Markdown or HTML are derived views.
   - Reporting must not alter test semantics or require tests to write files themselves.

A likely future package shape is:

```text
tests/assurance/
    contracts.py        # minimal shared metadata protocols, only after proven reuse
    adapters.py         # shared adapter protocols, not domain implementations
    effects.py          # small composable observable-effect fragments
    reporting.py        # passive pytest collection and JSON rendering
    combinatorial.py    # optional constrained generation support
    mutation.py         # targeted mutation configuration/report integration

tests/ban_assurance/
    cases.py
    adapters.py
    effects.py
    invariants.py

tests/<next_domain>_assurance/
    ...
```

This shape is directional, not mandatory. Detailed stage plans must derive actual modules from demonstrated duplication.

### Compatibility rules for early stages

To avoid rewriting pilot tests when the framework is later introduced:

- case IDs must be stable and machine-readable;
- cases must keep inputs separate from expected effects;
- execution must occur through a replaceable harness or adapter boundary;
- observed effects must be returned as data rather than asserted only inside opaque helpers;
- metadata collection must be passive;
- domain enums and state types must not depend on reporting code;
- report formats must not become part of the test API;
- generic abstractions must be extracted from working domain code, never designed in advance and imposed downward.

When the second domain is implemented, Stage 5 must explicitly decide whether to:

- keep the domains independent because their contracts differ;
- extract only shared protocols and effect fragments; or
- introduce the first minimal `tests/assurance/` framework layer.

That decision must include a migration assessment showing which existing pilot tests change and why. A framework extraction is successful only if most existing cases remain unchanged and only their harness/report wiring moves behind shared interfaces.

## Stage Completion Contract

Every stage is independently complete only when:

- its detailed plan is based on the current code and test suite;
- existing tests remain green;
- new tests required by the stage are green;
- any discovered production defect has a failing regression test before its fix;
- no generalized framework is introduced without demonstrated reuse;
- generated artifacts are reproducible and do not become handwritten sources of truth;
- test runtime remains appropriate for the stage’s intended execution cadence;
- documentation and developer commands are updated where the stage changes workflow;
- the stage records what it proves and what it intentionally does not prove;
- the next stage is not required to make the current stage useful.

## Stage Boundary Model

### Boundary A — Baseline visibility

Stages 1 and 2 may inventory, classify, parameterize, and improve assertions. They must not add property-based, stateful, combinatorial, concurrency, or mutation tooling.

### Boundary B — Reusable executable contracts

Stages 3 and 4 may introduce limited shared case/effect structures and generated reports after the pilot proves their shape. They must not create a general model generator or advanced search system.

### Boundary C — Generated input exploration

Stages 5 and 6 may add property-based and stateful testing. They must use independent invariants and preserve the scenario suite as the primary named-behavior layer.

### Boundary D — Interaction and cross-path assurance

Stages 7 and 8 may add constrained combinatorial coverage, differential checks, metamorphic relations, fault injection, and selected concurrency tests. They must not depend on mutation testing.

### Boundary E — Test-strength measurement

Stage 9 introduces targeted mutation testing only after the critical contracts and deterministic advanced tests are stable.

### Boundary F — Operational adoption

Stage 10 decides CI cadence, thresholds, and expansion to additional domains based on measured cost and value from the previous stages.

## Ordered Stages

## Stage 1 — Establish a measurable test baseline

### Problem

The suite count is known, but there is no stable baseline for branch coverage, test duration, flaky behavior, or which production modules are exercised by which broad test groups.

### Direction

Create a detailed plan that:

- adds coverage tooling with branch measurement;
- defines commands for behavior, command/operation, project-unit, and vendored DiscordOps groups;
- records execution time and branch coverage for policy-critical modules;
- uses `pytest --collect-only` or equivalent collection output to inventory test node IDs without interpreting their semantics;
- identifies the initial pilot domain as ban management;
- records baseline metrics without imposing project-wide coverage percentages.

Likely policy-critical modules for the initial report include:

- `src/bridge_policy.py`
- `src/federation_policy.py`
- `src/local_community_permissions.py`
- `src/user_bans.py`
- `src/operations/common_preconditions.py`
- `src/operations/ban_user.py`
- `src/operations/unban_user.py`

### Must not include

- rewriting existing tests into case objects;
- custom pytest plugins;
- semantic gap claims based only on line or branch coverage;
- a global coverage threshold chosen without baseline evidence;
- mutation or property-based testing.

### Exit state

- one reproducible baseline command set exists;
- branch coverage and durations are available as generated artifacts;
- the project knows which domain will be piloted next;
- no test behavior or production behavior has changed.

### Independent value

This stage immediately reveals unexecuted branches, slow groups, and unsuitable future CI costs even if no later formalization is adopted.

## Stage 2 — Formalize one bounded pilot domain without a framework

### Problem

Ban management has many dimensions—caller role, scope, community state, target identity, existing ban state, outcome, audit, and persistence—but current tests express them across separate behavior, command, and operation files without one executable contract vocabulary.

### Direction

Create a detailed plan that studies all ban-related production paths and tests, then introduces domain-specific typed case data only where it reduces duplication.

The pilot should cover at least:

- community owner versus super-admin versus unauthorized caller;
- community-scoped versus global ban/unban;
- local versus remote target resolution;
- missing, disabled, and enabled communities;
- absent, active, removed, and reactivated ban states;
- success, validation failure, and forbidden outcomes;
- response visibility and reason codes;
- database and audit effects;
- runtime enforcement where a banned actor attempts a supported action.

The detailed plan must choose a small subset of existing tests to parameterize rather than rewriting the whole ban suite.

Example:

```python
@pytest.mark.parametrize("case", BAN_AUTHORIZATION_CASES, ids=lambda case: case.id)
async def test_ban_authorization_contract(case, ban_harness):
    observed = await ban_harness.execute(case)
    assert observed == case.expected
```

### Must not include

- a generic cross-project `PolicyCase` hierarchy;
- automatic gap generation;
- custom report plugins;
- generated combinations;
- Hypothesis or mutation tooling;
- migration of unrelated policy or fanout tests.

### Exit state

- the ban pilot has a compact typed vocabulary;
- selected duplicate cases are parameterized;
- existing readable behavior scenarios remain intact where they communicate intent better;
- the pilot proves which fields and observable effects are actually reusable.

### Independent value

The ban contract becomes easier to review and extend without committing the project to a generalized framework.

## Stage 3 — Standardize observable effect assertions

### Problem

Existing tests often assert the right behavior, but related scenarios may verify different subsets of response, database, audit, Discord, federation, mapping, receipt, retry, or dedup effects.

### Direction

Create a detailed plan that inventories assertion patterns in the ban pilot and introduces narrowly scoped effect snapshots or assertion helpers only where omissions are likely.

Possible shape:

```python
@dataclass(frozen=True)
class BanObservedEffects:
    result: OperationOutcome
    reason_code: str | None
    active_bans: tuple[BanRecord, ...]
    audit_events: tuple[AuditRecord, ...]
    discord_calls: tuple[DiscordEffect, ...]
```

The helper must collect observable outcomes after the real action. It must not conceal important assertions behind a generic equality failure with no useful diagnostics.

The stage should also identify existing tests that pass while asserting only a partial contract and strengthen them.

### Must not include

- one universal effects object for the entire bridge;
- assertions against private implementation state;
- replacing named scenario assertions that are clearer inline;
- generated reports or advanced testing techniques.

### Exit state

- the pilot uses consistent complete assertions for critical success and rejection paths;
- forbidden and failed actions explicitly prove the absence of prohibited side effects;
- successful state-changing actions prove required audit and persistence effects;
- assertion failures remain readable.

### Independent value

The suite becomes more sensitive to partial regressions even without any new scenario-generation mechanism.

## Stage 4 — Generate contract coverage and gap reports from tests

### Problem

The project still cannot mechanically report which declared contract dimensions are represented, and manually maintained tables would drift.

### Direction

Create a detailed plan that builds a passive report from the typed pilot cases and pytest results.

The smallest acceptable implementation should:

- collect case ID and declared dimensions;
- associate each case with pass, fail, skip, or xfail status;
- emit JSON as the canonical generated format;
- optionally render a concise Markdown or HTML summary;
- show represented values and combinations;
- compare collected cases with a separately declared set of required ban rules or required combinations;
- report gaps without modifying test outcomes initially.

Example summary:

```text
ban authorization
  required rules: 18
  represented: 16
  missing: 2
  passing: 16
```

The test itself must not write the report. Prefer existing pytest hooks or a small collector over a large plugin package.

### Must not include

- inferring expected behavior from production code;
- treating branch coverage as semantic contract coverage;
- requiring metadata on all existing tests;
- failing CI on every unclassified historical test;
- generated Cartesian products.

### Exit state

- the ban pilot produces a deterministic machine-readable report;
- missing declared rules or combinations are visible;
- no handwritten decision matrix is required;
- report generation is separate from test execution semantics.

### Independent value

The project gains a maintainable answer to “what ban contracts are represented?” without reading every test manually.

## Stage 5 — Close confirmed behavioral gaps and expand the contract pattern

### Problem

The generated pilot report is useful only if missing cases are reviewed and translated into real behavioral protection. The project must also determine whether the pattern generalizes beyond bans.

### Direction

Create a detailed plan that:

- reviews each reported ban gap against product rules, code, and documentation;
- marks impossible or intentionally equivalent combinations through explicit constraints;
- adds behavior tests for confirmed missing cases;
- strengthens weak existing cases instead of duplicating them;
- fixes confirmed production defects only after a failing regression test exists;
- pilots the same typed-case/report pattern in one second bounded domain.

The second domain should be selected from current evidence, likely one of:

- bridge allowlist/blocklist precedence and management;
- community-management authorization;
- fanout routing-policy decisions.

Only after the second domain should the stage decide whether common base structures are justified.

### Must not include

- forcing all historical tests into the new model;
- property-based, stateful, combinatorial, or mutation testing;
- abstracting fields that are not genuinely shared by both domains.

### Exit state

- confirmed pilot gaps are closed or explicitly constrained;
- at least two domains demonstrate which metadata and effect structures are reusable;
- any shared abstraction is minimal and evidence-based;
- the project can reject further generalization if the domains are materially different.

### Independent value

This stage converts reporting into actual regression protection and validates whether the approach scales.

## Stage 6 — Add property-based invariants for value spaces

### Problem

Example-based scenarios cannot economically cover all meaningful actor handles, domains, URLs, case variants, duplicate entries, ordering, and malformed values.

### Direction

Create a detailed plan that introduces Hypothesis only for simple, independently stated invariants.

Candidate invariants include:

- a blocked domain is denied regardless of allowlist membership;
- adding an unrelated policy entry does not change another domain’s decision;
- reordering or deduplicating policy entries does not change effective policy;
- canonical-equivalent actor identifiers resolve consistently where the product contract defines equivalence;
- malformed identifiers fail according to the documented validation contract;
- a non-super-admin cannot produce a successful global state change for any generated target identity.

Property tests should target pure or narrow boundaries first. Runtime property tests are allowed only where fixtures remain fast and failures shrink to understandable examples.

### Must not include

- generating arbitrary full runtime states without constraints;
- duplicating production evaluators as reference models;
- replacing named edge-case scenarios;
- stateful operation sequences;
- project-wide Hypothesis adoption.

### Exit state

- Hypothesis is a dev dependency with documented profiles;
- selected invariants cover broad value spaces;
- failures produce reproducible minimized examples;
- runtime remains appropriate for normal or targeted CI according to measured cost.

### Independent value

The suite explores edge values and normalization cases that would be impractical to enumerate manually.

## Stage 7 — Add stateful model testing for lifecycle contracts

### Problem

Bans, communities, subscriptions, receipts, retries, and policy entries have lifecycle behavior that cannot be validated completely through isolated calls on fresh state.

### Direction

Create a detailed plan that selects one lifecycle domain, initially bans, and defines a small independent state machine.

Candidate operations:

```text
create community-scoped ban
create global ban
remove ban
reactivate ban
repeat create
repeat remove
query effective ban
attempt runtime action
```

The model should track only product-relevant state, such as `ABSENT`, `ACTIVE`, and `REMOVED`, while the system under test uses the real repository and operation path. Invariants should check persistence, authorization, audit consistency, idempotency, and enforcement after every generated sequence.

After the ban model is stable, a later detailed plan may evaluate subscription/retry or policy-entry lifecycles.

### Must not include

- modeling the entire bridge in one state machine;
- using the production repository as the model state;
- concurrency;
- sequence covering arrays;
- mutation testing.

### Exit state

- one bounded lifecycle has generated sequence coverage;
- failures shrink to a minimal reproducible action sequence;
- model and implementation state are compared after each rule;
- named scenario tests remain for important product narratives.

### Independent value

The project gains protection against transition-order and repeat-operation defects that single-action tests miss.

## Stage 8 — Add constrained combinatorial and cross-entry-point assurance

### Problem

Some contracts involve many factors, but a full Cartesian product is too large. The same rule may also be enforced through several commands, operations, handlers, or runtime paths.

### Direction

Create a detailed plan with two separate parts.

#### Part A — Constrained interaction coverage

For one proven multi-factor domain:

- declare factors and allowed values;
- declare impossible combinations as constraints;
- generate pairwise coverage by default;
- add selected 3-way coverage for high-risk interactions;
- retain explicit must-test cases for critical rules;
- report combination coverage separately from branch coverage.

Potential factors for ban authorization:

```text
actor_role
scope
community_state
target_kind
existing_ban_state
entry_point
```

The generated case must still obtain its expected result from an independent declarative rule or explicit expected mapping.

#### Part B — Cross-entry-point and metamorphic checks

Where equivalent product behavior is exposed through multiple paths:

- execute one contract through supported entry-point adapters;
- normalize transport-specific responses;
- compare each result with the independent expected contract;
- add metamorphic relations such as “adding a malformed target does not change healthy-target delivery” or “reordering policy entries does not change the decision.”

### Must not include

- exhaustive Cartesian generation;
- assuming pairwise coverage proves all interactions;
- comparing two entry points without an independent oracle;
- a universal adapter for unrelated actions;
- fault injection, concurrency, or mutation testing.

### Exit state

- selected high-dimensional contracts have measured 2-way or justified 3-way coverage;
- impossible combinations are explicit;
- alternative entry points cannot silently diverge for equivalent rules;
- generated case volume remains reviewable and performant.

### Independent value

The suite covers factor interactions systematically without thousands of handwritten cases.

## Stage 9 — Add failure ordering, partial-failure, and selected concurrency tests

### Problem

Correct happy-path authorization does not guarantee correct behavior when repositories, audit writes, Discord, federation delivery, retries, or concurrent actions fail in different orders.

### Direction

Create a detailed plan that inventories transaction and side-effect boundaries before writing tests.

Add deterministic fault injection through fakes at outer or persistence boundaries for cases such as:

- policy read failure before side effects;
- state mutation followed by audit failure;
- audit success followed by external delivery failure;
- one malformed or failing fanout target among healthy targets;
- retry after a partially completed action;
- duplicate inbound delivery;
- policy or ban state change during a multi-target action.

Selected concurrency tests may use async barriers or controlled fake repositories for:

- two concurrent creates for the same ban;
- ban and unban racing;
- duplicate ActivityPub activity processing;
- policy change during fanout where snapshot visibility is contractually defined.

Every case must define the expected persistence, audit, retry, dedup, and external side effects.

### Must not include

- nondeterministic sleep-based race tests;
- broad load or performance testing;
- mocks of internal domain methods merely to force branches;
- mutation testing;
- changing transactional behavior without a failing behavioral test and a separate implementation plan if the fix is substantial.

### Exit state

- critical failure points have deterministic tests;
- partial failure and retry outcomes are explicit;
- selected concurrency invariants are reproducible;
- tests distinguish fail-closed, target-isolated, retriable, and terminal outcomes.

### Independent value

The project protects the operational edges where many bridge defects occur despite green happy-path tests.

## Stage 10 — Introduce targeted mutation testing and operationalize the assurance layers

### Problem

Coverage and scenario counts do not prove that tests would detect a small, critical logic change. Mutation testing is expensive and noisy if applied before contracts and assertions are mature.

### Direction

Create a detailed plan that evaluates current Python mutation tools against the project’s Python version, pytest setup, async tests, incremental execution, and reporting needs. Do not predetermine the tool before a compatibility spike.

Start with a narrow target set, such as:

- `src/bridge_policy.py`
- `src/local_community_permissions.py`
- `src/operations/common_preconditions.py`
- `src/operations/ban_user.py`
- `src/operations/unban_user.py`
- narrowly selected fanout policy gates

The stage should:

- establish a clean baseline run;
- mutate only selected modules;
- run the smallest relevant test subset per target where tooling supports it;
- classify surviving mutants as real test gaps, equivalent mutants, unreachable/dead code, or low-value changes;
- add tests only for meaningful survivors;
- document a repeatable local command;
- define CI cadence from measured cost rather than running the full mutation suite on every commit.

Likely cadence options:

- targeted mutation checks on policy-related pull requests;
- scheduled nightly or weekly runs;
- manual runs before major policy releases.

The final part of this stage should define which earlier layers run:

- on every commit;
- on policy-related changes;
- nightly;
- manually.

### Must not include

- manually editing production files as the normal workflow;
- mutating the entire repository initially;
- treating mutation score as a universal quality grade;
- requiring every equivalent mutant to be killed;
- weakening tests to make mutation execution faster;
- adding a hard CI threshold before baseline classification.

### Exit state

- targeted mutation testing runs automatically through a selected tool;
- meaningful surviving mutants become actionable gaps;
- equivalent/no-value mutants are documented or excluded narrowly;
- execution cost and cadence are known;
- fast deterministic tests remain the normal development gate;
- the project has a documented layered test strategy.

### Independent value

This final layer measures whether the suite actually detects seeded policy defects and provides evidence about assertion strength rather than execution alone.

## Suggested Expansion Order After the Ban Pilot

The umbrella stages define techniques, not a requirement to formalize every domain at once. Expansion should follow risk and evidence:

1. ban authorization and lifecycle;
2. bridge allowlist/blocklist precedence and management;
3. community management authorization and disabled-community behavior;
4. fanout routing policy and malformed-target isolation;
5. inbound ActivityPub ban/policy enforcement;
6. subscription, retry, dedup, and out-of-order delivery state machines;
7. broader content routing only where reports show useful gaps.

A domain should not be migrated merely to increase a “formalized tests” count.

## Touched Files

This umbrella plan does not authorize a fixed implementation file list. Each stage plan must derive the exact files from the repository state at that time.

Known areas that detailed plans are likely to inspect include:

- `pyproject.toml`
- `AGENTS.md`
- `tests/conftest.py`
- `tests/support/`
- `tests/behavior/test_local_community_user_ban_scenarios.py`
- `tests/behavior/test_bridge_policy_management_scenarios.py`
- `tests/commands/test_ban_user_command.py`
- `tests/commands/test_unban_user_command.py`
- `tests/operations/test_ban_user_operation.py`
- `tests/operations/test_unban_user_operation.py`
- `tests/test_bridge_policy_core.py`
- `tests/test_command_access_policy.py`
- `tests/test_federation_policy.py`
- `tests/test_local_community_permissions.py`
- `src/bridge_policy.py`
- `src/federation_policy.py`
- `src/local_community_permissions.py`
- `src/user_bans.py`
- `src/operations/common_preconditions.py`
- `src/operations/ban_user.py`
- `src/operations/unban_user.py`
- policy-sensitive fanout and ActivityPub modules identified by later inventories

A stage plan must not copy this list blindly.

## New Files

No shared framework file is mandated by this umbrella plan.

Possible files that later detailed plans may justify include:

- domain-specific case definitions under `tests/support/` or a focused `tests/contracts/` package;
- domain-specific effect collectors;
- a small pytest collection/report hook;
- generated-report scripts under a developer tooling directory;
- Hypothesis strategies and state machines;
- combinatorial model definitions;
- mutation-tool configuration.

Each new file must have a narrow responsibility. Generated reports should normally be written outside tracked source directories or ignored by Git.

## Implementation Steps

This umbrella plan is executed by completing the stages in order:

1. establish baseline visibility;
2. formalize the ban pilot without a general framework;
3. standardize complete observable-effect assertions;
4. generate contract coverage and gap reports;
5. close confirmed gaps and validate reuse in a second domain;
6. add property-based invariants;
7. add stateful lifecycle testing;
8. add constrained combinatorial and cross-entry-point assurance;
9. add deterministic fault and selected concurrency testing;
10. introduce targeted mutation testing and CI cadence.

After every stage:

- inspect the resulting test architecture before planning the next stage;
- confirm the stage remains useful independently;
- measure runtime and maintenance impact;
- remove or simplify abstractions that did not prove useful;
- do not continue automatically if the next layer has no demonstrated target or expected value.

## Tests

Every detailed stage plan must specify its own tests. At umbrella level, the required strategy is:

- keep existing runtime/scenario tests as the primary named behavior layer;
- add parameterized cases only where they improve reviewability and coverage;
- assert real effects rather than only call counts;
- use branch coverage as diagnostic evidence, not proof of correctness;
- use property/stateful/combinatorial generation only with explicit constraints and independent oracles;
- make fault and concurrency tests deterministic;
- run mutation testing only on selected critical production modules;
- preserve minimal reproducible examples and useful failure output;
- keep expensive layers outside the default local loop unless measured runtime proves otherwise.

## Metrics and Reports

No single metric is a release-quality score. The project should use a small set of complementary measurements:

- collected and passing tests by test group;
- branch coverage for selected critical modules;
- represented required contract rules;
- known missing or constrained combinations;
- property/stateful examples executed and minimized failures;
- pairwise or 3-way combination coverage for selected models;
- meaningful surviving mutants after classification;
- runtime and flake rate by assurance layer.

Generated reports must distinguish facts from interpretation. For example, an uncovered branch is a fact; whether it is a product risk requires review.

## Regression and Blind-Spot Analysis

- **Specification duplication:** A manual matrix can drift from tests. Contract data should live with executable cases.
- **Oracle duplication:** Reusing production logic for expected results can reproduce the same defect in both sides.
- **Framework overgrowth:** A generic testing framework created from one domain will likely encode the wrong abstractions.
- **False completeness:** High line, branch, pairwise, or mutation coverage does not prove all product behavior is correct.
- **Cartesian explosion:** Unconstrained combinations create large suites dominated by impossible or redundant cases.
- **Weak negative assertions:** Forbidden and failed actions must prove prohibited side effects did not occur.
- **Transport confusion:** Different entry points may have different response shapes while sharing one authorization contract; normalization must not erase meaningful differences.
- **Flaky concurrency:** Sleep-based race tests create noise rather than assurance.
- **Mutation noise:** Equivalent and low-value mutants must be classified rather than forcing meaningless tests.
- **Runtime regression:** Advanced tests can make the normal development loop too slow; cadence must be based on measured cost.
- **Historical-test migration:** Existing readable tests must not be rewritten solely to satisfy metadata or reporting coverage.
- **Generated artifact drift:** Reports should be regenerated, not edited manually.

## Research Basis

The direction of this plan follows established practices:

- pytest parametrization and markers support machine-readable families of cases without requiring a separate handwritten matrix;
- property-based and stateful testing are complementary to example-based tests, not replacements;
- NIST combinatorial testing uses covering arrays and sequence coverage to reduce interaction spaces without exhaustive products;
- OWASP authorization testing emphasizes checking roles, protected functions, resources, privilege escalation, and bypass through alternative paths;
- branch coverage identifies executed control-flow paths but cannot measure assertion quality;
- mutation tools seed small source changes and rerun tests, making mutation testing appropriate only after critical contracts and assertions are stable.

Detailed stage plans should re-check current tool compatibility and documentation before adding dependencies because versions and supported Python releases may change.

## Open Questions

There are no product decisions required to adopt this sequence.

Each detailed stage may discover a product-rule ambiguity, such as expected behavior for duplicate operations, audit failure atomicity, or a race between policy changes and fanout. Such questions must be resolved from existing code, documentation, and prior decisions where possible. If the intended product behavior genuinely remains undefined, ask one clarification question according to `AGENTS.md` before finalizing that stage plan.
