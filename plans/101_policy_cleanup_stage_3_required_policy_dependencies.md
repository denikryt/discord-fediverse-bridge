# 101 — Policy Cleanup Stage 3: Required Policy Dependencies

## Problem / Goal

Policy-sensitive production components currently accept `BridgePolicyService | None` and either construct an unrestricted replacement from incidental `settings`/`database` state or silently skip enforcement. This permits incomplete runtime composition and lets test harnesses determine production fallback behavior.

Stage 3 makes every policy dependency that is required by current product behavior explicit at construction or registration time. It updates the application composition root, all direct production callers, command adapters, DiscordOps inputs, shared test builders, and direct test fakes in one atomic change. It does not remove service locators, change policy ownership, alter policy decisions, define malformed routing metadata behavior, or change read frequency.

## Expected Behavior

- `DiscordEventRouter`, `ContentPublishService`, `LocalCommunityRuntime`, and `DiscordFanout` require a policy service when constructed.
- Policy-sensitive command registration and autocomplete factories require the shared service supplied by `BridgeBot` composition.
- Policy-sensitive DiscordOps input objects require the same service; they no longer construct replacements or skip checks when it is absent.
- `CommandAccessInput` requires a policy service and memoizes snapshots from that service only.
- Production startup wires one long-lived `BridgePolicyService` through every affected dependency.
- Test builders and fakes explicitly construct and pass a real policy service backed by their test database and settings.
- Existing allowlist/blocklist precedence, command precondition ordering, target routing, missing metadata behavior, and policy read frequency remain unchanged.

## Architecture

The application composition root in `src/app.py` remains the owner of the long-lived service:

```python
bridge_policy_service = BridgePolicyService(
    settings=settings,
    repository=database.bridge_policy_entries,
)
```

That instance is passed directly into all policy-sensitive runtimes and adapters. No constructor creates a synthetic settings object or replacement service.

Required constructor examples:

```python
DiscordEventRouter(..., bridge_policy_service=bridge_policy_service)
ContentPublishService(..., bridge_policy_service=bridge_policy_service)
LocalCommunityRuntime(..., bridge_policy_service=bridge_policy_service)
DiscordFanout(..., policy_service=bridge_policy_service)
```

Required operation input example:

```python
BanUserInput(..., policy_service=bridge_policy_service)
```

`get_policy_snapshot()` methods continue memoizing one snapshot per operation exactly as before, but use only the required field. This stage does not move reads to another layer or consolidate reads across actions.

## Boundary Table

| Area | Stage 3 changes | Explicitly preserved | Later-stage work left untouched | Stable completion reason |
|---|---|---|---|---|
| Runtime constructors | Required policy parameters; remove unrestricted constructor fallbacks | Existing policy checks and read locations | Locator deletion in Stage 4 | All production and test construction paths migrate together |
| Commands | Required service in policy-sensitive register/autocomplete/modal inputs | DiscordOps preconditions and user-visible outcomes | Policy-read ownership in Stage 7 | `BridgeBot.setup_hook()` already owns the shared service |
| Operations | Required service fields; remove input-level replacement construction and conditional skip paths | Preconditions, snapshot memoization, audit semantics | Read-frequency optimization in Stage 8 | Every command caller and test supplies the dependency |
| Tests | Builders/fakes model production composition | Scenario behavior and database state | Malformed target semantics in Stage 5 | Test databases can construct the same real service |

## Touched Files

Production files:

- `src/commands/ban_user.py`
- `src/commands/create_community.py`
- `src/commands/edit_community.py`
- `src/commands/guild_guard.py`
- `src/commands/list_banned_users.py`
- `src/commands/list_subs.py`
- `src/commands/publish_guild_invite.py`
- `src/commands/register.py`
- `src/commands/remove_guild_invite.py`
- `src/commands/subscribe.py`
- `src/commands/unban_user.py`
- `src/commands/unsubscribe.py`
- `src/community_sync/discord_fanout.py`
- `src/content_publish_service.py`
- `src/discord_bot.py`
- `src/discord_event_router.py`
- `src/local_communities/runtime.py`
- `src/operations/ban_user.py`
- `src/operations/common_preconditions.py`
- `src/operations/edit_community.py`
- `src/operations/list_banned_users.py`
- `src/operations/subscribe.py`
- `src/operations/unban_user.py`
- `src/operations/unsubscribe.py`

Test and shared-builder files:

- `tests/behavior/test_bridge_policy_management_scenarios.py`
- `tests/behavior/test_cross_stage_scenarios.py`
- `tests/behavior/test_discord_directory_snapshot_scenarios.py`
- `tests/behavior/test_inbound_comment_backfill.py`
- `tests/behavior/test_inbound_scenarios.py`
- `tests/behavior/test_local_community_disabled_scenarios.py`
- `tests/behavior/test_local_community_edit_delete_scenarios.py`
- `tests/behavior/test_local_community_edit_metadata_scenarios.py`
- `tests/behavior/test_local_community_inbound_scenarios.py`
- `tests/behavior/test_local_community_publish_scenarios.py`
- `tests/behavior/test_local_community_remote_fanout_scenarios.py`
- `tests/behavior/test_local_community_stage3_local_subscriber_sync_scenarios.py`
- `tests/behavior/test_local_community_stage4_local_subscriber_origin_scenarios.py`
- `tests/behavior/test_local_community_stage5_participant_edit_delete_scenarios.py`
- `tests/behavior/test_local_community_surface_stage2_scenarios.py`
- `tests/behavior/test_local_community_user_ban_scenarios.py`
- `tests/behavior/test_local_subscriber_stage1_scenarios.py`
- `tests/behavior/test_publish_scenarios.py`
- `tests/behavior/test_registration_scenarios.py`
- `tests/behavior/test_subscription_scenarios.py`
- `tests/behavior/test_unified_community_discovery_scenarios.py`
- `tests/behavior/test_unsubscribe_retry_scenarios.py`
- `tests/commands/test_ban_user_command.py`
- `tests/commands/test_create_community_command.py`
- `tests/commands/test_edit_community_command.py`
- `tests/commands/test_guild_guard.py`
- `tests/commands/test_list_banned_users_command.py`
- `tests/commands/test_list_subscriptions_command.py`
- `tests/commands/test_publish_guild_invite_command.py`
- `tests/commands/test_register_command.py`
- `tests/commands/test_remove_guild_invite_command.py`
- `tests/commands/test_subscribe_command.py`
- `tests/commands/test_unban_user_command.py`
- `tests/commands/test_unsubscribe_command.py`
- `tests/operations/test_ban_user_operation.py`
- `tests/operations/test_edit_community_operation.py`
- `tests/operations/test_list_banned_users_operation.py`
- `tests/operations/test_management_audit_events.py`
- `tests/operations/test_subscribe_operation.py`
- `tests/operations/test_unban_user_operation.py`
- `tests/operations/test_unsubscribe_operation.py`
- `tests/support/runtime.py`
- `tests/test_discord_publish_flow.py`
- `tests/test_end_to_end_dedup_flow.py`
- `tests/test_phase2_fanout_scenarios.py`
- `tests/test_phase3_message_fanout_scenarios.py`
- `tests/test_phase4_reply_preservation.py`
- `tests/test_phase5_inbound_ap_shared_groups.py`
- `tests/test_phase6_dedup_hardening.py`
- `tests/test_phase8_edit_delete_sync.py`
- `tests/test_phase9_bidirectional_mirror_messages.py`
- `tests/test_user_bans_plan93.py`
- `tests/test_required_policy_dependencies.py`

`src/app.py` was inspected and remains unchanged because it already constructs the shared `BridgePolicyService`; `src/discord_bot.py` is the affected composition wiring point for these consumers.

## New Files

- `plans/101_policy_cleanup_stage_3_required_policy_dependencies.md`

No new production module is required.

## Implementation Steps

1. Add failing constructor/input regression tests proving omitted required policy dependencies are rejected.
2. Make runtime constructor parameters mandatory and remove synthetic settings/replacement service branches.
3. Replace conditional policy enforcement in `ContentPublishService`, `DiscordFanout`, subscription, and unsubscribe paths with direct service use while preserving decisions and call frequency.
4. Make `CommandAccessInput.policy_service` required and remove its database-backed fallback construction.
5. Make policy-sensitive operation input fields required and simplify snapshot methods to use them directly.
6. Make affected command register/autocomplete/modal factory parameters required and remove local `BridgePolicyService(...)` construction.
7. Verify `BridgeBot.setup_hook()` and `src/app.py` pass the shared service to every production consumer.
8. Update shared test builders first, then direct test constructors and command/operation harnesses.
9. Run repository-wide searches to prove no affected optional type or fallback remains.
10. Run focused regressions, all tests, compile checks, and diff checks.

## Tests

Behavior tests remain primary and must prove current outcomes are unchanged for:

- Discord event routing under allowed and denied guild policy.
- Discord-to-ActivityPub publication under allowed and denied federation policy.
- Local-community runtime and fanout policy filtering.
- command access and autocomplete under dynamic policy.
- subscribe/unsubscribe policy decisions.
- ban, unban, edit-community, and list-banned-users preconditions.

Contract regressions assert that affected constructors, command factories, and operation inputs cannot omit the required service.

Required validation:

- all `tests/behavior` tests;
- all command and operation tests;
- all remaining project tests;
- vendored DiscordOps tests;
- `python -m compileall -q src tests`;
- `git diff --check`.

## Documentation

No external behavior, deployment contract, database schema, ActivityPub contract, or command semantics change. The stage is internal dependency wiring, already governed by this detailed plan and umbrella architecture; no existing user-facing documentation has responsibility for constructor signatures. The compatibility catalog is not changed because service-locator and adapter cleanup remains Stage 4/6.

## Stage Handoff

Contracts changed:

- all identified policy-sensitive constructors, command factories, and operation inputs require an explicit `BridgePolicyService`;
- no affected constructor/input invents an unrestricted replacement or silently skips a policy check.

Contracts intentionally preserved:

- service ownership remains at current components;
- all policy read locations and frequency remain unchanged;
- locator helpers remain available and unchanged for Stage 4;
- malformed routing metadata behavior remains unchanged for Stage 5;
- command authorization remains in DiscordOps preconditions.

Remaining known problems:

- Stage 4 removes locator helpers and dynamic discovery;
- Stage 5 defines missing/malformed routing metadata behavior;
- Stage 6 removes obsolete wrappers/placeholders;
- Stage 7 consolidates policy-read ownership.

No temporary compatibility branch or broken caller is handed forward. Stage 4 may rely on every affected production component already owning its required service explicitly.

## Open Questions

None. Current production composition already creates one shared service, and every affected path uses policy for correctness.
