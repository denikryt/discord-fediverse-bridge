# 99 — Policy Cleanup Stage 1: Remove Isolated Legacy Internal APIs

## Problem / Goal

Two policy-related helper surfaces still accept obsolete in-repository call forms:

1. `src.lemmyverse_communities.autocomplete_lemmyverse_communities()` accepts both the current `policy_snapshot` input and a legacy bootstrap-only `allowlist` input. The legacy branch fabricates a snapshot and cannot represent dynamic blocklist precedence.
2. `src.local_community_permissions` accepts both the current `policy_snapshot` input and legacy `settings` input through `_resolve_snapshot()`. That fallback fabricates an empty repository and therefore cannot represent dynamic super-admin policy.

All production callers already pass `BridgePolicySnapshot`. Only focused tests still use the legacy forms. This stage will make the current snapshot input mandatory, migrate those tests atomically, remove the compatibility branches and update the compatibility catalog. It will not change dependency ownership, constructor wiring, runtime failure semantics, policy-read ownership, or read frequency.

## Expected Behavior

- Lemmyverse autocomplete always receives one explicit `BridgePolicySnapshot`.
- Local-community permission helpers always receive one explicit `BridgePolicySnapshot`.
- Dynamic blocklist precedence and dynamic super-admin entries remain represented exactly as they are today.
- Calling either API with the removed `allowlist=` or `settings=` keyword raises Python's normal `TypeError`, proving there is one supported internal call form.
- Existing observable permission and autocomplete behavior remains unchanged after test callers are migrated.

Examples:

```python
snapshot = BridgePolicySnapshot(
    (
        EffectivePolicyEntry(
            PolicyType.FEDERATION_ALLOW,
            "lemmy.world",
            "bootstrap",
        ),
    )
)
choices = await autocomplete_lemmyverse_communities(
    cache,
    current="python",
    policy_snapshot=snapshot,
)
```

```python
allowed = can_manage_local_community(
    policy_snapshot=policy_service.snapshot(),
    discord_user_id="999",
    local_community=community,
)
```

Removed forms:

```python
autocomplete_lemmyverse_communities(cache, current="python", allowlist=[])
can_manage_local_community(settings=settings, discord_user_id="999", local_community=community)
```

## Architecture

The immutable `BridgePolicySnapshot` remains the sole policy value accepted by these pure helper functions. The functions do not acquire services, repositories, settings, or cached action state themselves.

`autocomplete_lemmyverse_communities()` continues to:

1. obtain cache entries through the existing non-blocking cache API;
2. evaluate every entry with `policy_snapshot.federation_decision()`;
3. rank and cap the allowed entries.

`local_community_permissions` continues to:

1. evaluate effective super-admin status from the supplied snapshot;
2. combine that result with persisted ownership, guild, and status checks.

No runtime call graph changes are required because current production callers already supply snapshots.

## Stage Boundary

| Area | Stage 1 decision |
|---|---|
| Changes | Remove obsolete `allowlist` and `settings` call forms; migrate direct test callers; remove compatibility implementation and comments. |
| Preserves | Current policy semantics, production callers, service ownership, snapshot creation sites, and number/timing of policy reads. |
| Leaves for later stages | Reflective capability probes, optional constructor dependencies, service locators, malformed metadata behavior, dead wrappers, read ownership, and read-frequency optimization. |
| Stable exit | Every affected helper has one mandatory snapshot contract and all callers compile and pass without an adapter. |

## Touched Files

- `src/lemmyverse_communities.py`
- `src/local_community_permissions.py`
- `tests/test_lemmyverse_communities.py`
- `tests/test_local_community_permissions.py`
- `docs/discord_lemmy_bridge_compatibility_catalog.md`

## New Files

- `plans/99_policy_cleanup_stage_1_internal_api.md`

No new production module is required because the canonical snapshot type already exists.

## Implementation Steps

1. Add regression coverage that calls Lemmyverse autocomplete with `allowlist=` and verifies the obsolete keyword is rejected.
2. Add regression coverage that calls local-community permission helpers with `settings=` and verifies the obsolete keyword is rejected.
3. Add a small test helper that constructs `BridgePolicySnapshot` values from federation allow entries, and migrate all Lemmyverse autocomplete tests from `allowlist=` to `policy_snapshot=`.
4. Add a small test helper that constructs snapshots from super-admin entries, and migrate all local-community permission tests from `settings=` to `policy_snapshot=`.
5. Change `autocomplete_lemmyverse_communities()` so `policy_snapshot` is mandatory; remove `allowlist`, the fallback snapshot construction, and imports used only by that branch.
6. Change `is_super_admin()`, `can_manage_local_community()`, and `can_access_local_community_from_guild()` so `policy_snapshot` is mandatory; delete `_resolve_snapshot()`, the `settings` arguments, the fabricated repository, and the now-unused service import.
7. Keep all production call sites unchanged because they already pass snapshots.
8. Update the compatibility catalog with a focused record that both snapshot compatibility shims were removed, while unrelated compatibility remains untouched.
9. Run the focused tests first, then the complete repository checks required by the project.

## Tests

Focused tests:

- `tests/test_lemmyverse_communities.py`
  - existing ranking, filtering, cache, retry, and non-blocking scenarios using explicit snapshots;
  - regression that `allowlist=` is no longer accepted.
- `tests/test_local_community_permissions.py`
  - owner, super-admin, non-owner, null-owner, and exact-ID behavior using explicit snapshots;
  - regression that `settings=` is no longer accepted.

Repository validation:

- run the complete Python test suite;
- run configured formatting, lint, type, compile, and repository-specific checks discovered from project configuration;
- confirm the working tree contains only Stage 1 scope before committing.

## Documentation

The compatibility catalog owns temporary in-repository compatibility paths, so it will record removal of these two shims. Other documents do not describe these function signatures and need no update.

## Stage Handoff

Contracts changed:

- Lemmyverse autocomplete and local-community permission helpers now require `BridgePolicySnapshot` and expose no bootstrap-only compatibility parameters.

Contracts preserved:

- snapshot semantics, allowlist/blocklist precedence, super-admin decisions, cache behavior, permission outcomes, production dependency wiring, and policy-read frequency.

Remaining later-stage work:

- Stages 2–7 remain exactly as assigned by plan 97.

No temporary adapter or broken caller is handed forward. Stage 2 may rely on all policy helper call sites using the canonical snapshot contracts.

## Open Questions

None. The current production call graph and umbrella plan already select `BridgePolicySnapshot` as the canonical contract.
