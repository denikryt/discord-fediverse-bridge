# 102 — Policy Cleanup Stage 4: Remove Policy Service Locators

## Problem / Goal

`bridge_policy_service_for()` and `runtime_bridge_policy_service()` still discover or reconstruct `BridgePolicyService` from arbitrary runtime attributes. They use `getattr`, synthesize empty settings for incomplete harnesses, and rebuild a service from incidental `settings` and `database` state. Stage 3 established an explicit `bridge_policy_service` on the production `Runtime`; Stage 4 removes these locators and makes every remaining caller use that established owner directly.

This stage changes dependency access only. It preserves every policy decision, every policy-read location and frequency, command preconditions, malformed routing-metadata behavior, and all runtime action boundaries.

## Expected Behavior

- Inbound ActivityPub admission reads `runtime.bridge_policy_service` directly.
- Dashboard aggregation reads `runtime.bridge_policy_service` directly.
- `CommunityRuntime` inbound policy checks read the `bridge_policy_service` carried by the action runtime directly.
- No production function searches arbitrary objects for `bridge_policy_service`, `settings`, or `database` in order to obtain or reconstruct policy dependencies.
- `bridge_policy_service_for()` and `runtime_bridge_policy_service()` are deleted after their final callers migrate.
- Lightweight test runtimes explicitly expose the same `bridge_policy_service` field as production `Runtime`.
- Existing allowlist/blocklist outcomes and the number of `snapshot()` calls remain unchanged.

## Architecture

The production ownership path is already explicit:

```text
src/app.py
  -> BridgePolicyService(...)
  -> Runtime.bridge_policy_service
  -> ActivityPub handler / dashboard / CommunityRuntime action
```

Callers use the owner directly:

```python
snapshot = runtime.bridge_policy_service.snapshot()
```

They must not use a helper that accepts `object`, checks attributes dynamically, creates fake settings, or reconstructs a service from `runtime.database`.

`CommunityRuntime` methods continue receiving the process/action runtime object exactly as they do now. Stage 4 only replaces locator invocation with direct typed ownership access; it does not add a `BridgePolicyService` constructor parameter to `CommunityRuntime`, move policy evaluation to another layer, or create cached action state.

## Boundary Table

| Area | Stage 4 changes | Explicitly preserved | Later-stage work untouched | Stable completion reason |
|---|---|---|---|---|
| Policy helper module | Delete dynamic lookup/reconstruction helpers | `BridgePolicyService` and snapshot semantics | Dead-wrapper inventory beyond these owned locators in Stage 6 | All known callers migrate atomically |
| ActivityPub | Directly access `Runtime.bridge_policy_service` | Admission decision, outcome, logging, read count | Read ownership redesign in Stage 7 | Production `Runtime` already owns the service |
| Dashboard | Directly access runtime-owned service | Filtering and public payload | Read-frequency optimization in Stage 8 | Request runtime already carries the dependency |
| Community runtime | Directly access action runtime-owned service | Current action boundaries and per-call reads | Malformed metadata semantics in Stage 5 and ownership consolidation in Stage 7 | Existing method signatures already receive runtime |
| Tests | Add explicit service to lightweight runtime fakes | Existing scenarios and policy settings | No production fallback for incomplete fakes | Fakes model the production contract |

## Touched Files

Production:

- `src/bridge_policy.py`
- `src/activitypub_handlers.py`
- `src/dashboard.py`
- `src/community_sync/runtime.py`

Tests and shared builders:

- `tests/behavior/test_cross_stage_scenarios.py`
- `tests/behavior/test_dashboard_scenarios.py`
- `tests/behavior/test_guild_invite_publication_scenarios.py`
- `tests/behavior/test_inbound_comment_backfill.py`
- `tests/behavior/test_inbound_scenarios.py`
- `tests/behavior/test_local_community_disabled_scenarios.py`
- `tests/behavior/test_local_community_edit_delete_scenarios.py`
- `tests/behavior/test_local_community_stage5_participant_edit_delete_scenarios.py`
- `tests/behavior/test_local_community_user_ban_scenarios.py`
- `tests/behavior/test_registration_scenarios.py`
- `tests/behavior/test_subscription_scenarios.py`
- `tests/behavior/test_unsubscribed_inbound_activity_skip.py`
- `tests/support/registration.py`
- `tests/test_community_runtime_scenarios.py`
- `tests/test_end_to_end_dedup_flow.py`
- `tests/test_federation_allowlist_handlers.py`
- `tests/test_follow_subscription_flow.py`
- `tests/test_inbound_activity_outcomes.py`
- `tests/test_internal_fedify_api.py`
- `tests/test_phase5_inbound_ap_shared_groups.py`
- `tests/test_phase6_dedup_hardening.py`
- `tests/test_phase8_edit_delete_sync.py`
- `tests/test_registration_flow.py`
- `tests/test_user_bans_plan93.py`
- `tests/test_policy_service_locator_removal.py`

## New Files

- `plans/102_policy_cleanup_stage_4_remove_service_locators.md`

No new production module is required. A focused test file may be added only if existing scenario files cannot express the locator-removal contract clearly.

## Implementation Steps

1. Add failing regression coverage showing ActivityPub and dashboard runtime paths require an explicit `bridge_policy_service` and do not reconstruct one from `settings`/`database`.
2. Replace `runtime_bridge_policy_service(runtime)` in `src/activitypub_handlers.py` with direct `runtime.bridge_policy_service` access.
3. Replace the dashboard locator call with direct runtime-owned service access.
4. Replace both `CommunityRuntime` locator calls with direct access to the runtime object passed into the existing action.
5. Remove imports of locator helpers from every migrated caller.
6. Delete `runtime_bridge_policy_service()` and `bridge_policy_service_for()` from `src/bridge_policy.py` after repository-wide search proves no callers remain.
7. Update lightweight runtime builders and direct fakes to construct and expose a real `BridgePolicyService` using their existing test settings and policy repository.
8. Preserve settings-sensitive tests by passing the same settings object to the test service; do not replace policy-bearing settings with unrestricted defaults.
9. Run repository-wide searches proving no locator name, dynamic policy-service discovery, or service reconstruction remains in production.
10. Verify snapshot read placement and frequency are unchanged at each migrated call site.
11. Run focused behavior regressions, all project tests, vendored DiscordOps tests, compile checks, and diff checks.

## Tests

Behavior-first coverage must retain observable outcomes for:

- blocked and non-allowlisted inbound ActivityPub activities;
- allowed inbound ActivityPub dispatch;
- dashboard filtering and federation metadata under bootstrap and dynamic policy;
- inbound post/comment handling paths in `CommunityRuntime` that currently perform policy checks.

Focused contract coverage must prove:

- a lightweight runtime with `settings` and `database` but no `bridge_policy_service` is rejected at direct attribute access rather than silently reconstructed;
- a runtime with an explicit service uses that service and preserves the expected decision;
- `src.bridge_policy` no longer exposes either locator helper.

Required validation:

- all `tests/behavior` tests;
- all command and operation tests;
- all remaining project tests;
- vendored DiscordOps tests;
- `python -m compileall -q src tests`;
- `git diff --check`.

## Documentation

The compatibility catalog was inspected because it owns temporary compatibility logic. It does not currently document these policy locators, so no catalog entry must be removed or edited. No external API, deployment, database, command, or federation payload contract changes; the detailed stage plan is the relevant architecture record.

## Stage Handoff

Contracts changed:

- remaining policy consumers use the explicit `Runtime.bridge_policy_service` ownership path;
- locator and reconstruction helpers no longer exist.

Contracts intentionally preserved:

- all policy decisions and snapshot semantics;
- all current policy-read locations and frequency;
- ActivityPub, dashboard, and community runtime public entry points;
- malformed routing metadata behavior;
- command authorization architecture.

Remaining known problems:

- Stage 5 defines strict missing/malformed routing metadata outcomes;
- Stage 6 inventories unrelated dead wrappers and placeholders;
- Stage 7 assigns final policy-read ownership across action boundaries;
- Stage 8 alone may optimize repeated reads.

No temporary adapter or incomplete caller is handed forward. Stage 5 may rely on missing policy dependencies being impossible in production and all policy consumers having a traceable path to the composition-root service.

## Open Questions

None. Stage 3 and the current `Runtime` dataclass already establish the only valid owner and dependency path.
