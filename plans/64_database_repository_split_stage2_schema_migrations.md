# 64 — Database repository split Stage 2 schema and migration extraction

## Problem / Goal

Stage 2 extracts schema bootstrap and migration implementation details out of the large persistence method file while keeping `Database.create_all()` and `Database.migrate()` as the public infrastructure facade. This is the first physical package-layout step toward `src/db/` and must preserve all current runtime behavior.

## Expected Behavior

Runtime behavior is unchanged. Existing imports such as `from src.db import Database` and package-relative imports such as `from .db import Database` must continue to work. Existing callers must still invoke `Database.create_all()` and `Database.migrate()` with the same observable effects:

- `create_all()` creates the current SQLAlchemy metadata schema;
- `migrate()` applies the existing additive SQLite migrations;
- the Stage 2 local-community surface invariant checks still run during migration;
- `Database` remains the owner of engine/session/create_all/migrate.

## Stage Boundary

Owns:

- replace `src/db.py` with a `src/db/` package layout;
- move the active `Database` implementation into `src/db/database.py`;
- create `src/db/__init__.py` that re-exports `Database`;
- move `create_all()` implementation details into `src/db/schema.py`;
- move `migrate()`, `_table_columns()`, and `_verify_stage2_surface_invariants()` implementation details into `src/db/migrations.py`;
- keep `Database.create_all()` and `Database.migrate()` as public methods delegating to the extracted modules;
- adjust relative imports caused by moving `database.py` one package level deeper.

Does not own:

- moving domain repository methods out of `Database`;
- creating domain repository classes;
- changing application call sites;
- changing models or migration behavior beyond relocation;
- changing query semantics, schema design, ActivityPub behavior, Discord behavior, or command behavior.

## Architecture

Target package shape for this stage:

```text
src/db/
  __init__.py
  database.py
  schema.py
  migrations.py
```

`src/db/database.py` keeps the full `Database` facade and all domain methods in their current order. Only the schema/migration method bodies become delegators:

```python
from . import migrations, schema

class Database:
    def create_all(self) -> None:
        schema.create_all(self.engine)

    def migrate(self) -> None:
        migrations.migrate(self.engine)
```

`src/db/schema.py` imports `Base` from `src.models` and calls `Base.metadata.create_all(engine)`. `src/db/migrations.py` imports `Base`, `LocalCommunityThreadSurface`, and `LocalCommunityMessageSurface`, runs the same additive migration SQL, and verifies the same Stage 2 surface invariants.

This stage preserves the umbrella invariant:

```text
Database remains the single owner of engine/session/create_all/migrate.
Repositories share Database session ownership and do not create independent
engines or session factories.
```

No repository classes are introduced, and no independent engine/session factory is created.

## Touched Files

```text
src/db.py
src/db/__init__.py
src/db/database.py
src/db/schema.py
src/db/migrations.py
docs/development/navigation.md
docs/architecture/database-method-inventory.md
docs/architecture/overview.md
docs/architecture/event-flows.md
docs/discord_lemmy_bridge_compatibility_catalog.md
tests/test_stage5_schema_cleanup.py
```

## New Files

```text
plans/64_database_repository_split_stage2_schema_migrations.md
src/db/__init__.py
src/db/database.py
src/db/schema.py
src/db/migrations.py
```

`src/db.py` is removed because a module file and package directory with the same import name cannot coexist in the same directory.

## Implementation Steps

1. Move `src/db.py` to `src/db/database.py` with `git mv` through a temporary filename if necessary.
2. Change moved imports from `from .models import ...` to `from ..models import ...`.
3. Add `from . import migrations, schema` in `src/db/database.py`.
4. Replace `Database.create_all()` with a delegating call to `schema.create_all(self.engine)`.
5. Replace `Database.migrate()` with a delegating call to `migrations.migrate(self.engine)`.
6. Remove private migration helpers from `Database` after moving their exact implementation to module-level helpers in `src/db/migrations.py`.
7. Create `src/db/schema.py` with a purpose docstring and `create_all(engine)` implementation.
8. Create `src/db/migrations.py` with a purpose docstring, the existing migration list, surface-table creation, `_table_columns()`, and `_verify_stage2_surface_invariants()`.
9. Create `src/db/__init__.py` re-exporting `Database`.
10. Update database navigation and architecture docs so current persistence references point to `src/db/database.py`, `src/db/schema.py`, and `src/db/migrations.py` instead of removed `src/db.py`.
11. Run focused schema/import checks:
    - import `Database` from `src.db`;
    - create an in-memory SQLite database and run `create_all()` plus `migrate()`.
12. Run full pytest.
13. If static schema-cleanup guards still read `src/db.py`, update them to inspect `src/db/migrations.py` because Stage 2 removes the old module file.
14. Review the diff to confirm only schema/migration implementation moved, stale static guard paths were updated, and domain methods remain on `Database`.

## Tests

Focused checks:

```bash
./.venv/bin/python - <<'PY'
from src.db import Database

db = Database('sqlite+pysqlite:///:memory:')
db.create_all()
db.migrate()
print(Database.__module__)
PY
```

Full suite:

```bash
./.venv/bin/pytest -q
```

If full pytest fails, classify failures using the orchestrator categories. Stage-owned failures include import regressions, create_all/migrate regressions, stale static guard paths for the moved migration module, or changed schema invariant behavior.

## Regression / Blind-Spot Analysis

The main risk is Python import breakage because `src/db.py` becomes `src/db/`. `src/db/__init__.py` must preserve the existing `src.db.Database` import surface, and moved relative imports must use `..models`.

A second risk is accidentally changing migration order or invariant behavior while moving code. The migration SQL, table creation calls, `PRAGMA table_info` column detection, and RuntimeError messages must remain equivalent.

A third risk is accidentally moving domain persistence methods into repository modules too early. Stage 2 must leave domain methods on `Database`; Stage 3 through Stage 7 own domain extraction.

A fourth risk is hidden direct references to private migration helpers. The Stage 0 inventory excludes private helpers, and exact search should confirm `_table_columns` and `_verify_stage2_surface_invariants` are only internal migration implementation details.

## Open Questions

None. The package migration is required because Python cannot keep both `src/db.py` and `src/db/` as the same import target. The compatibility surface is `src.db.Database`, not the physical file path.
