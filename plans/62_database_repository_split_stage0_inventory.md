# 62 — Database repository split Stage 0 inventory

## Problem / Goal

Stage 0 must create a checked-in inventory of the current `Database` public method surface before any repository extraction begins. The current `docs/architecture/database-map.md` maps tables to ownership areas, but it does not map every public `Database` method to a target repository, primary call sites, and test coverage. This stage fills that gap so Stage 1 can proceed without silently skipping the orchestrator's Stage 0 gate.

## Expected Behavior

Runtime behavior is unchanged. This is a documentation and inventory stage only. It does not move code, change imports, change schema behavior, alter call sites, or introduce repositories.

## Stage Boundary

Owns:

- enumerate every public/non-private method on `src/db.py::Database`;
- map each method to the target repository or infrastructure owner from `plans/60_database_repository_split_umbrella.md`;
- record primary source call sites for each repository group;
- record relevant tests for each repository group;
- identify risky extraction groups before Stage 1 begins;
- update navigation/database documentation so maintainers can find the inventory.

Does not own:

- adding section banners to `src/db.py` beyond what already exists;
- creating `src/db/` package files;
- creating repository classes;
- changing application call sites;
- changing runtime behavior, migrations, schema, federation semantics, Discord behavior, or command behavior.

## Architecture

The inventory will live under `docs/architecture/database-method-inventory.md`. `docs/architecture/database-map.md` remains the table-level schema ownership document and will link to the method inventory.

This stage preserves the umbrella invariant because it does not change code:

```text
Database remains the single owner of engine/session/create_all/migrate.
Repositories share Database session ownership and do not create independent
engines or session factories.
```

The inventory records future repository ownership only. It is not an implemented API contract until later extraction stages commit code.

## Touched Files

```text
docs/architecture/database-map.md
```

## New Files

```text
plans/62_database_repository_split_stage0_inventory.md
docs/architecture/database-method-inventory.md
```

## Implementation Steps

1. Inspect `src/db.py::Database` with an AST-based method list so private helpers are excluded and public methods are not missed.
2. Inspect `src/` and `tests/` call sites with exact method-token searches such as `.<method_name>(`.
3. Group methods by the target repositories named in the umbrella plan:
   - database infrastructure;
   - legacy Lemmy mappings;
   - event receipts;
   - remote subscriptions;
   - users;
   - registration sessions;
   - local communities;
   - remote subscribers;
   - local subscribers;
   - local-community content;
   - local-community surfaces;
   - local-community relay;
   - ActivityPub objects and message mappings;
   - remote actors;
   - Discord fanout groups;
   - bridge actor follows.
4. Write `docs/architecture/database-method-inventory.md` with:
   - a purpose paragraph;
   - a repository-group inventory table;
   - method lists for every public `Database` method;
   - primary call sites;
   - relevant test files;
   - extraction risks.
5. Add a short pointer from `docs/architecture/database-map.md` to the new method inventory.
6. Verify the inventory contains every public `Database` method exactly once.
7. Run the full Python suite as required by the orchestrator.
8. Review the diff to confirm the stage is documentation-only.

## Tests

Required command:

```bash
./.venv/bin/pytest -q
```

No focused behavior tests are required because this stage only adds documentation. If dependencies are unavailable, report the exact environment failure and do not classify it as a stage-owned regression.

## Regression / Blind-Spot Analysis

The main risk is an incomplete inventory that lets a later extraction stage miss hidden call sites or duplicate a method in the wrong repository. To reduce that risk, the method list must be generated from the current `Database` class and checked for one-to-one coverage in the documentation.

A second risk is confusing table ownership with method ownership. The existing database map remains table-level documentation; the new inventory explicitly owns method-level repository planning.

No runtime regression is expected because this stage does not edit executable code. The final diff must contain only documentation and plan files.

## Open Questions

None for implementation. Stage 0 is not complete in the current checked-in documentation because no document maps every public `Database` method to target repositories, primary call sites, and tests. This stage therefore creates the required inventory, and the repository split series will require nine commits total if Stage 1 through Stage 8 are all completed after it.
