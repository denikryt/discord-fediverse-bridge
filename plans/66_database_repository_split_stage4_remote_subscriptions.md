# 66 — Database repository split Stage 4: remote subscription repositories

## Problem / Goal

Stage 4 extracts remote community subscription lifecycle persistence and bridge actor follow persistence from `Database` into explicit repositories. The goal is to isolate remote `/subscribe-channel` / `/unsubscribe-channel` state from local-community persistence while keeping current runtime call sites compatible through temporary `Database` forwarding wrappers.

## Expected Behavior

Runtime behavior is unchanged.

Remote subscribe/unsubscribe rows, bridge actor Follow tracking, inbound `Accept(Follow)` matching, stale inbound filtering, allowlist behavior, and subscription counts must use the same predicates and state transitions as before. Existing `Database.*` methods stay callable in this stage as temporary wrappers and are removed by Stage 8.

## Stage Boundary

Owns extraction for:

- `ChannelCommunitySubscription` persistence into `RemoteSubscriptionRepository`.
- `BridgeActorFollow` persistence into `BridgeActorFollowRepository`.

Does not own local subscriber behavior, local-community fanout, user/registration/event receipt persistence, ActivityPub object storage, legacy Lemmy mappings, Discord fanout groups, schema changes, ActivityPub JSON changes, or any subscribe/unsubscribe semantic change.

## Architecture

`Database` remains the single owner of engine/session/create_all/migrate. Repositories share Database session ownership and do not create independent engines or session factories.

Add two repository modules under `src/db/repositories/` and instantiate them from `Database.__init__()` using the existing shared `self.session` provider:

```python
self.remote_subscriptions = RemoteSubscriptionRepository(self.session)
self.bridge_actor_follows = BridgeActorFollowRepository(self.session)
```

Keep the current `Database.get_subscription_by_channel(...)` and `Database.create_bridge_actor_follow(...)` style methods as temporary forwarding wrappers. Stage 8 removes these wrappers after call sites move to repository properties.

## Touched Files

- `plans/66_database_repository_split_stage4_remote_subscriptions.md`
- `src/db/database.py`
- `src/db/repositories/__init__.py`
- `src/db/repositories/remote_subscriptions.py`
- `src/db/repositories/bridge_actor_follows.py`
- `docs/development/navigation.md`

## New Files

- `src/db/repositories/remote_subscriptions.py`
- `src/db/repositories/bridge_actor_follows.py`

## Implementation Steps

1. Move the `ChannelCommunitySubscription` method bodies from `Database` into `RemoteSubscriptionRepository` without changing predicates, inserted fields, ordering, or return values.
2. Move the `BridgeActorFollow` method bodies from `Database` into `BridgeActorFollowRepository` without changing follow activity matching or accepted-state update behavior.
3. Import and instantiate the two repositories in `Database` using the shared session provider.
4. Replace the extracted `Database` methods with temporary wrappers preserving current signatures and return annotations.
5. Update navigation docs so subscription maintainers can find the new repository files.
6. Run the remote subscription focused test group and full pytest.

## Tests

Focused Stage 4 command:

```bash
./.venv/bin/pytest -q \
  tests/behavior/test_subscription_scenarios.py \
  tests/behavior/test_unsubscribe_retry_scenarios.py \
  tests/test_follow_subscription_flow.py \
  tests/test_federation_allowlist_handlers.py
```

Full suite:

```bash
./.venv/bin/pytest -q
```

## Regression / Blind-Spot Analysis

The key regression risk is reintroducing stale direct follow acceptance. Repository extraction must preserve the existing rule that remote follow acceptance requires a matching `BridgeActorFollow` row.

The second risk is changing subscription count or deletion behavior used by unsubscribe flows. Method bodies should move mechanically, including existing filters and ordering.

The third risk is session ownership drift. Both repositories must receive the `Database.session` provider and must not create their own engine or session factory.

The fourth risk is wrapper permanence. Wrappers are allowed only as staged refactor scaffolding and are removed by Stage 8.

## Open Questions

None blocking. This stage intentionally does not migrate call sites so the behavior diff stays mechanical and reviewable.
