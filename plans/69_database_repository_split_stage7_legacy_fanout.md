# 69 — Database repository split Stage 7: legacy mappings and Discord fanout groups

## Problem / Goal

Stage 7 extracts the remaining remote-community persistence domains from `Database`: older Lemmy post/comment mapping rows and Discord fanout group/delivery rows. The goal is to move the final large remote publish/fanout state out of the facade while preserving all existing dedup, reply preservation, backfill, edit/delete, and cross-channel fanout behavior.

## Expected Behavior

Runtime behavior is unchanged.

Legacy `PostLink`/`CommentLink` rows, `CommunityThreadGroup`/delivery rows, and `CommunityMessageGroup`/delivery rows must keep the same lookup predicates, insertion fields, ordering, and dedup semantics. Existing `Database.*` methods remain temporary forwarding wrappers until Stage 8.

## Stage Boundary

Owns extraction for:

- `PostLink` and `CommentLink` persistence into `LegacyLemmyMappingRepository`.
- `CommunityThreadGroup`, `CommunityThreadGroupDelivery`, `CommunityMessageGroup`, and `CommunityMessageGroupDelivery` persistence into `DiscordFanoutGroupRepository`.

Does not own local-community persistence, remote subscription lifecycle, user/registration/event receipt persistence, ActivityPub object JSON, remote actor cache, schema changes, fanout behavior changes, dedup key changes, or Discord message formatting changes.

## Architecture

`Database` remains the single owner of engine/session/create_all/migrate. Repositories share Database session ownership and do not create independent engines or session factories.

Add two repository modules under `src/db/repositories/` and instantiate them from `Database.__init__()` using the shared session provider:

```python
self.legacy_lemmy_mappings = LegacyLemmyMappingRepository(self.session)
self.discord_fanout_groups = DiscordFanoutGroupRepository(self.session)
```

The existing `Database.create_post_link(...)`, `Database.create_thread_group(...)`, and similar methods become temporary forwarding wrappers. Stage 8 removes these wrappers after call sites move to repository properties.

## Touched Files

- `plans/69_database_repository_split_stage7_legacy_fanout.md`
- `src/db/database.py`
- `src/db/repositories/__init__.py`
- `src/db/repositories/legacy_lemmy_mappings.py`
- `src/db/repositories/discord_fanout_groups.py`
- `docs/development/navigation.md`

## New Files

- `src/db/repositories/legacy_lemmy_mappings.py`
- `src/db/repositories/discord_fanout_groups.py`

## Implementation Steps

1. Move legacy Lemmy post/comment mapping method bodies from `Database` into `LegacyLemmyMappingRepository` without changing dedup predicates or inserted columns.
2. Move Discord fanout group/delivery method bodies into `DiscordFanoutGroupRepository` without changing lookup predicates, delivery row creation, ordering, or cross-channel fanout semantics.
3. Import and instantiate the two repositories in `Database` using the shared session provider.
4. Replace extracted `Database` methods with temporary wrappers preserving current signatures and return annotations.
5. Update navigation docs so remote publish/fanout maintainers can find the new repositories.
6. Run focused fanout/dedup tests and then full pytest.

## Tests

Focused Stage 7 command:

```bash
./.venv/bin/pytest -q \
  tests/test_phase3_message_fanout_scenarios.py \
  tests/test_phase4_reply_preservation.py \
  tests/test_phase5_inbound_ap_shared_groups.py \
  tests/test_phase6_dedup_hardening.py \
  tests/test_phase8_edit_delete_sync.py \
  tests/test_phase9_bidirectional_mirror_messages.py \
  tests/behavior/test_inbound_comment_backfill.py \
  tests/behavior/test_inbound_scenarios.py
```

Full suite:

```bash
./.venv/bin/pytest -q
```

## Regression / Blind-Spot Analysis

The main risk is changing dedup keys or fanout lookup paths while moving code. The extraction must preserve method bodies mechanically.

The second risk is breaking reply preservation or edit/delete delivery mapping by changing group/delivery queries. Existing ordering and predicates must remain unchanged.

The third risk is session ownership drift. Repositories must use the shared `Database.session` provider and must not create independent engines or session factories.

Wrappers are temporary extraction scaffolding only and are removed by Stage 8.

## Open Questions

None blocking. This stage intentionally keeps call sites on wrappers so remote fanout behavior is not mixed with repository API migration.
