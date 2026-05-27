# 63 — Database repository split Stage 1 navigation banners

## Problem / Goal

Stage 1 prepares `src/db.py` for repository extraction by making its existing persistence domains easy to review without moving code. Stage 0 created `docs/architecture/database-method-inventory.md`, which maps every public `Database` method to a target repository group. This stage aligns the in-file navigation banners and development navigation docs with that inventory.

## Expected Behavior

Runtime behavior is unchanged. This is a navigation/documentation stage only. It does not move methods, create repository classes, change imports, change call sites, alter queries, or change schema/migration behavior.

## Stage Boundary

Owns:

- replace vague `Navigation marker for repository helpers in this persistence area` comments in `src/db.py` with domain-specific banners;
- add additional section banners where current broad blocks contain multiple future repository domains;
- preserve all existing method bodies and method order;
- update `docs/development/navigation.md` so maintainers know to read the method inventory before editing persistence code.

Does not own:

- physical file split;
- `src/db/` package creation;
- repository class creation;
- call-site migration;
- schema/migration extraction;
- behavior changes or new tests for already covered behavior.

## Architecture

`Database` remains the only executable persistence facade. Stage 1 only adds comments around existing method groups. The target repository grouping follows `docs/architecture/database-method-inventory.md`, but no repository API is introduced yet.

This stage preserves the umbrella invariant because it does not change ownership or executable behavior:

```text
Database remains the single owner of engine/session/create_all/migrate.
Repositories share Database session ownership and do not create independent
engines or session factories.
```

## Touched Files

```text
src/db.py
docs/development/navigation.md
```

## New Files

```text
plans/63_database_repository_split_stage1_navigation_banners.md
```

## Implementation Steps

1. Re-read `docs/architecture/database-method-inventory.md` and use it as the source of truth for Stage 1 domain boundaries.
2. Update the existing top-level banners in `src/db.py` so each has a concrete purpose paragraph, for example:
   - engine/session/schema helpers;
   - legacy Lemmy post/comment mapping helpers;
   - inbound ActivityPub event receipt helpers;
   - remote subscription helpers;
   - user and registration-session helpers;
   - local community identity helpers;
   - remote subscriber helpers;
   - local subscriber helpers;
   - local-community canonical content helpers;
   - local-community Discord surface helpers;
   - local-community relay helpers;
   - message mapping and published ActivityPub object helpers;
   - remote actor cache helpers;
   - shared Discord fanout group helpers;
   - bridge actor follow helpers.
3. Insert sub-banners where a current broad block contains multiple future repositories, without moving any methods.
4. Remove stale phase comments only when they duplicate the new Stage 1 banners and do not describe runtime behavior.
5. Update `docs/development/navigation.md` database-related entries to point to `docs/architecture/database-method-inventory.md` alongside `src/db.py` and `docs/architecture/database-map.md`.
6. Verify no executable lines changed by reviewing `git diff --word-diff` and/or checking that method bodies are unchanged except comments.
7. Run full pytest.

## Tests

Required command:

```bash
./.venv/bin/pytest -q
```

No focused tests are required because this stage changes only comments and documentation. If full pytest fails, classify failures using the orchestrator categories.

## Regression / Blind-Spot Analysis

The main regression risk is accidentally changing executable code while editing a large file. The implementation must keep method order and method bodies intact. A diff review should show only comment/docstring/navigation documentation changes.

A second risk is adding banners that do not match the Stage 0 inventory. The inventory remains the source of truth; banners should describe the same future repository groups rather than inventing new names.

A third risk is over-documenting temporary wrapper semantics before wrappers exist. Stage 1 must not promise repository APIs or dual call paths. It only documents current `Database` method regions.

## Open Questions

None. Stage 0 inventory exists at `docs/architecture/database-method-inventory.md`, so Stage 1 can proceed as the first behavior-preserving implementation stage after the inventory commit.
