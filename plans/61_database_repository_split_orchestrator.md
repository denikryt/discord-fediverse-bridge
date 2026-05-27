# 61 — Database repository split orchestrator

## Problem / Goal

`60_database_repository_split_umbrella.md` describes a staged refactor of the
Python persistence layer from one large `src/db.py` module into a repository
oriented persistence package. The umbrella plan intentionally describes broad
stage boundaries instead of prescribing every implementation detail.

This orchestrator defines the execution protocol for completing that umbrella
work safely and repeatably. It does not replace the umbrella plan and it does
not contain implementation details for individual stages. Each stage still
requires its own detailed plan written under `plans/` before code changes begin.

The goal is to make the work executable as a strict sequence:

```text
read orchestrator -> read umbrella -> write stage plan -> read stage plan ->
implement stage -> self-check stage -> run tests -> commit -> bundle ->
repeat from orchestrator for next stage
```

The executor must follow this process until the database repository split is
complete through Stage 8.

The executor must continue through all remaining stages in one uninterrupted
execution sequence unless an explicit blocker prevents safe progress. Finishing
a stage, producing a commit, or producing a bundle is not a stopping point by
itself. After each bundle, immediately restart the loop from this orchestrator
and continue with the next stage. Stop only for a concrete blocker such as an
unresolvable test failure, missing required project files, unavailable tooling,
a conflict with these rules or the umbrella scope, or an explicit user request
to pause or stop.

## Scope

This orchestrator applies only to the database repository split described by:

```text
plans/60_database_repository_split_umbrella.md
```

It must not be used as a general permission to change runtime behavior,
federation semantics, Discord behavior, database schema design, or command
behavior outside the stage boundaries defined by the umbrella plan and the
current detailed stage plan.

## Commit Count Rule

The intended implementation sequence after the umbrella plan is ready is:

```text
Stage 1 -> Stage 2 -> Stage 3 -> Stage 4 -> Stage 5 -> Stage 6 -> Stage 7 -> Stage 8
```

That means **eight implementation commits** for the repository split stages.

Stage 0 is an inventory and repository-map stage. It must be complete before
Stage 1 starts. There are two valid ways to satisfy Stage 0:

1. If Stage 0 is already complete in checked-in documentation/plans, record that
   fact in the Stage 1 plan and proceed with the eight implementation commits.
2. If Stage 0 is not complete, write and execute a detailed Stage 0 plan first.
   In that case the total becomes nine commits, and the executor must state
   that Stage 0 was not previously complete.

Do not silently skip Stage 0. Do not pretend the result is eight commits if a
new Stage 0 implementation commit was required.

## Global Execution Rules

For every stage, the executor must:

1. Re-read this orchestrator plan before writing or implementing the detailed
   stage plan. This includes the first stage and every later stage after a
   bundle has been produced.
2. Re-read `AGENTS.md` before writing or implementing the detailed stage plan.
3. Re-read `plans/60_database_repository_split_umbrella.md`.
4. Identify the current stage boundary from the umbrella plan.
5. Inspect the current project state and relevant code before writing the stage
   plan.
6. Write a dedicated detailed stage plan under `plans/` following project plan
   rules.
7. Re-read the newly written stage plan before implementation.
8. Implement only the scope described by that stage plan.
9. Keep full Python test suite expectations green unless a failure is proven
   unrelated or environment-only and reported precisely.
10. Self-check the implementation against the detailed stage plan before commit.
11. Make exactly one stage commit after the stage is complete.
12. Create and verify a git bundle for the updated branch.
13. Report the bundle path, commit hash, tests run, and any non-green test
    analysis.
14. Start the next stage only after the previous stage has been committed and
    bundled. After bundling, return to step 1 and re-read this orchestrator
    before reading the umbrella plan again.

The executor must not combine multiple stages in one commit. The executor must
not implement a later stage just because the code is nearby. The executor must
not leave uncommitted stage work before moving to the next stage.

## Detailed Stage Plan Requirements

Each detailed stage plan must include:

```text
Problem / Goal
Expected Behavior
Stage Boundary
Architecture
Touched Files
New Files
Implementation Steps
Tests
Regression / Blind-Spot Analysis
Open Questions
```

For mechanical extraction stages, `Expected Behavior` must explicitly say that
runtime behavior is unchanged. For stages that introduce temporary wrappers,
the plan must state exactly when those wrappers are removed, normally by
Stage 8.

Each stage plan must also include a short statement of how it preserves the
umbrella invariant:

```text
Database remains the single owner of engine/session/create_all/migrate.
Repositories share Database session ownership and do not create independent
engines or session factories.
```

## Stage Loop

### Before writing a stage plan

For the next stage, perform this sequence. Do not skip the first two reads and
do not proceed from memory after a previous stage has been bundled:

```text
1. Re-read plans/61_database_repository_split_orchestrator.md.
2. Re-read AGENTS.md.
3. Re-read plans/60_database_repository_split_umbrella.md.
4. Read any previous detailed stage plans for this repository split.
5. Inspect current git history and diff from the previous stage commit.
6. Inspect current src/db.py or src/db/ package layout.
7. Inspect call sites for the persistence domain owned by the stage.
8. Inspect tests that cover the persistence domain owned by the stage.
9. Decide the exact stage boundary.
```

Then write the detailed stage plan.

### Before implementing a stage

After writing the stage plan:

```text
1. Re-read this orchestrator plan.
2. Re-read the new stage plan completely.
3. Re-read the relevant part of the umbrella plan.
4. Confirm what is explicitly out of scope.
5. Confirm which tests must be run before the stage can be considered complete.
6. Only then begin implementation.
```

### During implementation

Implementation must follow the stage plan. If the executor discovers that the
plan is wrong or incomplete, it must update the stage plan first, then continue.

Do not let implementation details leak into the orchestrator. The orchestrator
only controls sequencing and quality gates.

### After implementation

Before commit, perform a self-check:

```text
1. Re-read this orchestrator plan.
2. Re-read the detailed stage plan.
3. Check every implementation step and acceptance criterion.
4. Check that out-of-scope items were not changed.
5. Run required focused tests.
6. Run full pytest.
7. Review git diff.
8. Confirm no unrelated cleanup or behavior change is included.
```

After the commit and bundle are produced, the next stage must begin by
re-reading this orchestrator plan again before re-reading the umbrella plan.

If a stage-owned test fails, fix it before commit.

If a non-stage test fails, classify it as one of:

```text
stage-owned regression
indirectly related regression
stale test expectation caused by stage-owned refactor
environment-only failure
unrelated existing failure
```

Stage-owned and indirectly related regressions must be fixed inside the stage.
Environment-only and unrelated failures must be reported with command, test
name, error summary, and reason.

## Stage Order

The executor must process stages in this order.

### Stage 0 — Persistence inventory and repository map

Use the umbrella plan Stage 0 boundary. Before Stage 1 starts, there must be a
checked-in inventory of current `Database` methods mapped to target
repositories, primary call sites, and relevant tests.

If this inventory already exists, do not create duplicate inventory work. State
where it exists in the next stage plan.

### Stage 1 — Navigation banners and baseline freeze

Write a detailed Stage 1 plan, implement it, self-check it, test it, commit it,
and bundle it.

This stage should make the current persistence file easier to review before
physical extraction begins. It must not move repository code.

### Stage 2 — Schema and migration extraction

Write a detailed Stage 2 plan, implement it, self-check it, test it, commit it,
and bundle it.

This stage extracts schema/migration implementation while keeping public
`Database.create_all()` and `Database.migrate()` calls working.

### Stage 3 — Local-community repository extraction

Write a detailed Stage 3 plan, implement it, self-check it, test it, commit it,
and bundle it.

This stage owns the local-community persistence domains listed in the umbrella
plan. It must not change local-community runtime semantics.

### Stage 4 — Remote subscription and bridge-follow repository extraction

Write a detailed Stage 4 plan, implement it, self-check it, test it, commit it,
and bundle it.

This stage owns remote subscription lifecycle persistence and bridge actor
follow persistence. It must not change subscribe/unsubscribe or Follow/Accept
semantics.

### Stage 5 — Registration, user, and event receipt repository extraction

Write a detailed Stage 5 plan, implement it, self-check it, test it, commit it,
and bundle it.

This stage owns registration/user state and ActivityPub event receipt
idempotency persistence.

### Stage 6 — ActivityPub object, remote actor, and mapping repository extraction

Write a detailed Stage 6 plan, implement it, self-check it, test it, commit it,
and bundle it.

This stage owns generic ActivityPub object/mapping persistence. It must not
change object IDs, actor IDs, JSON rendering, or federation compatibility
fallbacks.

### Stage 7 — Legacy Lemmy mapping and Discord fanout group repository extraction

Write a detailed Stage 7 plan, implement it, self-check it, test it, commit it,
and bundle it.

This stage owns older remote-community mapping and Discord fanout group
persistence. It must not change remote Lemmy publish/fanout behavior.

### Stage 8 — Remove temporary facade wrappers and finalize repository API

Write a detailed Stage 8 plan, implement it, self-check it, test it, commit it,
and bundle it.

This stage removes temporary `Database` forwarding wrappers and leaves one
supported persistence API for domain operations: repository properties on
`Database`.

After Stage 8:

```text
- no domain operation should have both Database.method(...) and
  Database.repository.method(...) as supported paths;
- `src/db.py` should be replaced by a `src/db/` package or reduced to a minimal
  import shim;
- docs should describe the final persistence layout;
- full pytest must be green or non-green failures must be fully analyzed.
```

## Commit Requirements

Each stage commit message should have a concise descriptive subject and a short
body. The subject should describe the stage result, not just name the stage.

Good subject examples:

```text
refactor: section database persistence domains
refactor: extract database schema migrations
refactor: move local community persistence into repositories
refactor: extract remote subscription repositories
```

Avoid subjects like:

```text
stage 3
update files
fix stuff
```

The commit body should describe the changes in a few concrete lines. It should
be more informative than one sentence, but it must not become a long
implementation report. Mention the main files or persistence domains touched,
the API boundary that changed, and any important test or migration effect.

Example commit message shape:

```text
refactor: extract database schema migrations

Move schema creation and migration helpers out of the Database facade into the
new persistence schema module. Keep Database as the owner of engine/session
setup and preserve the existing migrate() entry point for callers.

The runtime-facing behavior is unchanged; focused migration tests and full
pytest pass.
```

Each commit should contain exactly one completed stage unless the user
explicitly approves a different structure.

## Bundle Requirements

After each stage commit:

1. Create a clean bundle for the current branch.
2. Verify the bundle.
3. List bundle heads and confirm the expected branch is present.
4. Provide the bundle path to the user.

Recommended command shape:

```bash
git bundle create /mnt/data/<name>.bundle <current-branch>
git bundle verify /mnt/data/<name>.bundle
git bundle list-heads /mnt/data/<name>.bundle
```

Do not include uncommitted files in the report as if they were bundled. Git
bundles contain committed objects only.

## Testing Requirements

Every stage must run at least:

```bash
./.venv/bin/pytest -q
```

If the current environment does not have dependencies installed, install them
or report the exact environment failure. Do not mark a stage complete without
running the required tests unless the failure is environment-only and explained.

When a detailed stage plan requires focused tests, run those before full
pytest.

If the stage touches local-community repositories, run the local-subscriber
stage test group from the umbrella plan.

If the stage touches remote subscription repositories, run the remote
subscription test group from the umbrella plan.

## Documentation Requirements

Each stage must update documentation only when its own boundary requires it.
The executor must read the purpose paragraph of potentially relevant docs before
editing them. Do not mix documentation responsibilities.

At minimum, repository boundary changes should keep these documents accurate:

```text
docs/architecture/database-map.md
docs/development/navigation.md
notes/known_issues.md
```

If these files are not present in the current checkout, the stage plan must say
what documentation exists instead and where repository navigation is recorded.

## Out-of-Scope Guardrails

This orchestrator does not authorize changes to:

```text
ActivityPub payload compatibility fallbacks
Lemmy/Mastodon rendering compatibility
Fedify raw JSON fallback behavior
Discord message formatting/header fallback
old Discord autocomplete selected-value parsing
remote/local subscriber runtime semantics
schema redesign beyond repository relocation
new command behavior
new dashboard features
```

If a stage appears to require one of these changes, stop and write a separate
plan. Do not fold it into the database repository split.

## Final Completion Criteria

The repository split is complete only when all of these are true:

```text
- Stage 0 inventory/map is complete;
- Stage 1 through Stage 8 have been implemented in order;
- each implementation stage has its own commit;
- each stage was bundled after commit;
- full pytest was run for each stage;
- final Database owns engine/session/create_all/migrate;
- final domain persistence operations live on repository properties;
- temporary facade wrappers are gone;
- `src/db.py` is gone or only a minimal import shim;
- docs reflect the final persistence structure;
- no out-of-scope behavior was changed.
```
