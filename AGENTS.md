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


## Documentation Maintenance

- Documentation is required maintenance, not optional cleanup.
- For every code, route, data model, gateway contract, deployment, or behavior change, check whether documentation is affected.
- Read the purpose paragraph of each potentially relevant document before editing it.
- Update only documentation whose stated responsibility covers the changed concept.
- Do not dump unrelated details into nearby documentation files.
- Preserve the established formatting style of the document family being edited.
- If no documentation update is needed, understand and be able to explain why the change is outside the existing documentation boundaries.
- Remove completed items from `dev/future_tasks.md`; do not replace them with completion notes.

## Planning Clarification Workflow

- When planning work, first study the relevant code, docs, notes, and existing plans before asking questions.
- Ask clarification questions only for non-obvious product or architecture decisions that are not already answered by the codebase or these rules.
- Ask one question at a time and wait for the answer before asking the next one.
- Each clarification question must offer three concrete choices, unless the user explicitly requests a different format.
- Do not ask obvious questions, repeat already answered questions, or ask questions whose answers are already specified by project rules.
- Write the plan only after the necessary decisions are fixed.

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
- Plans must include concrete implementation examples, not only conceptual descriptions.
- Plans must describe function-level or module-level changes when the affected code path is known.
- Plans should include example inputs, outputs, payload shapes, database rows, or assertions when they clarify the intended implementation.
- Avoid shallow phrases such as "normalize payload" or "update handler" unless followed by the exact normalization rules or handler behavior.
- Implementation steps must be specific enough that a developer can execute them without re-discovering the whole design.

- Plans must identify expected conflicts and compatibility risks before implementation begins.
- Plans must include a regression and blind-spot analysis for behavior that could be accidentally changed.
- Plans must explicitly state when a proposed path is generic protocol behavior rather than vendor-specific behavior.
- If implementation requires work that is not described in the plan, stop and report the missing planning item before changing code.

## Known Issues Journal

- Maintain a short known-issues journal at `notes/known_issues.md`.
- Update the journal during development when a new issue, limitation, regression, verified behavior, or resolved issue is discovered.
- Keep entries short and factual. The journal records status and findings; it is not a replacement for implementation plans.
- Move or rewrite entries when their status changes, for example from open issue to fixed/verified or known behavior.
- When a note file under `notes/` must be included in a commit or bundle, add it explicitly with `git add -f notes/known_issues.md` because `notes/` may be ignored.