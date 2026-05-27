# 70 — Database repository split Stage 8: finalize repository API

## Problem / Goal

Stage 8 removes temporary `Database` domain forwarding wrappers and makes repository properties the single supported persistence API for domain operations. After this stage, `Database` should be a small engine/session/schema/repository container rather than a duplicate facade for every persistence domain.

## Expected Behavior

Runtime behavior is unchanged.

All command, runtime, dashboard, HTTP, ActivityPub, and test call sites must reach the same repository method bodies introduced in Stages 3 through 7. The only intended API boundary change is that domain operations are called through `Database` repository properties instead of `Database.method(...)` wrappers.

## Stage Boundary

Owns:

- migrating call sites from `database.domain_method(...)` to `database.repository.domain_method(...)`;
- removing temporary `Database` forwarding wrappers for all domain persistence operations;
- reducing `src/db/database.py` to infrastructure ownership plus repository construction;
- updating persistence/navigation docs to describe the final layout.

Does not own schema changes, runtime behavior changes, ActivityPub payload compatibility changes, Lemmy/Mastodon rendering changes, Fedify gateway contract changes, Discord formatting changes, subscription semantics changes, local-community semantics changes, or new command/dashboard behavior.

## Architecture

`Database` remains the single owner of engine/session/create_all/migrate. Repositories share Database session ownership and do not create independent engines or session factories.

Final supported shape:

```python
database.create_all()
database.migrate()
with database.session(): ...

database.local_communities.create_local_community(...)
database.remote_subscriptions.get_subscription_by_channel(...)
database.bridge_actor_follows.mark_bridge_actor_follow_accepted(...)
```

No domain operation should remain callable through both `Database.method(...)` and `Database.repository.method(...)` after this stage.

## Touched Files

- `plans/70_database_repository_split_stage8_finalize_api.md`
- `src/db/database.py`
- runtime, operation, route, dashboard, and test files that still call temporary wrappers
- `docs/development/navigation.md`
- `docs/architecture/database-method-inventory.md`

## New Files

No new source files are expected.

## Implementation Steps

1. Build a complete map from every temporary wrapper method to its repository property.
2. Replace project call sites outside `src/db/database.py` and repository modules so domain calls use repository properties directly.
3. Remove all temporary domain wrapper methods from `Database`.
4. Remove unused domain model/select/datetime imports from `src/db/database.py` so it contains only infrastructure and repository construction.
5. Update docs to state that `Database` no longer owns domain method wrappers and repository properties are the supported API.
6. Search for any remaining direct `Database` domain wrapper calls.
7. Run focused cross-domain tests and then full pytest.

## Tests

Focused Stage 8 command:

```bash
./.venv/bin/pytest -q \
  tests/test_registration_flow.py \
  tests/test_follow_subscription_flow.py \
  tests/test_db_federation_identity.py \
  tests/test_community_runtime_scenarios.py \
  tests/behavior/test_local_subscriber_stage1_scenarios.py \
  tests/behavior/test_subscription_scenarios.py \
  tests/behavior/test_inbound_scenarios.py \
  tests/behavior/test_local_community_stage5_participant_edit_delete_scenarios.py
```

Full suite:

```bash
./.venv/bin/pytest -q
```

## Regression / Blind-Spot Analysis

The main risk is missing a call site and leaving code calling a removed wrapper. A repository-property migration search must cover `src/`, `tests/`, and helper files.

The second risk is accidental replacement of non-Database methods with coincidentally similar names. The method names are project-specific, but the final diff and full suite must verify replacements.

The third risk is keeping duplicate APIs. After this stage, domain wrappers must be gone from `Database`; only infrastructure methods `session()`, `create_all()`, and `migrate()` remain.

The fourth risk is documentation drift. The inventory document was useful for staged migration, but final docs must state that repository properties are now the API boundary.

## Open Questions

None blocking. Repository method names remain the extracted names from Stages 3 through 7; this stage removes duplicate call paths rather than renaming repository methods.
