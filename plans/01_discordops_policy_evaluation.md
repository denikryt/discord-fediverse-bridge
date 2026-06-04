# DiscordOps Policy Evaluation and Shared Command Access

## Problem / Goal

The project currently has two mechanisms for deciding whether command work may proceed:

- `vendor/discordops` evaluates ordered `Precondition` objects for domain/application operations.
- `src/commands/guild_guard.py` imperatively evaluates guild context, guild allowlist, and registration checks, and also sends Discord responses.

This duplicates the policy model and mixes policy evaluation with Discord presentation. The immediate duplication is visible in `/create_community`, where guild access and registration are checked by command helpers, while subscription operations already express registration as a DiscordOps precondition.

The goal is to make DiscordOps preconditions the single reusable condition mechanism without forcing command-access checks into fake operations with empty bodies and without making the vendor package aware of this bridge's settings, database, Discord SDK objects, or audit rules.

The implementation must:

- add a body-less policy evaluation API to DiscordOps;
- make operation execution and policy evaluation use one shared precondition evaluator;
- define guild context, guild allowlist, and registered-user access as reusable project-level preconditions/policies;
- keep Discord response behavior in the command adapter;
- use the same registered-user `Precondition` object in command access and subscription operations;
- preserve the existing user-visible messages, short-circuit order, allowlist semantics, audit semantics, and command side-effect ordering.

## Expected Behavior

### Framework behavior

`PolicyDefinition` declares an ordered tuple of existing `Precondition` objects but has no `reject` or `body` callback.

```python
REGISTERED_GUILD_COMMAND_ACCESS = PolicyDefinition(
    name="registered_guild_command_access",
    preconditions=(
        GUILD_CONTEXT_REQUIRED,
        GUILD_ALLOWLISTED,
        DISCORD_USER_REGISTERED,
    ),
)
```

`evaluate_policy()` and `evaluate_policy_async()` return a `PolicyResult`:

```python
PolicyResult(
    allowed=False,
    reason="not_allowlisted",
    message="This Discord server is not allowed to use this bridge bot.",
    extra_kwargs=None,
)
```

On success:

```python
PolicyResult(
    allowed=True,
    reason=None,
    message=None,
    extra_kwargs=None,
)
```

Evaluation remains ordered and stops on the first failed precondition. Sync evaluation rejects async predicates or async `reject_kwargs_factory` callbacks with the same fail-fast behavior as the existing sync operation runner.

`run_operation_definition()` and `run_operation_definition_async()` use the same internal evaluation core. Their public return contract remains `OperationResult`, and existing operation behavior must not change.

### Command-access behavior

The project defines two access policies:

```text
guild_command_access
  1. guild context exists
  2. guild is allowed by configuration

registered_guild_command_access
  1. guild context exists
  2. guild is allowed by configuration
  3. Discord user is registered
```

The empty guild allowlist continues to allow every guild. A missing guild ID continues to fail before allowlist or registration work. A disallowed guild continues to fail before a registration lookup.

Slash commands and modal submits evaluate the applicable policy and send the returned message ephemerally when denied. Autocomplete evaluates the same policy but returns `[]` without trying to send a normal interaction response.

Expected command profiles:

- `/register`: `guild_command_access`.
- `/create_community` launcher: `registered_guild_command_access` before opening the modal.
- `CreateCommunityModal.on_submit`: `registered_guild_command_access` again before validation, channel placement, database mutation, snapshot creation, or audit creation.
- `/subscribe-community`: `registered_guild_command_access` before community resolution and any automatic Discord forum-channel creation.
- `/list-subscriptions`: `registered_guild_command_access` before reading subscription data.
- `/unsubscribe-channel`: `registered_guild_command_access` before selecting or running remote/local unsubscribe operations.
- `/edit-community`, `/ban-user`, `/unban-user`, `/list-banned-users`: `guild_command_access` before their existing community-management operation checks. Existing ownership/super-admin/community-status preconditions remain authoritative for management access.
- All community autocomplete callbacks: `guild_command_access`; denied access returns no choices and performs no discovery/database/network work.

Registration remains a defensive precondition inside `subscribe_operation` and `subscribe_local_community_operation`. It may therefore run once at command ingress and once at the domain mutation boundary. Both evaluations must use the same `DISCORD_USER_REGISTERED` `Precondition` object and predicate implementation. This is deliberate boundary defense, not a second policy system.

No guild-context, allowlist, or registration policy denial writes a management audit event. These remain command-context/onboarding failures rather than management authorization failures.

## Architecture

### DiscordOps framework

Add a body-less policy abstraction while preserving `Precondition` as the only condition type.

```text
Precondition
    ↓
shared sync/async precondition evaluator
    ├── PolicyDefinition -> PolicyResult
    └── OperationDefinition -> reject callback or body callback -> OperationResult
```

The internal evaluator is responsible only for:

- ordered predicate execution;
- sync/async callback handling;
- first-failure short circuiting;
- resolving a static or callable message;
- resolving optional rejection kwargs;
- returning a neutral evaluation result.

It must not call Discord APIs, construct `OperationResult`, invoke operation rejection, or know application-specific reason codes.

`PolicyDefinition` and `PolicyResult` are public framework types. A separate class hierarchy, boolean policy DSL, decorators, dependency injection container, and automatic Discord responses are out of scope.

### Shared project preconditions

Create a project module that owns reusable operation/access preconditions. The registered-user precondition uses a structural input contract:

```python
class RegisteredDiscordUserInput(Protocol):
    def get_bridge_user(self) -> object | None: ...
```

`SubscribeInput` and `SubscribeLocalCommunityInput` already expose `get_bridge_user()`. The new command access input will expose the same method and memoize its database lookup. This permits one immutable precondition object:

```python
DISCORD_USER_REGISTERED = Precondition(
    name="discord_user_is_registered",
    message=REGISTRATION_REQUIRED_MESSAGE,
    predicate=lambda value: value.get_bridge_user() is not None,
)
```

Keep the existing reason code `discord_user_is_registered` in this change to avoid silently changing tests, logs, or adapter behavior. Renaming positive precondition names to failure-oriented reason codes is a separate compatibility decision.

### Project access policies

Create a Discord-SDK-independent access module containing:

```python
@dataclass
class CommandAccessInput:
    settings: Settings | object | None
    database: Database | Any | None
    discord_guild_id: int | None
    discord_user_id: str
```

The input provides:

- normalized access to `discord_guild_allowlist`;
- a memoized `get_bridge_user()` method;
- no `discord.Interaction` reference.

This keeps policies testable without Discord mocks and prevents domain/application policy code from sending responses.

Define reusable preconditions with the existing stable guild reasons and messages:

```text
GUILD_CONTEXT_REQUIRED -> reason `no_guild`
GUILD_ALLOWLISTED -> reason `not_allowlisted`
DISCORD_USER_REGISTERED -> reason `discord_user_is_registered`
```

Define `GUILD_COMMAND_ACCESS` and `REGISTERED_GUILD_COMMAND_ACCESS` as `PolicyDefinition` instances.

### Discord adapter

Refactor `src/commands/guild_guard.py` into a thin presentation adapter rather than a policy engine. It should:

- build `CommandAccessInput` from an interaction plus settings/database;
- call `evaluate_policy_async()`;
- send an ephemeral rejection for slash commands/modal submits;
- return a boolean or `PolicyResult` that lets handlers stop cleanly;
- evaluate the same policy for autocomplete and return only an allow/deny decision without sending a response.

The adapter must not reimplement guild allowlist membership or registration lookup logic.

A representative command path becomes:

```python
access = await evaluate_command_access(
    interaction,
    definition=REGISTERED_GUILD_COMMAND_ACCESS,
    settings=settings,
    database=database,
)
if not access.allowed:
    await send_command_access_rejection(interaction, access)
    return
```

A small combined helper may remain for handler ergonomics, but it must delegate all decisions to `evaluate_policy_async()` and only own presentation/control flow.

## Touched Files

vendor/discordops/discordops/framework.py
vendor/discordops/discordops/types.py
vendor/discordops/discordops/__init__.py
vendor/discordops/README.md
vendor/discordops/tests/framework/test_operations.py
vendor/discordops/tests/framework/test_preconditions.py
vendor/discordops/tests/test_public_api.py
src/commands/guild_guard.py
src/commands/register.py
src/commands/create_community.py
src/commands/subscribe.py
src/commands/list_subs.py
src/commands/unsubscribe.py
src/commands/edit_community.py
src/commands/ban_user.py
src/commands/unban_user.py
src/commands/list_banned_users.py
src/operations/subscribe.py
src/operations/subscribe_local_community.py
tests/commands/test_guild_guard.py
tests/commands/test_register_command.py
tests/commands/test_create_community_command.py
tests/commands/test_subscribe_command.py
tests/commands/test_list_subscriptions_command.py
tests/commands/test_unsubscribe_command.py
tests/commands/test_edit_community_command.py
tests/commands/test_ban_user_command.py
tests/commands/test_unban_user_command.py
tests/commands/test_list_banned_users_command.py
tests/operations/test_subscribe_operation.py

## New Files

vendor/discordops/tests/framework/test_policies.py
src/operations/common_preconditions.py
src/command_access.py
tests/test_command_access_policy.py

## Implementation Steps

1. Add failing DiscordOps policy tests before framework implementation.

   Cover:

   - an all-passing sync policy returns `allowed=True`;
   - a failed sync precondition returns its name, resolved message, and kwargs;
   - evaluation stops before later predicates;
   - callable messages receive the original input;
   - async policy evaluation supports mixed sync/async predicates and kwargs factories;
   - sync policy evaluation closes/rejects awaitables and points callers to `evaluate_policy_async()`;
   - policy evaluation never invokes an operation body or rejection callback because neither exists;
   - public imports expose `PolicyDefinition`, `PolicyResult`, `evaluate_policy`, and `evaluate_policy_async`.

2. Extract neutral precondition evaluation from the operation runners.

   In `vendor/discordops/discordops/framework.py`, introduce private sync and async evaluators that return a neutral internal result containing pass/fail state, reason, message, and kwargs. Preserve the current operation callback ordering and exact sync-runner error behavior.

   Refactor `run_operation_definition()` and `run_operation_definition_async()` to:

   - call the shared evaluator;
   - call `definition.reject(...)` only on failure;
   - call `definition.body(...)` only on success;
   - retain current handling for async reject/body callbacks in sync mode.

   Existing operation tests must remain green without changing their expected results.

3. Add the public policy types and runners.

   Add frozen `PolicyDefinition` and `PolicyResult` dataclasses with mandatory docstrings. Implement sync and async runners on top of the shared evaluator. Export the new API from `discordops.__init__` and document it in the vendored README with one body-less access-policy example.

4. Add shared registered-user precondition tests and implementation.

   Create `src/operations/common_preconditions.py` with the `RegisteredDiscordUserInput` protocol, registration message constant, and `DISCORD_USER_REGISTERED` precondition.

   Update `subscribe_operation` and `subscribe_local_community_operation` to import and use this exact object instead of constructing separate inline preconditions. Preserve their current input memoization, reason, and message behavior. Add an identity assertion or equivalent focused test proving both operation definitions reference the shared object.

5. Add failing project access-policy tests.

   In `tests/test_command_access_policy.py`, test `CommandAccessInput` and both policies without Discord interactions:

   - missing guild returns `no_guild` and performs no user lookup;
   - non-allowlisted guild returns `not_allowlisted` and performs no user lookup;
   - empty allowlist permits any non-null guild;
   - matching allowlist permits the configured guild;
   - registered policy rejects an unknown user with the existing registration message;
   - registered policy permits a known user;
   - repeated use of `get_bridge_user()` within one input uses one repository lookup;
   - guild-only policy does not require a database;
   - ordered short-circuiting prevents registration access for invalid guild contexts.

6. Implement project policies in `src/command_access.py`.

   Add `CommandAccessInput`, `GUILD_CONTEXT_REQUIRED`, `GUILD_ALLOWLISTED`, `GUILD_COMMAND_ACCESS`, and `REGISTERED_GUILD_COMMAND_ACCESS`. Import `DISCORD_USER_REGISTERED` from `src/operations/common_preconditions.py` rather than duplicating registration logic.

   Keep the policy module independent of `discord.py`. It may depend on project settings/database abstractions and DiscordOps types.

7. Convert `guild_guard.py` into a presentation adapter.

   Replace `GuildGuardResult`, `check_guild_allowed()`, `is_registered_discord_user()`, and direct decision logic with functions that:

   - construct `CommandAccessInput` from primitive interaction fields;
   - evaluate a supplied policy;
   - send the policy message as an ephemeral initial response or follow-up, depending on whether the interaction response is already complete;
   - expose an autocomplete helper that evaluates the same policy and returns `False`/`[]` without response side effects.

   Preserve existing message text. Prefer new names that describe policy execution, but keep temporary compatibility wrappers only if needed to avoid a risky all-at-once command migration. Remove wrappers once all in-repository callers are migrated in the same change.

8. Migrate command ingress paths profile by profile.

   Apply `GUILD_COMMAND_ACCESS` to `/register`, management command launchers, management modal submits, and management autocomplete callbacks.

   Apply `REGISTERED_GUILD_COMMAND_ACCESS` to `/create_community` launcher and submit, `/subscribe-community`, `/list-subscriptions`, and `/unsubscribe-channel`.

   For `/subscribe-community`, evaluate access before community discovery, target resolution, forum placement, or channel creation. The existing subscription operation then performs its defensive registration precondition again immediately before mutation/federation work.

   For modal flows, retain evaluation at both modal launch and modal submit. Do not rely on launch-time state at submit time.

   For autocomplete, deny by returning no choices before database/network discovery. Do not attempt an ephemeral response.

9. Update command tests around observable behavior, not helper calls.

   Rewrite tests that patch or assert legacy guard helpers so they invoke real command callbacks/modal submits and assert:

   - the correct ephemeral denial message;
   - no modal opens when denied;
   - no channel placement/creation occurs when denied;
   - no operation runner, database mutation, federation request, or management audit occurs when denied;
   - registration lookup is skipped for DM and non-allowlisted contexts;
   - autocomplete returns `[]` and avoids discovery/network calls;
   - allowed paths still reach the existing operation and preserve response visibility.

10. Run focused tests, then the full suite, and refactor only after green.

    Run at minimum:

    ```text
    pytest vendor/discordops/tests/framework vendor/discordops/tests/test_public_api.py
    pytest tests/test_command_access_policy.py tests/commands/test_guild_guard.py
    pytest tests/commands tests/operations/test_subscribe_operation.py
    pytest
    ```

    After behavior is green, remove obsolete guard types/functions, consolidate duplicated test fixtures, and verify all new modules, classes, public functions, and non-trivial blocks satisfy the project comment/docstring rules.

11. Update documentation within its ownership boundary.

    Update `vendor/discordops/README.md` because it defines the framework API and currently states that handlers verify authority outside the framework. Document policies as body-less ordered precondition evaluation and clarify that presentation remains application-owned.

    No database, federation protocol, gateway contract, deployment, or bridge event-flow documentation should change because this work changes only command ingress policy composition and framework execution structure. Record a known-issues entry only if implementation discovers a new limitation or verified regression; do not add a journal entry merely for planned work.

## Tests

### DiscordOps framework tests

- Policy success and first-failure results.
- Ordered short circuiting.
- Static and callable messages.
- Rejection kwargs propagation.
- Mixed sync/async policy callbacks.
- Sync misuse errors for async callbacks.
- Existing operation sync/async behavior after evaluator extraction.
- Public API imports and README-facing names.

### Project policy tests

- Guild context and allowlist behavior using primitive inputs.
- Empty allowlist compatibility.
- Registration success/failure.
- Registration lookup memoization.
- Shared precondition object reuse by remote and local subscribe operations.
- No database requirement for guild-only policy.

### Command scenario tests

Each test should follow user action in a defined state -> observable result.

- User invokes a command in DM -> receives guild-only rejection; no subsequent work occurs.
- User invokes a command from a non-allowlisted guild -> receives allowlist rejection; no registration lookup or operation occurs.
- Unregistered user invokes a registered-only command in an allowed guild -> receives registration guidance; no Discord channel or domain side effect occurs.
- Registered user invokes the same command -> existing behavior continues.
- User opens a create-community modal while allowed, then submits after becoming unregistered -> submit is rejected and no channel/community is created.
- Autocomplete in a denied context -> returns no choices and performs no external lookup.
- Subscription command ingress passes, but the defensive operation registration precondition fails after state changes -> operation rejects without mutation.
- Management commands continue to produce their existing management authorization/audit outcomes after command access passes.

## Compatibility Risks

- Extracting the operation evaluator can subtly change callback order, awaitable handling, coroutine closing, callable-message timing, or kwargs evaluation. Existing vendor tests must lock these contracts before refactoring.
- Changing reason codes would affect tests and potentially logs/adapters. This plan preserves `no_guild`, `not_allowlisted`, and `discord_user_is_registered`.
- A generic command helper that always requires a database would break `/register` and guild-only autocomplete. `CommandAccessInput.database` must be optional, and only the registered-user precondition may require it.
- Moving registration earlier in `/subscribe-community` changes when database reads happen but must not change success/rejection messages. It intentionally prevents discovery or channel creation for unregistered users.
- Modal interactions may already have a completed response in error paths. The presentation adapter must choose initial response versus follow-up safely.
- Reusing one registration precondition across input classes depends on the `get_bridge_user()` structural contract. All participating inputs must preserve this method and its memoization semantics.
- Running registration twice can observe changed state between ingress and mutation. That is intentional; a later failure must stop mutation and use the same rejection contract.
- Management authorization must remain in existing domain operations. Command access policies must not absorb owner/super-admin/community-state checks or alter audit reason classification.

## Regression and Blind-Spot Analysis

- Verify every registered slash command, modal launcher, modal submit, and autocomplete callback; missing one leaves an imperative or unguarded path.
- Check command callbacks that delegate to secondary handler modules, especially `subscribe_community_handler.py`, so access occurs before any placement side effect rather than only before the final operation.
- Preserve empty-allowlist behavior; treating an empty list as deny-all would be a deployment regression.
- Preserve DM behavior for commands and autocomplete even when settings are absent or lightweight test objects are used.
- Confirm local-community and remote-community subscription paths both retain the defensive registration precondition.
- Confirm list and unsubscribe commands do not accidentally lose guild scoping while access code is changed.
- Confirm denied access never writes management audit events and never gets mapped to `forbidden` management results.
- Confirm operation callers outside Discord commands remain valid because operation definitions and runners retain their public API.
- Confirm vendor tests run against the vendored source selected by `pyproject.toml`, not an installed external `discordops` package.
- Confirm no project-specific `Settings`, `Database`, Discord interaction, or audit imports enter `vendor/discordops`.

## Open Questions

None. The architectural decisions needed for implementation are fixed by the requested single-policy-system goal and the current codebase contracts.
