# 105 — Policy Cleanup Stage 7: Policy-Read Ownership

## Purpose

Implement only Stage 7 of the umbrella plan: make policy-read ownership explicit across command, Discord event, ActivityPub event, dashboard, and fanout flows without changing policy semantics or the number of effective-policy reads. Stage 8 optimization is explicitly excluded.

## Current Call Graph

### Commands and DiscordOps operations

Slash-command adapters begin command actions. DiscordOps input objects own memoized snapshots when multiple preconditions/body steps need one policy view. Autocomplete and presentation-only command callbacks may read policy directly because they are their own action boundary.

### Discord events

`DiscordEventRouter` owns the top-level Discord guild admission decision. After routing, domain services own narrower independent decisions: `ContentPublishService` owns outbound federation destination checks, while Discord fanout services own per-target guild checks. These are separate current reads and must remain separate.

### ActivityPub events

`activitypub_handlers.dispatch_activitypub_event()` owns inbound origin admission. The selected runtime owns later action-specific checks, and federation/Discord fanout components own their target checks. No snapshot is propagated from dispatch into nested services.

### Dashboard

`dashboard.build_dashboard_data()` owns one dashboard-request snapshot and reuses it across rows. This existing one-read boundary remains unchanged.

### Fanout

`DiscordFanout`, `LocalCommunityDiscordFanout`, and `LocalCommunityFederationFanout` own target-level policy evaluation because they possess the persisted routing target and enforce Stage 5 side-effect isolation. Their current read frequency remains unchanged.

## Chosen Model

`BridgePolicyService` becomes the sole technical effective-policy reader. It exposes narrow evaluator methods that each create one snapshot and answer one question:

```python
service.is_discord_guild_allowed(guild_id)
service.federation_decision(url_or_host)
service.is_super_admin(discord_user_id)
service.list_effective_entries(policy_type)
```

Calling one evaluator performs exactly the same single `snapshot()` read as the replaced `service.snapshot().method(...)` expression. Existing action owners that intentionally reuse one snapshot—DiscordOps input memoization, dashboard rendering, multi-target federation fanout, and other multi-use local scopes—continue to call `snapshot()` directly. This preserves current read counts and does not introduce an action context or snapshot propagation.

Lower runtime components no longer choose the snapshot representation merely to ask one narrow question. They invoke the narrow evaluator through their already-explicit service dependency.

## Boundary Table

| Changes | Preserves | Excluded |
| --- | --- | --- |
| Add narrow evaluator API; migrate single-question runtime reads; document action owners | All allow/block precedence, Stage 5 malformed-target outcomes, constructor ownership, public action methods, snapshot memoization, current read counts | Snapshot propagation, one-read-per-action/batch optimization, cached action state, command authorization redesign |

## Implementation

### Bridge policy service

Add the four evaluator methods above, each implemented as one `snapshot()` call followed by the existing immutable snapshot method. Keep `snapshot()` public for explicit multi-use action owners.

### Single-question runtime consumers

Replace direct single-use snapshot chains in:

- `src/discord_event_router.py`
- `src/activitypub_handlers.py`
- `src/content_publish_service.py`
- `src/community_sync/runtime.py`
- `src/community_sync/discord_fanout.py`
- `src/local_communities/runtime.py`
- `src/local_communities/discord_fanout.py`

Do not replace local variables that intentionally reuse a snapshot across multiple decisions/targets.

`LocalCommunityFederationFanout` keeps its one snapshot per target batch because replacing it with one service call per target would change read frequency. Dashboard and DiscordOps inputs likewise retain their current snapshots.

### Commands

Command and autocomplete code remains an action owner. Single-question autocomplete calls may use the narrow service API, but no command input snapshot memoization is removed. DiscordOps preconditions continue to consume operation-input snapshots.

### Documentation

Add a policy-read ownership section to architecture/navigation documentation mapping the five flow types and explicitly reserving read-frequency optimization for Stage 8.

## Tests

Add service tests proving each narrow evaluator performs exactly one repository read and returns the same result as snapshot evaluation. Add structural/runtime tests proving selected lower components call the narrow evaluator rather than `snapshot()` for single-question checks. Retain existing tests that verify dashboard and operation inputs reuse one snapshot.

Run all repository suites, compileall, and diff checks.

## Touched Files

- `src/bridge_policy.py`
- `src/discord_event_router.py`
- `src/activitypub_handlers.py`
- `src/content_publish_service.py`
- `src/community_sync/runtime.py`
- `src/community_sync/discord_fanout.py`
- `src/local_communities/runtime.py`
- `src/local_communities/discord_fanout.py`
- `src/commands/subscribe.py`
- `src/commands/subscribe_community_handler.py`
- `src/operations/subscribe.py`
- `src/operations/unsubscribe.py`
- `tests/test_bridge_policy_core.py`
- `tests/test_policy_read_ownership.py`
- `tests/test_policy_routing_metadata.py`
- `docs/architecture/bridge-policy.md`
- `docs/development/navigation.md`
- `plans/105_policy_cleanup_stage_7_policy_read_ownership.md`

## Verification and Handoff

Verify every direct `snapshot()` call is either an explicit multi-use action owner or a memoized operation input. Confirm no snapshot/action context is threaded through public runtime APIs and no read count changes.

Changed contract: narrow policy questions go through `BridgePolicyService`; direct snapshots are reserved for explicit multi-use action owners.

Preserved contract: all policy semantics, failure behavior, dependencies, action entry points, and read frequency.

After this stage, stop. Stage 8 must not be started.
