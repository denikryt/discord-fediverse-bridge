# 65 — Database repository split Stage 3: local-community repositories

## Problem / Goal

Stage 3 extracts the local-community persistence domains from `Database` into repository classes while preserving the existing `Database` method surface as temporary forwarding wrappers. The immediate goal is a reviewable mechanical extraction: local-community data access code gets clear repository ownership, but runtime call sites can keep using their existing `Database.*` calls until Stage 8 removes the wrappers.

## Expected Behavior

Runtime behavior is unchanged.

Existing local-community flows must keep the same database rows, lookup keys, fanout target selection, retry decisions, participant ownership rules, and edit/delete routing. The old `Database.*` methods remain callable in this stage, but only as temporary wrappers around repository properties.

## Stage Boundary

Owns extraction for these Stage 3 groups from the inventory:

- `LocalCommunityRepository`: local community identity rows and lookups.
- `RemoteSubscriberRepository`: remote followers of bridge-owned local communities.
- `LocalSubscriberRepository`: same-instance local subscriber rows.
- `LocalCommunityContentRepository`: canonical local-community thread/message rows.
- `LocalCommunitySurfaceRepository`: Discord surface rows for canonical local-community content.
- `LocalCommunityRelayRepository`: relay source/delivery persistence.

Does not own remote subscription lifecycle, bridge actor follows, user/registration/event receipts, ActivityPub object storage, remote actor cache, legacy Lemmy mappings, Discord fanout groups, schema changes, ActivityPub payload changes, or local-community runtime semantics.

## Architecture

`Database` remains the single owner of engine/session/create_all/migrate. Repositories share Database session ownership and do not create independent engines or session factories.

Add a small repository base under `src/db/repositories/` that stores the shared session provider. Each Stage 3 repository receives `self.session` from the `Database` instance. Repository methods continue to open one short transactional session per operation, matching the current method-level session behavior.

`Database.__init__()` will construct repository properties:

```python
self.local_communities = LocalCommunityRepository(self.session)
self.remote_subscribers = RemoteSubscriberRepository(self.session)
self.local_subscribers = LocalSubscriberRepository(self.session)
self.local_community_content = LocalCommunityContentRepository(self.session)
self.local_community_surfaces = LocalCommunitySurfaceRepository(self.session)
self.local_community_relay = LocalCommunityRelayRepository(self.session)
```

The existing `Database.create_local_community(...)` style methods become temporary wrappers that delegate to those properties. These wrappers are removed by Stage 8 after call sites are migrated to repository APIs.

## Touched Files

- `plans/65_database_repository_split_stage3_local_communities.md`
- `src/db/database.py`
- `src/db/repositories/__init__.py`
- `src/db/repositories/base.py`
- `src/db/repositories/local_communities.py`
- `src/db/repositories/remote_subscribers.py`
- `src/db/repositories/local_subscribers.py`
- `src/db/repositories/local_community_content.py`
- `src/db/repositories/local_community_surfaces.py`
- `src/db/repositories/local_community_relay.py`
- `docs/development/navigation.md`

## New Files

The six Stage 3 repository modules and the repository package/base files listed above are new files.

## Implementation Steps

1. Create `src/db/repositories/` with a base repository class that accepts the shared session provider.
2. Move the Stage 3 method bodies from `Database` into the six repository classes without changing query predicates, inserted columns, commit timing, or return values.
3. Import and instantiate the six repositories in `Database.__init__()`.
4. Replace each moved `Database` method with a temporary forwarding wrapper whose signature and return annotation remain compatible with existing callers.
5. Keep wrapper names identical to current `Database` method names so no runtime call-site migration is required in this stage.
6. Update navigation docs to point maintainers at the new local-community repository modules while noting that wrappers are transitional until Stage 8.
7. Run focused local-community tests and then full pytest.

Concrete example:

```python
class Database:
    def get_local_subscriber_by_channel(self, discord_channel_id: int) -> LocalSubscriber | None:
        return self.local_subscribers.get_local_subscriber_by_channel(discord_channel_id)
```

The repository method keeps the current select predicate exactly:

```python
with self.session() as session:
    return session.scalar(select(LocalSubscriber).where(LocalSubscriber.discord_channel_id == discord_channel_id))
```

## Tests

Focused Stage 3 command:

```bash
./.venv/bin/pytest -q \
  tests/behavior/test_local_subscriber_stage1_scenarios.py \
  tests/behavior/test_local_community_surface_stage2_scenarios.py \
  tests/behavior/test_local_community_stage3_local_subscriber_sync_scenarios.py \
  tests/behavior/test_local_community_stage4_local_subscriber_origin_scenarios.py \
  tests/behavior/test_local_community_stage5_participant_edit_delete_scenarios.py
```

Full suite:

```bash
./.venv/bin/pytest -q
```

## Regression / Blind-Spot Analysis

The highest regression risk is session ownership drift. Repository classes must not create engines or session factories; they must call the shared `Database.session` provider.

The second risk is changing local-community runtime semantics while moving code. This stage must not alter any model field, uniqueness rule, ordering, lookup predicate, or retry marker. Method bodies should move mechanically.

The third risk is hidden call sites. Existing `Database.*` wrappers intentionally remain in place for this stage so runtime modules, tests, and helpers continue to work even if not all call sites are found by static search. Stage 8 owns wrapper removal.

The fourth risk is repository naming drift. This stage keeps wrapper method names unchanged and may keep repository method names identical to the old method names to reduce mechanical extraction risk. Shorter repository names can be introduced in a later cleanup only if Stage 8 explicitly covers the call-site migration.

## Open Questions

None blocking. The stage intentionally chooses temporary wrappers instead of direct call-site migration to keep this large extraction reviewable and behavior-preserving.
