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


## Documentation

- Do not make shit up or speculate. Write only what is supported by facts.
- Do not write flowery prose, padding, or pages of useless text. Every statement must give the reader concrete, useful information.
- Documentation serves a specific reader and purpose. It describes the current project, supported behavior, or a practical procedure in clear, concise, useful language.
- Documentation is not a changelog, development log, implementation diary, or record of every change. Do not add text merely to document that work was done.
- For each change, check whether an existing document's purpose and current content require an update. Update only the relevant information; a change can require no documentation edits.
- Read the purpose and relevant content of a document before editing it. Verify statements against the current code and established behavior, distinguish verified facts from uncertainty, and remove stale or unnecessary text when it is in scope.
- Preserve a document's established format when it serves its purpose; improve it when the format obscures useful information.
- Remove completed items from `dev/future_tasks.md`; do not replace them with completion notes.

### README.md

- Describe the project, its fundamental capabilities and boundaries, and where to find further information.
- Update the overview when it becomes inaccurate or a fundamental project capability changes. Keep routine changes and implementation details in their appropriate documents.

### docs/FEDERATION.md

- Describe the implemented federation profile for ActivityPub implementers: protocols, actors, activities, interoperability details, and meaningful limitations.
- Keep it factual and concise. Use federation documentation from Mitra, Lemmy, Harmony, Mbin, NodeBB, and Mastodon as references for structure and useful detail. Verify claims against this project's implementation. Put operational instructions in an appropriate runbook.

### docs/DEPLOY.md

- Provide practical instructions for deploying, updating, operating, and recovering the project, including relevant scenarios and prerequisites.
- Keep commands and procedures accurate for the current deployment.

### docs/known_issues.md

- Maintain the single known-issues document at `docs/known_issues.md`.
- Record current issues, limitations, and regressions with concise, factual descriptions and useful context.
- Update entries when their status changes; remove resolved issues. Keep implementation plans and development history outside this document.

### Writing and factual accuracy

- Use direct, affirmative wording. Negative wording is allowed only when it describes an actual, relevant behavior, restriction, or limitation.
- Do not use negative contrasts, invented alternatives, filler, or unsupported claims. State what the project does and what the reader needs to know.
- Only state facts supported by the code, observed behavior, or reliable sources. Identify uncertainty and the limits of verification explicitly.

## Plans

- When explicitly asked to create a plan, create a new Markdown file in `plans/`.
- Name the file with a numeric prefix and short slug, in the form `01_plan_name.md`. Choose the name from the task context without asking unless genuinely ambiguous.
- A plan must include these blocks:
  `Problem / Goal`,
  `Expected Behavior`,
  `Specification`,
  `Architecture`,
  `Touched Files`,
  `New Files`,
  `Implementation Steps`,
  `Tests`,
  `Open Questions` if any.
- In `Touched Files` and `New Files`, write plain file paths, not Markdown links.
- Describe function-level or module-level changes when the affected code path is known.
- Make implementation steps specific enough that a developer can execute them without re-discovering the design.
- Identify expected conflicts and compatibility risks before implementation begins.
- Explicitly distinguish generic protocol behavior from vendor-specific behavior when applicable.
- If implementation requires substantial work not described in the plan, stop and report the missing planning item before changing code.
