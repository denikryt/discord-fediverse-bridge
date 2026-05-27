# 68 — Database repository split Stage 6: ActivityPub object repositories

## Problem / Goal

Stage 6 extracts generic ActivityPub object, source-message mapping, and remote actor cache persistence from `Database` into repository classes. The goal is to isolate federation object state from Discord fanout and community-specific persistence while preserving current object IDs, actor IDs, JSON storage, and lookup compatibility.

## Expected Behavior

Runtime behavior is unchanged.

Published object JSON, message mapping rows, activity/object/Discord lookup predicates, remote actor cache upserts, and actor key/inbox fields must remain identical. Existing `Database.*` methods remain temporary forwarding wrappers until Stage 8.

## Stage Boundary

Owns extraction for:

- `MessageMapping` persistence into `MessageMappingRepository`.
- `PublishedActivityObject` persistence into `ActivityPubObjectRepository`.
- `RemoteActor` persistence into `RemoteActorRepository`.

Does not own local-community runtime semantics, ActivityPub JSON rendering, Fedify gateway contracts, object/activity URL migration, remote subscription behavior, legacy Lemmy mappings, Discord fanout groups, schema changes, or federation fallback behavior.

## Architecture

`Database` remains the single owner of engine/session/create_all/migrate. Repositories share Database session ownership and do not create independent engines or session factories.

Add three repository modules under `src/db/repositories/` and instantiate them from `Database.__init__()` using the shared session provider:

```python
self.message_mappings = MessageMappingRepository(self.session)
self.activitypub_objects = ActivityPubObjectRepository(self.session)
self.remote_actors = RemoteActorRepository(self.session)
```

The current `Database.create_message_mapping(...)`, `Database.create_published_activity_object(...)`, and `Database.upsert_remote_actor(...)` methods become temporary wrappers and are removed by Stage 8 after call-site migration.

## Touched Files

- `plans/68_database_repository_split_stage6_activitypub_objects.md`
- `src/db/database.py`
- `src/db/repositories/__init__.py`
- `src/db/repositories/message_mappings.py`
- `src/db/repositories/activitypub_objects.py`
- `src/db/repositories/remote_actors.py`
- `docs/development/navigation.md`

## New Files

- `src/db/repositories/message_mappings.py`
- `src/db/repositories/activitypub_objects.py`
- `src/db/repositories/remote_actors.py`

## Implementation Steps

1. Move `MessageMapping` method bodies from `Database` into `MessageMappingRepository` without changing ID fields or lookup predicates.
2. Move `PublishedActivityObject` method bodies into `ActivityPubObjectRepository` without changing persisted JSON or Discord/object ID lookups.
3. Move `RemoteActor` method bodies into `RemoteActorRepository` without changing upsert/update behavior.
4. Import and instantiate the three repositories in `Database` using the shared session provider.
5. Replace extracted `Database` methods with temporary wrappers preserving current signatures and return annotations.
6. Update navigation docs for generic ActivityPub persistence.
7. Run focused object/mapping/actor tests and then full pytest.

## Tests

Focused Stage 6 command:

```bash
./.venv/bin/pytest -q \
  tests/test_db_federation_identity.py \
  tests/test_community_runtime_scenarios.py \
  tests/behavior/test_local_community_inbound_scenarios.py \
  tests/behavior/test_local_community_edit_delete_scenarios.py
```

Full suite:

```bash
./.venv/bin/pytest -q
```

## Regression / Blind-Spot Analysis

The primary risk is changing object/activity IDs or persisted JSON by treating extraction as an opportunity to normalize data. This stage must move method bodies mechanically.

The second risk is changing remote actor upsert behavior. Existing actor URL matching and cached inbox/key updates must remain identical.

The third risk is session ownership drift. Repositories must use the shared `Database.session` provider and must not create independent engines or session factories.

Temporary wrappers must not become permanent dual APIs; Stage 8 removes them.

## Open Questions

None blocking. This stage intentionally avoids call-site migration to keep object compatibility stable.
