# 59 — Stage 5 obsolete schema migration cleanup

## Problem / Goal

The compatibility-cleanup umbrella has already closed the runtime naming and
behavioral compatibility stages:

- Stage 0 verified the live deployment database and chose the `migrate existing DB`
  path;
- Stage 1 removed the old remote-subscriber naming compatibility layer from
  Python and gateway runtime code;
- Stage 2 removed the `DiscordPublishService` naming compatibility layer;
- Stage 3 is effectively complete because old one-surface local-community helper
  APIs are no longer present;
- Stage 4 removed the old direct `Accept(Follow)` subscription-acceptance path.

The remaining compatibility layer is old SQLite schema upgrade support inside
`src/db.py`. The code still knows how to upgrade databases from pre-Stage-1 and
pre-Stage-2 shapes:

```text
local_community_followers
  -> remote_subscribers

local_community_threads.discord_thread_id
local_community_threads.discord_starter_message_id
local_community_messages.discord_message_id
local_community_messages.parent_discord_message_id
  -> local_community_thread_surfaces / local_community_message_surfaces
```

That upgrade support was correct while old databases had to be preserved. After
Stage 0, the project has explicitly verified the deployment database and selected
the current schema as the baseline. Stage 5 should remove those obsolete
old-schema migration branches so the codebase has one current schema model and
one current local-community persistence shape.

Stage 5 is not a federation interoperability cleanup. Do not remove ActivityPub,
Lemmy, Mastodon, Fedify, Discord formatting, WebFinger, actor URL, or old
Discord autocomplete compatibility paths in this stage.

## Expected Behavior

### Fresh and current-schema databases still initialize and migrate

A fresh SQLite database should still work through the normal project path:

```text
Database(...)
  -> Base.metadata.create_all(...)
  -> Database.migrate()
```

Expected result:

- all current tables exist;
- `remote_subscribers` exists;
- `local_subscribers` exists;
- `local_community_thread_surfaces` exists;
- `local_community_message_surfaces` exists;
- `local_subscriber_id` exists on both surface tables;
- `channel_community_subscriptions.discord_guild_id` exists;
- `Database.migrate()` can be run repeatedly without changing current data.

### Old pre-Stage-1 table migration is no longer supported

If a database still contains only the old table:

```text
local_community_followers
```

Stage 5 code should not rename/copy/drop that table into `remote_subscribers`.
The current supported baseline is `remote_subscribers`.

The implementation does not need to add a new hard failure for this old table.
It is enough to remove the compatibility branch. A database that still depends
on the old table is outside the Stage 5 supported baseline and may fail later
because current code reads `remote_subscribers`.

### Old pre-Stage-2 canonical Discord-id migration is no longer supported

If a database still has old canonical local-community columns:

```text
local_community_threads.discord_thread_id
local_community_threads.discord_starter_message_id
local_community_messages.discord_message_id
local_community_messages.parent_discord_message_id
```

Stage 5 code should not backfill host surfaces from those columns and should
not rebuild canonical tables to remove them. The supported baseline is already:

```text
local_community_threads
local_community_messages
local_community_thread_surfaces
local_community_message_surfaces
```

with Discord ids stored on surface rows, not canonical rows.

### Current surface invariants are still checked

Keep the current invariant check that every canonical local-community thread and
message has exactly one host surface in the supported current schema. That check
is not obsolete compatibility code. It protects the current canonical/surface
model from ambiguous routing and edit/delete ownership.

### Current additive migrations remain available

Do not remove additive migrations that are still part of the supported current
schema maintenance path, especially:

```text
channel_community_subscriptions.discord_guild_id
local_community_thread_surfaces.local_subscriber_id
local_community_message_surfaces.local_subscriber_id
```

Those are not old-schema translation branches in the same sense as the removed
pre-Stage-1/pre-Stage-2 migrations. They are safe idempotent column guards for
current deployments and migrate-only test fixtures.

## Architecture

### Keep `Database.migrate()` but narrow its responsibility

Do not delete `Database.migrate()`. It still owns current additive schema
maintenance and invariant checks.

After Stage 5, `Database.migrate()` should be easier to scan:

```text
1. ensure current surface tables exist for migrate-only callers;
2. add supported current additive columns if missing;
3. verify current surface invariants;
4. commit.
```

It should no longer contain branches whose only purpose is to translate old
project schemas into the current schema.

### Remove old-schema private helpers with their call sites

Delete these private methods if no call sites remain after the migration cleanup:

```text
_backfill_stage2_thread_surfaces(...)
_backfill_stage2_message_surfaces(...)
_rebuild_stage2_local_community_threads(...)
_rebuild_stage2_local_community_messages(...)
```

They exist only to support old canonical rows with direct Discord-id columns.
The current runtime and repository helpers already read/write surface rows.

### Do not touch current surface APIs

These fields and helpers are current model, not compatibility:

```text
LocalCommunityThreadSurface.discord_thread_id
LocalCommunityThreadSurface.discord_starter_message_id
LocalCommunityMessageSurface.discord_message_id
LocalCommunityMessageSurface.parent_discord_message_id

get_local_community_thread_surface_by_discord_thread_id(...)
get_local_community_thread_surface_by_starter_message_id(...)
get_local_community_message_surface_by_discord_message_id(...)
```

Stage 5 must not remove or rename them.

### Update documentation by responsibility

The compatibility catalog should no longer describe old SQLite schema migrations
as active code. It should instead say that those pre-Stage-1/pre-Stage-2 upgrade
paths were intentionally removed after Stage 0 verified the live DB baseline.

`notes/known_issues.md` should record the Stage 5 cleanup result in one short
entry.

Avoid editing federation documents unless the implementation unexpectedly
changes federation behavior. It should not.

## Touched Files

```text
src/db.py
docs/discord_lemmy_bridge_compatibility_catalog.md
notes/known_issues.md
tests/behavior/test_local_community_surface_stage2_scenarios.py
tests/test_stage1_remote_subscriber_naming.py
```

## New Files

```text
plans/59_stage5_obsolete_schema_migration_cleanup.md
tests/test_stage5_schema_cleanup.py
```

## Implementation Steps

### 1. Add Stage 5 cleanup regression tests first

Create `tests/test_stage5_schema_cleanup.py` before editing `src/db.py`.

Required tests:

1. Current-schema migrate remains idempotent.
   - Given a fresh database created through current models.
   - Given at least one local community, canonical thread, canonical message,
     host thread surface, and host message surface.
   - When `Database.migrate()` runs twice.
   - Then row counts stay stable.
   - Then `local_subscriber_id` columns exist on both surface tables.
   - Then canonical tables still do not expose old Discord-id columns.

2. Obsolete Stage 1 table upgrade code is absent.
   - Assert that `Database` no longer exposes code paths or helper names that
     mention `local_community_followers` in runtime source loaded from
     `src/db.py`.
   - This is a static guard against reintroducing the old table migration branch.

3. Obsolete Stage 2 rebuild helpers are absent.
   - Assert `Database` does not have:
     - `_backfill_stage2_thread_surfaces`
     - `_backfill_stage2_message_surfaces`
     - `_rebuild_stage2_local_community_threads`
     - `_rebuild_stage2_local_community_messages`
   - This locks the decision that pre-surface canonical migration is no longer
     supported.

These tests are intentionally cleanup-oriented. They should not construct an old
pre-Stage-2 database and expect it to migrate successfully, because Stage 5
removes that support.

### 2. Remove the Stage 1 old-table migration branch

In `src/db.py`, delete the branch in `Database.migrate()` that checks:

```python
if "local_community_followers" in existing_tables:
    ...
```

Also remove any comments describing active migration from
`local_community_followers` to `remote_subscribers`.

After this step, no runtime Python or gateway code should mention
`local_community_followers`.

### 3. Remove the Stage 2 old-canonical-column migration branches

In `Database.migrate()`, remove checks and calls for:

```python
if "discord_thread_id" in thread_columns:
    self._backfill_stage2_thread_surfaces(conn)

if "discord_message_id" in message_columns:
    self._backfill_stage2_message_surfaces(conn)

if "discord_thread_id" in thread_columns:
    self._rebuild_stage2_local_community_threads(conn)

if "discord_message_id" in message_columns:
    self._rebuild_stage2_local_community_messages(conn)
```

Keep current additive column checks and `_verify_stage2_surface_invariants(conn)`.
The invariant method name may remain as-is if renaming it would create noisy
unrelated churn; alternatively rename it to a neutral current-schema name only
if the change is small and call sites/tests are updated together.

### 4. Delete obsolete private migration helpers

Remove these methods from `src/db.py`:

```python
_backfill_stage2_thread_surfaces(...)
_backfill_stage2_message_surfaces(...)
_rebuild_stage2_local_community_threads(...)
_rebuild_stage2_local_community_messages(...)
```

Do not remove `_table_columns(...)`; it is still useful for current additive
column migrations.

Do not remove `_verify_stage2_surface_invariants(...)` unless it is replaced by
an equivalent current-schema invariant checker.

### 5. Update old tests that still require removed migration behavior

`tests/behavior/test_local_community_surface_stage2_scenarios.py` currently
contains a migration test that builds a pre-Stage-2 schema and asserts automatic
backfill/rebuild succeeds. That test directly contradicts Stage 5.

Replace it with a current-baseline assertion:

- current canonical rows have no direct Discord-id columns;
- current surface rows exist and preserve Discord ids;
- running `migrate()` twice remains idempotent;
- no local-subscriber surfaces are created by migrate alone.

Do not delete Stage 2 runtime behavior tests that verify current surface-based
behavior. Only remove or rewrite the old-database-upgrade expectation.

`tests/test_stage1_remote_subscriber_naming.py` may be extended to include the
old-table static guard, or the new Stage 5 test file may own that check. Avoid
asserting historical migration behavior there.

### 6. Update compatibility documentation and known issues

Update `docs/discord_lemmy_bridge_compatibility_catalog.md`:

- mark the pre-Stage-1 `local_community_followers` migration as removed;
- mark the pre-Stage-2 canonical Discord-id migration as removed;
- keep federation, Discord formatting, WebFinger, actor URL, timestamp, and old
  autocomplete compatibility sections unchanged unless the code changed them.

Update `notes/known_issues.md` with one short factual entry:

```text
Stage 5 compatibility cleanup removed obsolete pre-Stage-1/pre-Stage-2 SQLite
schema upgrade paths from Database.migrate(); current deployments are expected
to start from the Stage 0 verified schema baseline.
```

### 7. Verify there are no runtime old-schema references left

Run static checks:

```bash
grep -R "local_community_followers" -n src fedify-gateway tests docs notes
grep -R "_backfill_stage2\|_rebuild_stage2" -n src tests
grep -n 'if "discord_thread_id" in thread_columns\|if "discord_message_id" in message_columns' src/db.py
```

Expected result:

- no matches in `src/` or `fedify-gateway/` for `local_community_followers`;
- no `_backfill_stage2` or `_rebuild_stage2` helpers;
- no old canonical-column migration branches;
- documentation/notes may mention the removed paths only as historical cleanup,
  not as active behavior.

## Tests

Follow TDD for the cleanup guard tests.

Run at minimum:

```bash
./.venv/bin/pytest -q tests/test_stage5_schema_cleanup.py
./.venv/bin/pytest -q tests/behavior/test_local_community_surface_stage2_scenarios.py
./.venv/bin/pytest -q tests/test_stage1_remote_subscriber_naming.py
./.venv/bin/pytest -q
```

If the full pytest run reports failures outside Stage 5's touched behavior, do
not automatically repair unrelated areas as part of this stage. Analyze and
report:

- failing test name;
- command;
- error summary;
- whether it is Stage-5-caused, stale cleanup expectation, environment-only, or
  unrelated.

Stage 5 is complete only when:

- the new Stage 5 cleanup tests pass;
- the rewritten Stage 2 surface tests pass;
- full pytest passes, or any unrelated/environment failure is explicitly
  analyzed and reported.

## Expected Conflicts / Compatibility Risks

### Old-database upgrade support is intentionally removed

After this stage, a database that still has only `local_community_followers` or
old canonical Discord-id columns is no longer supported by automatic migration.
That is intentional and follows Stage 0's decision to proceed from the verified
current schema baseline.

### Migrate-only fixtures still need current table creation

Some tests may instantiate `Database` and call `migrate()` without going through
full app startup. Keep `Base.metadata.create_all(..., tables=[surface tables])`
or equivalent current table creation if those fixtures depend on it.

### Invariant checks must not become old-schema migration checks

Do not weaken `_verify_stage2_surface_invariants(...)` just because old migration
branches are removed. Current data should still fail loudly if canonical
local-community rows do not have exactly one host surface.

### Documentation can easily overstate removal

The compatibility catalog should be precise: Stage 5 removes old SQLite schema
upgrade paths only. It does not remove federation compatibility, Discord message
formatting compatibility, actor URL aliases, timestamp normalization, or old
Discord autocomplete value parsing.

## Regression and Blind-Spot Analysis

### Fresh database path

Removing old migration branches must not break a new database. The fresh path
uses current ORM models, so tests must verify fresh create/migrate still
produces all current tables and columns.

### Current migrated database path

Stage 0 selected a migrated current schema. The most important regression is
breaking `migrate()` on that current schema by leaving references to removed old
columns. The idempotency test must run `migrate()` twice against current data.

### Stage 2 behavior tests

The old pre-Stage-2 migration test is no longer valid after this cleanup. The
blind spot is accidentally deleting too much test coverage. Preserve behavior
coverage for current surface rows and only remove the old upgrade guarantee.

### Surface field confusion

Do not confuse old canonical Discord-id columns with current surface Discord-id
columns. `discord_thread_id`, `discord_starter_message_id`, `discord_message_id`,
and `parent_discord_message_id` remain valid on surface rows.

### Notes and docs drift

Because this cleanup changes support policy rather than runtime behavior, docs
can become misleading if they continue to say the old migrations are active.
The compatibility catalog and known-issues journal must be updated in the same
commit.

## Open Questions

None. Stage 0 already selected the current migrated DB baseline, so Stage 5 can
remove old-schema upgrade support.
