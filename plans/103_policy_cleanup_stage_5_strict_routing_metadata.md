# 103 — Policy Cleanup Stage 5: Strict Routing Metadata

## Purpose

Implement only Stage 5 of `plans/97_policy_dependency_cleanup_umbrella.md`: replace fail-open Discord fanout routing with explicit fail-closed, per-target handling for missing, malformed, or unreadable guild metadata. Preserve all dependency ownership, policy semantics, policy-read architecture, public runtime entry points, and fanout partial-success behavior established by Stages 1–4.

## Current State and Problem

`src/community_sync/discord_fanout.py` resolves a remote subscription by target channel and currently returns allowed when the row is missing or `discord_guild_id` is absent. Repository and policy snapshot exceptions occur outside the per-target Discord exception boundary and can abort the remaining batch.

`src/local_communities/discord_fanout.py` has the same fail-open rule in `_surface_is_allowed()`, while thread/message creation loops directly skip policy evaluation whenever a selected target has `discord_guild_id=None`. Host and local-subscriber targets therefore proceed when persisted routing metadata is incomplete. Point lookups used by edit/delete can also raise before the target's Discord mutation boundary.

Production creation flows pass guild IDs from Discord command context into remote subscriptions, local communities, and local subscribers. The nullable persistence columns remain necessary for existing/legacy rows and test fixtures; Stage 5 therefore requires no schema migration. Runtime code must treat null, non-integer, boolean, zero, or negative guild IDs as malformed rather than allowed.

## Stage Boundary

| Stage 5 changes | Explicitly preserved | Left for later stages |
| --- | --- | --- |
| Missing row, missing/invalid guild ID, repository failure, and policy-read failure outcomes in Discord fanout | Constructor signatures, explicit dependency wiring, allowlist/blocklist precedence, public fanout methods, current policy-read frequency, partial-success Discord delivery | Stage 6 wrapper cleanup; Stage 7 policy-read ownership; Stage 8 snapshots/read reduction |
| Per-target logging and fail-closed isolation before Discord/mapping/dedup side effects | Database schema and nullable legacy fields; subscription/community creation flows | Data repair tooling and unrelated persistence redesign |

The stage ends fully working because every fanout entry point keeps its existing return type and best-effort target loop; only unknown target authorization changes from implicit allow to explicit skip.

## Detailed Implementation

### 1. Shared local validation within each fanout module

Add private guild-ID validation local to each module rather than introducing a cross-cutting abstraction:

```python
def _valid_discord_guild_id(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value
```

This distinguishes valid Discord snowflake-shaped integers from missing/malformed persisted values. It does not attempt Discord API existence validation.

### 2. Remote subscription fanout

Change `DiscordFanout._channel_is_allowed(channel_id)` so it is the complete target authorization boundary:

1. Load `remote_subscriptions.get_subscription_by_channel(channel_id)`.
2. If the row is missing, log a warning identifying the channel and return `False`.
3. Validate `subscription.discord_guild_id`; when absent or malformed, log a warning and return `False`.
4. Read the existing effective policy and return its current `is_discord_guild_allowed(guild_id)` result.
5. If repository lookup or policy evaluation raises, log the exception with the channel and return `False`.

Every thread create, message create, edit, and delete loop already invokes this method before Discord access. The new boundary therefore guarantees that skipped targets produce no Discord mutation and no mutation-tracker state. Existing callers that persist successful delivery rows continue to receive no result for skipped targets, so they create no mapping/delivery state.

Example:

```text
channels = [healthy_allowed, missing_row, malformed_guild, healthy_allowed]
result   = [delivery_1, delivery_4]
```

A failure for the second or third target does not prevent the fourth target.

### 3. Local-community Discord fanout

Make target policy evaluation explicit and fail closed for both selected targets and persisted mutation surfaces.

- Replace `_surface_is_allowed(forum_channel_id)` with a boundary that catches local-subscriber/local-community lookup failures, rejects missing rows and invalid guild IDs, logs the target forum, and returns `False`.
- Add a private `_target_is_allowed(LocalDiscordFanoutTarget)` used by thread/message creation loops. It validates the guild ID carried by the selected host/subscriber row, evaluates current policy, catches policy failures, logs role/forum context, and returns `False`.
- Update thread and message creation loops to call `_target_is_allowed()` instead of treating `None` as allowed.
- Keep edit/delete surface loops using `_surface_is_allowed()` so malformed or unreadable persisted routing state skips only that surface before Discord access and before mutation side effects.
- Do not add new public summary fields: a policy/malformed-target skip remains a non-attempt, matching existing denied-target behavior.

Repository failure while enumerating all local subscribers remains an action-level persistence failure because no target list can be established safely. It propagates before any target loop or Discord side effect. Point lookup failures for already-known mutation surfaces are isolated per target.

### 4. Persistence invariants

Document, but do not alter, these invariants:

- New remote subscriptions receive `operation_input.guild_id`.
- New local communities receive their Discord guild ID.
- New local subscribers receive `operation_input.guild_id`.
- Nullable fields represent legacy/incomplete data and are rejected at runtime.
- No repair migration is required for this stage; malformed rows remain visible to management/data-repair tooling but cannot bypass policy.

### 5. Tests

Add behavior-focused runtime tests in a dedicated Stage 5 test module covering observable fanout outcomes:

1. Remote thread fanout skips a missing subscription row, performs no Discord call for it, and continues to a healthy target.
2. Remote fanout skips missing and malformed guild IDs.
3. Remote repository lookup failure and policy snapshot failure are logged and isolated; later healthy targets still deliver when the failure is target-specific/transient in the fake.
4. Remote edit/delete skipped targets do not call Discord and do not record mutation tracking.
5. Local thread/message fanout skips malformed host/local-subscriber metadata without creating surface rows and continues healthy targets.
6. Local edit/delete point-lookup failure or malformed metadata skips that surface before Discord mutation while healthy surfaces continue.
7. Existing denied-guild behavior remains unchanged.
8. Local subscriber-list repository failure propagates before Discord side effects, documenting the action-level boundary.

Use runtime fakes and real temporary repositories where practical. Unit-level helper checks are allowed only for malformed scalar variants not economically observable through scenarios.

### 6. Documentation

Update:

- `docs/development/navigation.md` fanout entries to state that routing metadata/policy validation occurs before Discord mutation and malformed targets are isolated.
- `docs/architecture/database-map.md` to state that nullable guild IDs are legacy/integrity states and are not routable.
- `docs/development/test-coverage-map.md` with the new Stage 5 routing-integrity scenario coverage.

## Touched Files

- `src/community_sync/discord_fanout.py`
- `src/local_communities/discord_fanout.py`
- `tests/test_policy_routing_metadata.py` (new)
- `tests/support/db.py`
- `tests/test_phase3_message_fanout_scenarios.py`
- `tests/test_phase4_reply_preservation.py`
- `tests/test_phase8_edit_delete_sync.py`
- `tests/test_phase9_bidirectional_mirror_messages.py`
- `tests/test_user_bans_plan93.py`
- `docs/development/navigation.md`
- `docs/architecture/database-map.md`
- `docs/development/test-coverage-map.md`
- `plans/103_policy_cleanup_stage_5_strict_routing_metadata.md` (this plan)

No schema, migration, constructor, command, operation, or policy-service API files will change.

## Verification

Run:

```bash
uv run pytest tests/test_policy_routing_metadata.py -q
uv run pytest tests/behavior -q
uv run pytest tests/commands tests/operations -q
uv run pytest tests --ignore=tests/behavior --ignore=tests/commands --ignore=tests/operations -q
uv run pytest vendor/discordops/tests -q
uv run python -m compileall -q src tests

git diff --check
```

Then verify the plan point by point, inspect the staged file list, force-add this ignored `plans/` file, commit with the author identity from Stage 5 start (`denikryt <danil.mirzoev@gmail.com>`), create a full-history bundle, run `git bundle verify`, and fetch it into a new empty repository.

## Handoff

Changed contract: unknown, malformed, or unreadable target routing state is never allowed; target-addressable failures are isolated before side effects.

Preserved contracts: explicit dependencies; current policy decisions; existing public fanout APIs; current policy-read frequency; healthy-target partial success; nullable persistence schema.

Remaining work: Stage 6 may remove dead wrappers/adapters; Stage 7 decides policy-read ownership; Stage 8 remains prohibited by the orchestrator.

No temporary adapter or broken caller is handed forward. Stage 6 may rely on both remote and local Discord fanout having one fail-closed routing-integrity boundary for every target.
