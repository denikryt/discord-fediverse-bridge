# 98 — Reusable Stage Orchestrator

## Configuration

- `UMBRELLA_PLAN`: `plans/117_formalized_test_migration_umbrella`
- `STAGES`: `1-9`

## Absolute Execution Rule

This orchestrator is a strict executable algorithm. Every instruction and every numbered step is mandatory.

Execute every step exactly in the written order. Do not skip, reorder, merge, reinterpret, weaken, approximate, postpone, substitute, or add anything outside the configured stage scope.

Do not proceed to the next step until the current step and every result it requires have been completed and verified.

Do not stop or end execution while any configured stage remains incomplete. Time elapsed, command timeouts, tool limits, or suspected platform limits are not valid Stop Conditions; rerun or split the work and continue. Stop only for the genuine blocker defined below.

If an error, omission, or deviation is discovered, immediately correct it, repeat all affected steps, and continue from the correct state. Do not stop, abandon the stage, or defer the correction.

Stopping is permitted only under the exact genuine-blocker condition defined in the Stop Condition section.

A stage is complete only when every instruction and every step has been executed and verified. Tests passing, code working, or a commit existing cannot compensate for any missing step.

Any uncorrected deviation invalidates the stage.

## Rules

1. Before every stage, read the author identity of the current `HEAD`. The stage commit must use the same author identity.
2. Do not stop between configured stages. Stop only for a genuine blocker that cannot be resolved from the project rules, configured umbrella plan, current codebase, tests, or documentation.
3. Implement only the stages listed by `STAGES`, in that exact order, from `UMBRELLA_PLAN`. Do not implement any stage outside that configured sequence.
4. Every stage must leave the project fully working. All tests and required repository checks must pass before committing the stage.

## Stage Algorithm

For each stage listed by `STAGES`, in the configured order:

1. Read this orchestrator completely, including the Configuration section.
2. Read `AGENTS.md` completely.
3. Read `UMBRELLA_PLAN` completely.
4. Read the section for the current stage in `UMBRELLA_PLAN`.
5. Study the current codebase according to `AGENTS.md`:
   - inspect the current implementation;
   - inspect all relevant callers and runtime paths;
   - inspect existing tests and documentation;
   - verify the exact boundary of the current stage;
   - identify conflicts and missing requirements before writing the stage plan.
6. Write a new detailed plan for the current stage according to `AGENTS.md`.
   - The plan must be detailed, include examples, and be fully thought through.
   - The plan must describe only the current stage.
   - The plan must preserve the stage boundaries from `UMBRELLA_PLAN`.
   - The plan must identify the exact files, modules, classes, functions, interfaces, callers, runtime paths, and documentation that will change.
   - The plan must describe the intended contracts, control flow, dependency wiring, failure behavior, and compatibility impact where relevant.
   - The plan must include concrete examples of the important API shapes, call flows, data transformations, error outcomes, or before/after behavior where examples improve precision.
   - Tests must follow the project rules: behavior tests have priority; unit tests are allowed when appropriate.
7. Read the completed stage plan completely and verify it against:
   - `AGENTS.md`;
   - this orchestrator;
   - `UMBRELLA_PLAN`;
   - the current codebase.
8. Implement the stage plan.
9. After implementation, verify the stage plan point by point:
   - every planned change is implemented;
   - every change is implemented as described;
   - no required item is omitted;
   - no work from another stage was added;
   - the project is fully working.
10. Run all tests and required repository checks. They must all pass.
11. Create a commit using the same author identity as the current `HEAD` read at the start of the stage. The commit must include a concise body that describes the implemented changes in more detail without being long or overly detailed.
12. Create and verify a Git bundle for the completed stage.
13. Send the stage bundle to the user.
14. If another configured stage remains, immediately continue with it by returning to Step 1.

## Stop Condition

Stop only if there is a genuine blocker that makes correct implementation impossible without a missing decision or unavailable requirement. When stopping, state the exact blocker and why it cannot be resolved from the available project information.

After the final stage listed by `STAGES` is completed, stop. Do not begin any stage outside the configured sequence.
