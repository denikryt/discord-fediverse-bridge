# 100 — Policy Cleanup Stage 2: Explicit Discord Mutation Tracking

## Problem / Goal

`DiscordFanout.propagate_edit()` and `DiscordFanout.propagate_delete()` currently discover required deduplication behavior with `getattr()` and `callable()`. If the supplied bot lacks `track_message_edit()` or `track_message_delete()`, bridge-originated Discord mutations are silently left untracked, allowing raw Discord events to re-enter edit/delete fanout and create echo loops.

The goal of Stage 2 is to replace that reflective capability probe with one explicit, narrow required interface while preserving all current policy-service ownership, policy-read frequency, malformed-target behavior, fanout isolation, and public runtime action entry points.

## Expected Behavior

- `DiscordFanout` cannot be constructed without an explicit mutation-tracking dependency.
- A successful mirror edit calls `track_message_edit(message_id)` exactly once.
- A successful mirror delete calls `track_message_delete(message_id)` exactly once.
- Failed or skipped Discord mutations are not recorded.
- Missing required tracking fails at construction/call wiring rather than being silently ignored.
- Production uses the existing `BridgeBot` implementation as both Discord API client and mutation tracker.
- Test fakes model the same explicit contract.
- Existing partial-failure behavior remains unchanged: one mirror failure does not block healthy targets.

## Architecture

Introduce a local `DiscordMutationTracker` protocol in `src/community_sync/discord_fanout.py` with two synchronous methods:

```python
class DiscordMutationTracker(Protocol):
    def track_message_edit(self, message_id: int) -> None: ...
    def track_message_delete(self, message_id: int) -> None: ...
```

`DiscordFanout.__init__()` receives this dependency separately from `bot`:

```python
DiscordFanout(
    bot=bot,
    mutation_tracker=bot,
    database=database,
    policy_service=bridge_policy_service,
)
```

The separation is intentional. `bot` remains responsible for Discord API access; `mutation_tracker` represents the narrower required deduplication capability. `BridgeBot` already implements both methods, so no new production implementation is needed.

Inside `propagate_edit()` and `propagate_delete()`, tracking occurs only after the awaited Discord mutation succeeds. The direct protocol call replaces the reflective branch.

## Boundary Table

| Area | Stage 2 changes | Explicitly preserved | Later-stage work left untouched | Stable completion reason |
|---|---|---|---|---|
| Mutation tracking | Replace `getattr`/`callable` probes with required protocol dependency | Edit/delete fanout semantics and partial failure | Constructor policy dependencies in Stage 3 | All constructors and fakes migrate atomically |
| Production composition | Pass `BridgeBot` as tracker | Existing object ownership and circular wiring | Service-locator removal in Stage 4 | Existing implementation already satisfies protocol |
| Tests | Require tracker in all `DiscordFanout` harnesses and assert successful tracking | Existing scenario outcomes | Malformed routing metadata in Stage 5 | Test fakes match production contract |
| Documentation | Record removal of compatibility probe | Catalog responsibility and format | Later policy-read architecture | Documentation describes only this compatibility cleanup |

## Touched Files

- `src/community_sync/discord_fanout.py`
- `src/app.py`
- `tests/test_phase2_fanout_scenarios.py`
- `tests/test_phase3_message_fanout_scenarios.py`
- `tests/test_phase4_reply_preservation.py`
- `tests/test_phase8_edit_delete_sync.py`
- `tests/test_phase9_bidirectional_mirror_messages.py`
- `tests/test_user_bans_plan93.py`
- `docs/discord_lemmy_bridge_compatibility_catalog.md`

## New Files

- `plans/100_policy_cleanup_stage_2_explicit_mutation_tracking.md`

No new production module is required because the protocol belongs beside its sole consumer and `BridgeBot` already provides the implementation.

## Implementation Steps

1. Add `DiscordMutationTracker` to `src/community_sync/discord_fanout.py` with documented edit and delete methods.
2. Make `mutation_tracker` a required keyword-only constructor argument on `DiscordFanout` and store it separately from `bot`.
3. Replace reflective edit tracking with a direct call after `message.edit()` succeeds.
4. Replace reflective delete tracking with a direct call after `message.delete()` succeeds.
5. Update `src/app.py` so production composition passes `mutation_tracker=bot`.
6. Update every direct `DiscordFanout` test constructor to supply a fake implementing the protocol.
7. Add scenario assertions that successful edit/delete fanout records the corresponding mutation.
8. Add a regression test proving construction without `mutation_tracker` raises `TypeError`.
9. Update the compatibility catalog to document that the reflective capability probe was removed in Stage 2.
10. Run focused tests, the full project test suite, compile checks, and repository diff checks.

## Tests

Behavior-priority coverage:

- Existing edit propagation scenario: after the mirror message is successfully edited, the tracker records that mirror message ID once.
- Existing delete propagation scenario: after the mirror message is successfully deleted, the tracker records that mirror message ID once.
- Existing partial-failure tests remain green, proving the explicit dependency does not change healthy-target isolation.

Contract regression coverage:

- Constructing `DiscordFanout(bot=...)` without `mutation_tracker` raises `TypeError`, proving missing required behavior is no longer silently accepted.

Required validation:

- Focused Stage 2 tests.
- All repository tests, split only if command runtime limits require it.
- `python -m compileall src tests`.
- `git diff --check`.

## Documentation

The compatibility catalog is the relevant document because its stated purpose is to inventory temporary compatibility probes and cleanup debt. No deployment, ActivityPub, schema, or command documentation changes because observable product behavior and external interfaces remain unchanged.

## Stage Handoff

Contracts changed:

- `DiscordFanout` now requires an explicit `DiscordMutationTracker`.
- Successful mirror edit/delete operations always invoke the corresponding tracker method.

Contracts intentionally preserved:

- Policy dependency optionality and ownership.
- Policy decisions and read frequency.
- Missing/malformed routing metadata behavior.
- Fanout partial-failure behavior.
- Public runtime action methods.

Remaining later-stage problems:

- Stage 3: make required policy dependencies mandatory.
- Stage 4: remove policy service locators.
- Stage 5: define malformed routing metadata outcomes.
- Stage 6: remove dead adapters and placeholders.
- Stage 7: consolidate policy-read ownership.

No temporary adapter or broken caller will be handed forward. Stage 3 may rely on all `DiscordFanout` construction paths already satisfying the explicit mutation-tracking contract.

## Open Questions

None. The current code establishes that mutation tracking is required for raw-event deduplication and that `BridgeBot` is the production implementation.
