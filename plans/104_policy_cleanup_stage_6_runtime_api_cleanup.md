# 104 — Policy Cleanup Stage 6: Runtime API Cleanup

## Purpose

Implement only Stage 6 of the policy cleanup umbrella: remove demonstrably obsolete compatibility behavior and private adapters while preserving real framework, persistence, HTTP, and runtime contracts.

## Inventory and Decisions

The production tree contains no accidental `pass`, ellipsis, or `perform()` operation methods. All class-based DiscordOps operations implement the required `reject()` and `body()` abstract methods directly. Function-based `OperationDefinition` reject/body callables are framework contracts and remain.

Most legacy references are real contracts and are not cleanup targets: database migrations, legacy Lemmy mappings, HTTP dashboard redirects, ActivityPub compatibility renderers, and nullable legacy persistence handling.

Two obsolete/test-shaped APIs are confirmed cleanup targets:

1. `src/commands/ban_user.py` swaps `user` and `community` at runtime when positional test calls use the old order. Discord supplies named options and the current callback signature is `(interaction, user, community=None, reason=None)`. Only project tests use the obsolete positional order.
2. `src/discord_oauth_client.py::_redirect_uri()` probes an older settings shape and falls back to `discord_oauth_redirect_uri`. Production `Settings` has the required `resolved_discord_oauth_redirect_uri` property. The helper exists only to tolerate incomplete test adapters and is called from two methods.

`CommunityActorBanRepository.create_active_ban()` is test-only, but it remains a useful self-transactional repository method rather than a pass-through runtime adapter: migrating every seed caller to a caller-owned SQLAlchemy transaction would make tests depend on a lower-level transaction API. It is intentionally preserved.

## Boundary

| Changes | Preserves | Later work untouched |
| --- | --- | --- |
| Remove ban callback positional argument swap; remove OAuth settings fallback/private wrapper; remove stale compatibility comments | Discord slash option names, operation semantics, OAuth redirect resolution, DiscordOps method contracts, repository methods, public runtime entry points | Stage 7 policy-read ownership; Stage 8 read optimization |

No dependency ownership, policy evaluation, malformed metadata behavior, persistence schema, or read frequency changes are allowed.

## Implementation

### Ban command

Delete the runtime heuristic:

```python
if community and "@" in community and "@" not in user:
    user, community = community, user
```

Update direct callback tests to call the current contract using keyword arguments or the correct positional order. Add a regression test proving the callback does not reinterpret values based on `@` characters.

### Discord OAuth client

Delete `_redirect_uri()`. Use `self.settings.resolved_discord_oauth_redirect_uri` directly in authorization URL construction and token exchange. Update boundary-test settings fakes to expose the production property. Add a test that an incomplete old settings adapter fails immediately rather than silently selecting a fallback.

### Stale comments

Replace the `BanUserOperation.body()` comment that claims to preserve a legacy remote-target call shape. The conditional `target_discord_user_id` field is current domain behavior: local targets carry immutable Discord identity; remote targets do not.

### Framework verification

Add/retain a structural test that every concrete class-based operation in the policy-management set implements `body` and `reject`, and no production operation defines `perform`. Existing vendored DiscordOps tests remain authoritative for framework lifecycle behavior.

## Touched Files

- `src/commands/ban_user.py`
- `src/operations/ban_user.py`
- `src/discord_oauth_client.py`
- `tests/commands/test_ban_user_command.py`
- `tests/test_discord_oauth_client.py`
- `tests/operations/test_policy_operation_contracts.py` or the existing equivalent contract test
- `docs/development/navigation.md`
- `plans/104_policy_cleanup_stage_6_runtime_api_cleanup.md`

## Tests and Checks

Run focused command, OAuth, and operation-contract tests, then all behavior, command/operation, remaining project, and vendored DiscordOps suites. Run compileall and `git diff --check`.

## Handoff

Changed: ban callback has one current argument contract; OAuth client requires the production settings contract; stale compatibility language is removed.

Preserved: all real framework methods and runtime entry points, repository methods, policy semantics, dependency ownership, failure semantics, and read frequency.

No temporary adapter is handed forward. Stage 7 may rely on a cleaner runtime call graph without these test-shaped compatibility branches.
