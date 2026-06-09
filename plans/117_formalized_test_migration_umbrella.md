# 117 — Formalized Test Migration Umbrella Plan

## Purpose of This Plan

This is an umbrella plan for progressively migrating the remaining test suite toward the formalized assurance architecture established by plans 107–116.

The goal is not to rewrite every test into one universal case schema. The goal is to make each important behavioral domain explicit, machine-readable, and reportable where that improves confidence, while preserving readable named scenarios where narrative setup and cross-system effects are clearer than tabular cases.

Each stage must be independently useful and releasable. No stage may depend on a later migration to restore coverage, readability, or project stability.

Before implementing any stage:

1. inspect the current production paths, existing tests, support builders, reports, and documentation for that domain;
2. write a new detailed implementation plan under `plans/` for only that stage;
3. identify which existing tests should become typed cases, which should remain named scenarios, and which are duplicates that can be removed only after equivalent coverage is proven;
4. use TDD for every new observable behavior or confirmed defect;
5. leave the full project working, tested, documented, committed, and bundle-ready.

## Problem / Goal

The project now has a proven formalized assurance pattern for ban management and bridge policy:

- domain-specific typed contract cases;
- independently declared expected effects;
- effect snapshots over real production paths;
- passive pytest collection and JSON reports;
- property, stateful, pairwise, cross-entry-point, and deterministic failure layers.

Most of the remaining suite still uses one-off tests and scenario modules without machine-readable contract metadata. Those tests remain valuable, but the project cannot yet answer consistently:

- which rules and dimensions are represented in each remaining domain;
- which scenarios are duplicates versus distinct product contracts;
- which tests assert complete observable effects;
- where cross-entry-point consistency exists or is missing;
- which shared test structures are truly reusable across domains;
- which domains should remain narrative-only because formalization would reduce clarity.

The migration must proceed without these failure modes:

1. forcing every test into one generic schema;
2. redesigning the assurance framework before the next domains prove stable reuse;
3. replacing readable scenario tests with opaque parameter tables;
4. deriving expected results from production logic;
5. creating reports that claim semantic completeness beyond declared rules;
6. migrating large areas in one commit and making regressions difficult to localize.

## Expected End State

After all justified stages are complete:

- every correctness-critical domain has an explicit assurance owner and migration status;
- typed contract cases exist for dense decision spaces and repeated outcome patterns;
- named scenario tests remain for cross-system narratives, ordering, and complex multi-effect flows;
- important scenarios carry stable rule IDs and domain metadata even when they remain narrative tests;
- observable effect collection is consistent within each domain;
- passive reports cover all formalized domains and distinguish typed cases, named scenarios, generated properties, stateful models, and fault/concurrency checks;
- repeated infrastructure is extracted only after at least two or three domains demonstrate the same contract;
- domain-specific inputs and expected effects remain separate from generic reporting code;
- reports identify declared rule representation and known constraints without pretending to infer unknown product rules;
- historical duplicate tests are removed only after replacement coverage is proven and reviewed;
- no production behavior changes merely to fit the test framework;
- mutation testing remains deferred until migration and deterministic assurance layers are stable.

## Existing Architecture to Preserve

The migration starts from these proven components:

```text
tests/support/ban_contracts.py
tests/support/ban_effects.py
tests/support/bridge_policy_contracts.py
tests/support/bridge_policy_effects.py
tests/support/pairwise.py
tests/operations/test_ban_contract_cases.py
tests/operations/test_bridge_policy_contract_cases.py
tests/property/
tests/stateful/
tests/assurance/
tools/contract_report_support.py
tools/ban_contract_report.py
tools/bridge_policy_contract_report.py
tools/ban_interaction_report.py
```

The following principles are already established and remain mandatory:

- cases declare expected outcomes independently from production evaluators;
- tests execute real operation, handler, runtime, and persistence paths where practical;
- reports are passive and generated;
- domain case/effect models remain domain-specific until repetition proves a stable common abstraction;
- generated reports are artifacts, not manually edited specifications;
- scenario tests remain the primary form for readable end-to-end narratives.

## Migration Classification Model

Every reviewed test must be classified into one of five categories. This classification is part of each detailed stage plan and final verification.

### A. Typed contract case

Use when several tests share:

- the same action boundary;
- the same setup dimensions;
- the same observable effect shape;
- repeated authorization, validation, state, or outcome combinations.

Typical result:

```python
@dataclass(frozen=True, slots=True)
class CommunityManagementCase:
    id: str
    action: CommunityAction
    caller_role: CallerRole
    community_state: CommunityState
    expected: CommunityExpectedEffects
```

### B. Named scenario with formal metadata

Use when the test describes a meaningful cross-system narrative, but should still participate in contract reporting.

The scenario may keep explicit setup and assertions while exposing stable metadata such as:

```text
domain
rule_id
entry_point
risk
side_effect_classes
```

Metadata must be passive and must not make the scenario write report files.

### C. Generated assurance test

Use for:

- property invariants;
- stateful lifecycle sequences;
- constrained combinatorial interactions;
- metamorphic relations;
- deterministic fault or concurrency ordering.

Generated tests must remain attached to explicit independent rules and constraints.

### D. Infrastructure or implementation-contract test

Use for configuration parsing, schema shape, SDK adapters, serialization, deployment, and other technical contracts that are not product decision tables.

These tests may receive domain metadata or shared fixtures, but they should not be forced into product contract cases.

### E. Duplicate or obsolete test

A test may be removed only when:

- its exact product contract is represented elsewhere;
- observable effects are at least as complete in the replacement;
- transport-specific behavior is not lost;
- removal is included in the stage plan and verified against reports and full-suite results.

## Future Framework Direction

The likely mature framework should remain small and protocol-oriented.

Potential shared package:

```text
tests/assurance/
    metadata.py
    collectors.py
    adapters.py
    effects.py
    reporting.py
    registry.py
```

This structure is directional, not pre-authorized. Shared modules may be introduced only when current domains prove the need.

### Stable shared concepts that may emerge

#### Contract metadata

A minimal immutable metadata shape may eventually include:

```python
@dataclass(frozen=True, slots=True)
class ContractMetadata:
    id: str
    domain: str
    rule_id: str
    entry_point: str
    risk: str | None = None
```

It must not contain domain inputs or expected effects.

#### Adapter protocol

A small adapter protocol may standardize execution without hiding real runtime paths:

```python
class ContractAdapter(Protocol[CaseT, ObservedT]):
    async def execute(self, case: CaseT) -> ObservedT: ...
```

Domain implementations remain separate.

#### Effect fragments

Small composable fragments may be shared when repetition is proven:

- operation result and reason;
- persistence row state;
- audit events;
- Discord outbound effects;
- federation delivery effects;
- mappings and receipts;
- retry/dedup state.

There must not be one universal effects object containing irrelevant fields for every domain.

#### Domain registry and report aggregation

A future registry may aggregate report providers without coupling test execution to reporting:

```python
register_domain_report(
    domain="community_management",
    collector=...,
    rules=...,
    renderer=...,
)
```

The registry must not become a runtime dependency of test cases.

### Compatibility rules during migration

To avoid later rewrites:

- all new case IDs and rule IDs must be stable and machine-readable;
- case input, expected effects, observed effects, adapters, and reporting must remain separate;
- scenario metadata must be attachable without restructuring the scenario body;
- reports must consume protocols or metadata, not concrete ban-specific classes;
- domain enums must not depend on reporting code;
- shared extraction should move wiring, not rewrite most existing case data;
- no stage may introduce a base class solely to reduce a few repeated field names.

## Stage Completion Contract

Every stage is complete only when:

- the detailed plan classifies the affected existing tests;
- every migrated or removed test has a documented replacement or preservation decision;
- expected behavior remains independent from production logic;
- observable effects are at least as complete as before;
- full existing and new test suites pass;
- reports are deterministic and generated outside tracked source directories unless explicitly justified;
- documentation identifies what the stage formalizes and what remains outside the model;
- shared abstractions are justified by demonstrated repetition;
- the stage leaves no partially migrated domain or temporary compatibility adapter;
- the next stage is not required to make the current migration useful.

## Stage Boundary Model

### Boundary A — Authorization and management contracts

Stages 1–2 formalize remaining command/operation decision spaces where inputs and outcomes are already structured.

### Boundary B — Routing and delivery contracts

Stages 3–5 formalize fanout, inbound ActivityPub, subscription, retry, dedup, and delivery behavior while preserving narrative scenarios.

### Boundary C — Content lifecycle and UI/configuration contracts

Stages 6–7 formalize publish/edit/delete/reply behavior and dashboard/configuration domains.

### Boundary D — Framework extraction and report consolidation

Stage 8 may extract only the shared infrastructure proven by earlier domains and aggregate reports.

### Boundary E — Final migration review

Stage 9 classifies remaining tests, removes proven duplicates, documents intentional non-migration, and produces a stable migration-completeness report.

Mutation testing is explicitly outside this umbrella plan.

## Ordered Stages

## Stage 1 — Formalize community-management authorization and status contracts

### Problem

Community creation, ownership, metadata updates, enable/disable behavior, and disabled-community restrictions are spread across behavior, command, operation, dashboard, and permission tests.

### Direction

Create a detailed plan that inventories:

- community creation authorization;
- owner versus super-admin management;
- missing and disabled community outcomes;
- metadata update versus status-change audit behavior;
- guild versus DM command context;
- command, DiscordOps operation, dashboard, and runtime enforcement paths.

Introduce domain-specific typed cases for dense authorization and state combinations. Preserve named scenarios for multi-step registration and user-facing Discord behavior.

Candidate areas:

```text
tests/behavior/test_local_community_disabled_scenarios.py
tests/behavior/test_local_community_registration_scenarios.py
tests/behavior/test_local_community_edit_metadata_scenarios.py
tests/behavior/test_dashboard_scenarios.py
tests/test_local_community_permissions.py
tests/commands/
tests/operations/
```

### Required decisions

The detailed stage plan must decide:

- which management actions share one expected-effect shape;
- whether dashboard and Discord entry points can share one contract adapter or only rule metadata;
- which disabled-community scenarios remain narrative runtime tests;
- whether existing audit-effect fragments can be reused without creating a universal effects type.

### Must not include

- fanout or ActivityPub migration;
- generalized domain registry;
- stateful or combinatorial expansion beyond existing Stage 8 capabilities;
- production behavior changes without a failing regression test.

### Exit state

- community-management authorization has typed executable contracts;
- disabled/missing/enabled behavior is represented explicitly;
- command/dashboard/runtime entry points are either aligned or their differences are documented;
- readable registration and disabled-community narratives remain intact;
- a deterministic domain report exists.

## Stage 2 — Formalize identity, discovery, and registration contracts

### Problem

Local/remote identities, actor handles, community discovery, registration, labels, and directory snapshots are tested across many files with overlapping normalization and resolution expectations.

### Direction

Create a detailed plan that formalizes:

- local versus remote user/community identity;
- canonical actor and community identifiers;
- discovery success, missing, malformed, and ambiguous outcomes;
- registration idempotency and ownership association;
- directory/label presentation contracts;
- Discord and Lemmyverse autocomplete or discovery boundaries where product behavior is explicit.

Candidate areas:

```text
tests/test_fediverse_identity.py
tests/test_db_federation_identity.py
tests/test_community_discovery_resolution.py
tests/test_community_labels.py
tests/test_lemmyverse_communities.py
tests/behavior/test_registration_scenarios.py
tests/behavior/test_unified_community_discovery_scenarios.py
tests/behavior/test_discord_directory_snapshot_scenarios.py
```

Use typed cases for deterministic resolution tables. Keep end-to-end registration and directory-refresh narratives as named scenarios with metadata.

### Framework checkpoint

After Stages 1–2, compare community-management, identity/discovery, ban, and bridge-policy structures. Extract shared metadata or adapter protocols only if at least three domains use them without domain-specific compromises.

### Must not include

- inbound ActivityPub delivery behavior;
- broad content fanout;
- one universal identity-case hierarchy;
- replacing database identity integration tests with pure reference models.

### Exit state

- resolution and registration contracts are machine-readable;
- normalization expectations are independent from production evaluators;
- duplicate table-driven identity tests are consolidated where safe;
- cross-domain shared abstractions are either minimally extracted or explicitly rejected.

## Stage 3 — Formalize subscription, follow, unfollow, and retry contracts

### Problem

Subscription lifecycle behavior spans Discord commands, ActivityPub Follow/Accept/Undo, persistence, retries, and local/remote community routing. Current tests are rich but distributed and difficult to summarize mechanically.

### Direction

Create a detailed plan that inventories:

- subscribe/follow initiation;
- accepted, pending, rejected, duplicate, and missing states;
- local versus remote community targets;
- unfollow and Undo handling;
- retryable versus terminal delivery outcomes;
- mapping and persistence side effects;
- idempotency under duplicate activities.

Candidate areas:

```text
tests/behavior/test_subscription_scenarios.py
tests/behavior/test_unsubscribe_retry_scenarios.py
tests/test_follow_subscription_flow.py
tests/test_inbound_activity_outcomes.py
fedify-gateway/tests/verify-accept-follow.ts
```

Introduce typed lifecycle/action cases for deterministic state transitions. Preserve multi-step protocol exchange scenarios and gateway verification scripts as narrative/integration tests with formal metadata.

### Must not include

- message/comment fanout;
- general retry framework extraction unrelated to subscriptions;
- nondeterministic concurrency tests;
- replacing protocol integration tests with mocks.

### Exit state

- subscription lifecycle states and outcomes are explicit;
- follow/unfollow entry points map to declared rules;
- retry and duplicate behavior are represented without duplicating protocol narratives;
- a domain report identifies typed, narrative, and gateway coverage.

## Stage 4 — Formalize outbound fanout and routing contracts

### Problem

Fanout correctness depends on subscriptions, guild/channel/community mappings, policy, malformed targets, healthy-target isolation, receipts, retries, and dedup. Existing phase and behavior tests cover many cases but do not expose one coherent routing contract model.

### Direction

Create a detailed plan that splits the domain into bounded families:

- target selection and routing;
- policy and malformed-metadata rejection;
- healthy-target isolation;
- mapping/receipt creation;
- retry state;
- local-community versus remote-community fanout;
- origin guild versus subscribed guild behavior.

Candidate areas:

```text
tests/test_phase2_fanout_scenarios.py
tests/test_phase3_message_fanout_scenarios.py
tests/behavior/test_local_community_remote_fanout_scenarios.py
tests/behavior/test_local_community_stage3_local_subscriber_sync_scenarios.py
tests/behavior/test_local_community_stage4_local_subscriber_origin_scenarios.py
tests/test_policy_routing_metadata.py
```

Use typed target-routing cases only for repeated decision/effect shapes. Keep complex multi-target narratives and Stage 9 deterministic failure-ordering scenarios as named tests with rule metadata.

### Required framework evaluation

This stage must evaluate whether shared observable fragments for delivery, mapping, receipt, and retry state are now stable across subscription and fanout domains.

### Must not include

- inbound ActivityPub parsing;
- edit/delete lifecycle;
- one universal fanout case object covering every direction;
- hiding target-specific assertions behind opaque aggregate equality.

### Exit state

- outbound routing decisions and side effects are formalized by bounded family;
- healthy-target isolation and retry consequences are explicit;
- scenario tests retain readable multi-target narratives;
- shared effect fragments are extracted only if proven across domains.

## Stage 5 — Formalize inbound ActivityPub handling and dedup contracts

### Problem

Inbound activities pass through parsing, identity resolution, policy/ban enforcement, dedup, parent resolution, mapping, backfill, and local-community routing. Existing tests are distributed by historical phase and feature.

### Direction

Create a detailed plan that classifies inbound actions by observable contract:

- accepted, ignored, rejected, malformed, and duplicate activity outcomes;
- actor/community policy and ban enforcement;
- shared-group and local-community routing;
- missing parent/backfill behavior;
- activity/object dedup and echo prevention;
- out-of-order delivery;
- database mappings and receipts.

Candidate areas:

```text
tests/behavior/test_inbound_scenarios.py
tests/behavior/test_inbound_comment_backfill.py
tests/behavior/test_local_community_inbound_scenarios.py
tests/behavior/test_unsubscribed_inbound_activity_skip.py
tests/test_phase5_inbound_ap_shared_groups.py
tests/test_phase6_dedup_hardening.py
tests/test_inbound_activity_outcomes.py
tests/test_internal_fedify_api.py
```

Typed cases should cover repeated handler outcomes and side-effect absence/presence. Protocol-rich payload examples and out-of-order narratives should remain explicit scenarios.

### Must not include

- outbound publish/edit/delete migration;
- generic ActivityPub payload generator for all types;
- deriving expected outcomes from handler dispatch tables;
- removing historical phase tests solely for naming consistency.

### Exit state

- inbound outcome rules and enforcement paths are machine-readable;
- dedup/backfill mappings are asserted consistently;
- historical tests are classified and only consolidated where equivalent coverage is proven;
- inbound contract report distinguishes handler cases, narratives, and protocol integration tests.

## Stage 6 — Formalize content publish, reply, edit, and delete lifecycles

### Problem

Content creation and mutation are spread across Discord publishing, local-community publishing, bidirectional mirrors, reply preservation, edit/delete synchronization, and placement tests.

### Direction

Create a detailed plan that separates:

- post/thread creation;
- comment/message creation;
- reply/parent resolution;
- starter/opening-message dedup;
- edit propagation;
- delete propagation;
- local versus remote origin;
- Discord versus ActivityPub entry points;
- mapping and mirror state.

Candidate areas:

```text
tests/behavior/test_publish_scenarios.py
tests/behavior/test_local_community_publish_scenarios.py
tests/behavior/test_local_community_edit_delete_scenarios.py
tests/behavior/test_local_community_stage5_participant_edit_delete_scenarios.py
tests/test_discord_publish_flow.py
tests/test_phase4_reply_preservation.py
tests/test_phase8_edit_delete_sync.py
tests/test_phase9_bidirectional_mirror_messages.py
tests/test_end_to_end_dedup_flow.py
```

Use typed cases for repeated direction/action/outcome combinations. Preserve full conversation narratives and message-tree scenarios where sequence readability is essential.

### Framework checkpoint

After routing, inbound, and content domains are formalized, evaluate a shared content/delivery effect vocabulary. Any extraction must compose small fragments rather than create one all-purpose bridge-effects record.

### Must not include

- dashboard/configuration migration;
- performance/load testing;
- changing public content behavior to simplify test data;
- replacing rich payload assertions with only normalized summaries.

### Exit state

- supported content directions and lifecycle operations have explicit contract coverage;
- reply, dedup, mapping, and mutation effects are consistently asserted;
- cross-direction differences remain visible rather than normalized away;
- reports identify formalized and intentionally narrative coverage.

## Stage 7 — Formalize dashboard, configuration, deployment, and technical contracts

### Problem

Dashboard flows, OAuth, configuration, public base URLs, backup, deployment, schema cleanup, and gateway technical behavior are important but do not fit product decision-table models.

### Direction

Create a detailed plan that classifies these tests as technical contracts and introduces only appropriate formalization:

- stable metadata and rule IDs;
- parameterization for repeated configuration cases;
- explicit expected technical effects;
- passive report inclusion;
- shared fixture lifecycle where proven useful.

Candidate areas:

```text
tests/behavior/test_dashboard_scenarios.py
tests/behavior/test_sqlite_backup_scenarios.py
tests/test_discord_oauth_client.py
tests/test_public_base_url_config.py
tests/test_docker_deployment.py
tests/test_stage5_schema_cleanup.py
fedify-gateway/tests/
```

Do not force these tests into authorization or behavioral-domain case schemas. Gateway TypeScript verification should remain native to its runtime while exposing compatible report metadata where useful.

### Must not include

- rewriting all gateway tests into Python;
- generic technical-case base classes without repeated need;
- weakening end-to-end deployment assertions;
- mixing generated artifacts with committed configuration.

### Exit state

- technical contracts have a clear formalization strategy distinct from product behavior;
- repeated configuration cases are consolidated where useful;
- gateway and Python test results can be represented in a unified high-level report without sharing execution frameworks;
- infrastructure tests remain readable and native to their tooling.

## Stage 8 — Extract the minimal shared assurance framework and aggregate reports

### Problem

After several domains are migrated, repeated metadata, adapter, effect-fragment, collection, and reporting patterns may exist. Keeping all repetition would hinder maintenance, while premature abstraction would encode the wrong model.

### Direction

Create a detailed architecture plan based on actual duplication across completed domains.

The stage must inventory and decide separately whether to extract:

- `ContractMetadata` or equivalent protocol;
- adapter execution protocols;
- small effect fragments;
- domain report provider protocols;
- pytest metadata hooks for named scenarios;
- report registry and aggregate JSON renderer;
- shared validation for stable IDs, duplicate IDs, missing rules, and orphan cases.

Possible target structure:

```text
tests/assurance/
    metadata.py
    adapters.py
    effects.py
    collectors.py
    registry.py
    reporting.py
```

Existing domain cases should remain mostly unchanged. A successful extraction moves shared wiring behind protocols rather than rewriting domain schemas.

### Required migration test

The detailed plan must demonstrate migration on at least three existing formalized domains and prove:

- case IDs remain stable;
- reports remain semantically equivalent;
- domain-specific fields remain available;
- test failure diagnostics do not become less clear;
- runtime does not materially regress.

### Must not include

- universal input or expected-effect base classes;
- mandatory inheritance for all tests;
- mutation testing;
- rewriting intentionally narrative scenarios into typed cases;
- adding framework features without current callers.

### Exit state

- only proven shared infrastructure is centralized;
- domain models remain independent;
- aggregate reporting works across formalized product, generated, failure, and technical layers;
- framework boundaries and extension instructions are documented.

## Stage 9 — Complete migration review, remove proven duplicates, and document intentional exceptions

### Problem

Even after domain migrations, some historical tests may remain unclassified, duplicated, or intentionally outside formalized structures. The project needs an honest completion state rather than a misleading “all tests migrated” claim.

### Direction

Create a detailed plan that inventories every collected Python and gateway test and assigns:

- domain;
- rule or technical contract ID where applicable;
- classification A–E from this umbrella plan;
- migration status;
- report participation;
- intentional reason for non-migration.

Generate a deterministic migration-completeness artifact such as:

```text
total tests: 700
formal typed cases: 180
named scenarios with metadata: 260
generated assurance tests: 30
technical contracts: 190
intentional unclassified helpers: 40
unknown/unreviewed: 0
```

Review duplicate candidates and remove only those proven redundant. Preserve named scenarios when they add narrative or transport-specific value.

### Must not include

- requiring every test to become a typed case;
- deleting tests solely to improve migration percentages;
- introducing mutation testing;
- production refactors unrelated to confirmed testability defects;
- hard CI thresholds before report stability is proven.

### Exit state

- every executable test is classified;
- unknown/unreviewed count is zero;
- intentional non-migration is documented by category, not ad hoc comments;
- proven duplicates are removed safely;
- aggregate reports describe the real assurance architecture without overstating completeness;
- the project is ready for a separate future mutation-testing plan.

## Ordering and Non-Overlap Invariants

The stage order is intentional:

1. finish authorization and management domains before routing complexity;
2. formalize identity/discovery before subscription and inbound flows that depend on identity;
3. formalize subscription before fanout and inbound delivery;
4. separate outbound routing from inbound processing;
5. formalize content lifecycle after routing/dedup structures are understood;
6. treat technical contracts separately from product behavior;
7. extract shared framework only after multiple domain families prove repetition;
8. perform final classification and dedup only after migration architecture is stable.

A detailed stage plan must include a boundary table with:

- what the stage formalizes;
- which tests remain narrative;
- which tests are intentionally untouched;
- which shared abstractions are introduced or deferred;
- why the stage ends in a complete working state.

## Touched Files

This umbrella plan does not authorize a fixed file list. Each detailed stage plan must inspect the current repository and derive the exact files.

Likely areas include:

```text
tests/behavior/
tests/commands/
tests/operations/
tests/property/
tests/stateful/
tests/assurance/
tests/support/
tests/test_*.py
tools/
docs/development/test-assurance.md
fedify-gateway/tests/
pyproject.toml
```

Production files may be changed only when a formalized test exposes a confirmed defect or when a narrow testability refactor is required and justified in the detailed stage plan.

## New Files

No specific shared framework file is mandated before Stage 8.

Earlier stages may add domain-specific files such as:

```text
tests/support/<domain>_contracts.py
tests/support/<domain>_effects.py
tests/<layer>/test_<domain>_contract_cases.py
tools/<domain>_contract_report.py
```

Named-scenario metadata support may be introduced earlier only if at least two domains need it and the detailed plan proves it does not alter test semantics.

## Implementation Steps

This umbrella plan is executed in order:

1. formalize community-management authorization and status;
2. formalize identity, discovery, and registration;
3. formalize subscription, follow, unfollow, and retry;
4. formalize outbound fanout and routing;
5. formalize inbound ActivityPub and dedup;
6. formalize content publish/reply/edit/delete lifecycles;
7. formalize dashboard, configuration, deployment, and gateway technical contracts;
8. extract the minimal proven shared assurance framework and aggregate reports;
9. complete migration review, deduplicate safely, and document intentional exceptions.

After every stage:

- regenerate affected domain reports;
- run the full Python and gateway suites;
- compare test counts and removed/added node IDs;
- verify no observable contract was lost;
- inspect whether new repetition justifies future shared extraction;
- update `docs/development/test-assurance.md`;
- keep generated artifacts ignored and reproducible;
- leave the repository clean, committed, and bundle-ready.

## Tests

Every detailed stage plan must include:

- regression tests for any confirmed production defect;
- tests of domain case/effect helpers;
- tests of passive report collection;
- stable case/rule ID validation;
- proof that retained narrative scenarios still run;
- full repository test execution;
- gateway checks where the domain crosses the TypeScript boundary;
- before/after node-ID inventory when tests are consolidated or removed.

Migration validation must compare observable behavior, not only test counts.

## Metrics and Reports

The migration should track:

- tests by classification A–E;
- formalized domains and required rules;
- represented/missing/orphan rule IDs;
- typed versus narrative coverage;
- tests removed as proven duplicates;
- domain report runtime;
- full-suite runtime and flake rate;
- shared framework callers by domain;
- unknown/unreviewed tests.

No percentage is a quality score. Metrics describe migration state and evidence only.

## Regression and Blind-Spot Analysis

- **Mass migration risk:** Rewriting many tests at once can remove transport-specific or narrative coverage.
- **Schema coercion:** Unrelated domains may appear similar but require different inputs and effects.
- **Oracle duplication:** Expected results must not call the production logic under test.
- **Effect loss:** Consolidated cases may assert fewer side effects than historical scenarios.
- **Duplicate removal errors:** Similar test names do not prove equivalent contracts.
- **Historical phase value:** Phase-named tests may preserve important regression narratives even if their naming is old.
- **Report false completeness:** Reports cover declared rules, not unknown product rules.
- **Metadata burden:** Requiring verbose metadata on every test can reduce readability and discourage maintenance.
- **Gateway mismatch:** Python and TypeScript suites should share report concepts, not execution abstractions.
- **Framework overgrowth:** Shared modules must have multiple real callers and narrow responsibilities.
- **Runtime inflation:** Formalization must not make the default suite impractically slow.
- **Production-for-tests changes:** Production code must not be redesigned merely to fit typed cases.
- **Generated artifact drift:** Reports must always be regenerated, never hand-edited.

## Open Questions

There are no product decisions required to adopt this sequence.

Each detailed stage may discover an unclear product rule or an ambiguous equivalence between historical tests. Resolve it from existing code, documentation, prior plans, and behavior where possible. If the intended product behavior genuinely remains undefined, ask one clarification question according to `AGENTS.md` before finalizing that stage plan.
