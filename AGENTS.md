# Project Rules

## Tests

- Use TDD by default: write the failing behavior test first, then implement.
- After the test turns green, refactor. Do not leave code in a "just works" state.
- Prefer runtime/scenario tests over isolated unit tests for bridge behavior.
- Tests must be written as user action in a defined system state -> observable result.
- An action means a specific action under specific preconditions, system state, and input data.
- In bridge code, platform events are tested as concrete runtime actions with defined system state, not as abstract event objects in isolation.
- Tests must verify how the system behaves when that action happens in that state.
- Run events through real handlers/runtime paths whenever possible.
- Assert real effects:
  DB state, mappings, dedup decisions, retry state, outbound fanout.
- Do not rely mainly on "mock was called" tests.
- Mock only outer boundaries:
  network, platform SDK edges, time, random IDs.
- Prefer fakes/harnesses over brittle mocks.
- Every new behavior needs tests.
- Every bug fix needs a regression test.

## Required Coverage

- Post/thread creation in every supported direction.
- Comment/message fanout in every supported direction.
- Dedup and echo prevention, including starter/opening messages.
- Reply/parent resolution.
- Out-of-order delivery and retry behavior.
- Partial failure: failed targets must not block healthy targets.
- Routing correctness by subscription, community, and thread mapping.

## Comments

- Writing comments is mandatory. Do not omit comments.
- Writing docstrings is mandatory. Do not omit docstrings.
- Every module, class, public function, and non-trivial method must have a docstring.
- Docstrings must state responsibility, important constraints, and behavior.
- Every non-trivial function, handler, operation, and subtle code block must include comments explaining intent, invariants, constraints, or failure handling.
- If there is any doubt whether a comment is needed, write the comment.
- If there is any doubt whether a docstring is needed, write the docstring.
- Missing comments or docstrings are a project-rules violation.
- Comments must explain why the code exists, what contract it preserves, and what behavior it protects.
- Docstrings describe what the module/class/function/method is responsible for; inline comments explain why a subtle block works the way it does.
- Comments do not restate obvious syntax line by line, but they must still be present.
- Add comments especially around:
  dedup, retries, mapping logic, sync fanout, compatibility code, schema constraints, and failure paths.
- Prefer one strong comment before a subtle block over many weak inline comments, but never leave non-trivial logic uncommented.
- Keep comments short, factual, and human-readable.

## Design

- Build from observable behavior inward.
- Treat routing, dedup, fanout, and retry logic as correctness-critical.
- Prioritize readable code, clear project navigation, and human-readable structure.
- Keep module and function responsibilities narrow and explicit.
- Do not dump unrelated logic into one long module.
- Keep boundaries between handlers, connectors, persistence, formatting, and domain logic clear.
- Refactor toward clarity, but do not overengineer.
- Code should be easy to scan, easy to trace, and easy to change safely.
- If code is hard to test through runtime scenarios, simplify the design.

## Plans

- When explicitly asked to create a plan, create a new Markdown file in `plans/`.
- Name the file with a numeric prefix and short slug, in the form `01_plan_name.md`.
- Choose the plan name from the task context. Do not ask for the name unless the context is genuinely ambiguous.
- A plan must include these blocks:
  `Problem / Goal`,
  `Expected Behavior`,
  `Architecture`,
  `Touched Files`,
  `New Files`,
  `Implementation Steps`,
  `Tests`,
  `Open Questions` if any.
- In `Touched Files` and `New Files`, write plain file paths, not Markdown links.
- Additional blocks are allowed when the task needs them.
- Before writing a new plan or updating an existing plan, first study the relevant code carefully.
- Plans must be based on the real codebase and current behavior, not on vague conceptual descriptions.
- The plan must be concrete and implementation-oriented, not high-level filler.
- The plan must be well thought out and well described, but not padded with empty detail.
- When writing a plan, think through how the requested work can actually be implemented in this project.
- Base the plan on the real codebase, real constraints, and real integration points.
- Do not invent architecture, files, or implementation steps disconnected from the current project state.
- Plans must explicitly describe the implementation path through the current codebase: which runtime path is exercised, where data is read, where it is transformed, and where side effects happen.
- Plans must identify likely conflicts with existing contracts, tests, storage schemas, environment flags, routing, deduplication, and compatibility behavior.
- Plans must include a regression and blind-spot analysis: what existing behavior could be broken, what cases are not covered yet, and what evidence is needed before implementation.
- Plans must state when a refactor is required for clarity or testability and when a smaller targeted change is safer.
- Plans must avoid hidden scope expansion. If implementation appears to require work not described in the plan, stop and revise the plan before coding.
