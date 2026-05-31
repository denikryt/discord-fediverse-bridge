# 74 — Unban user and list banned users

## Problem / Goal

The bridge now supports community-scoped local bans through `/ban-user`, stores the Discord user id that created each ban, and enforces owner-or-super-admin permissions for management commands. The moderation surface is still incomplete: a user can create an active ban but cannot remove it through Discord, and community members cannot inspect the active ban list without direct database access.

This plan adds two Discord slash commands:

```text
/unban-user community:<slug> user:<user@example.com>
/list-banned-users community:<slug>
```

The goal is to keep this stage small and local-only:

- unban deactivates an existing active ban row by setting `community_actor_bans.status = inactive`;
- unban does not delete rows;
- unban does not send any federated `Undo`, `Block`, `Reject`, or other ActivityPub moderation activity;
- list-banned-users shows active local bridge bans only;
- both commands use observable command behavior tests, not implementation-only tests;
- runtime checks remain the security boundary even where autocomplete narrows available choices.

This plan also fixes the repeated ban/unban lifecycle. The current uniqueness rule is:

```text
(local_community_id, actor_handle, status)
```

Creating a new inactive row for every unban would eventually collide when the same actor is banned, unbanned, banned, and unbanned again. For this version, the project should not implement full historical ban cycles. Instead, a new `/ban-user` after an unban should reactivate the existing inactive row for that community and actor handle.

## Expected Behavior

### `/unban-user` command shape

Add a new Discord slash command:

```text
/unban-user community:<slug> user:<user@example.com>
```

The command can be invoked from a guild. It should not use `@app_commands.default_permissions(manage_channels=True)` because community owners may not have Discord `Manage Channels`. Runtime preconditions are the authorization boundary.

All command responses are ephemeral.

Success response:

```text
Unbanned alice@example.com from community cats.
```

Expected errors:

```text
This command can only be used inside a guild.
```

```text
Unknown or inaccessible local community: cats
```

```text
You are not allowed to manage this local community.
```

```text
Invalid remote user handle. Use user@example.com.
```

```text
User alice@example.com is not actively banned in community cats.
```

The no-active-ban error is intentionally generic. It must not reveal whether an inactive historical row exists.

### `/unban-user` permission behavior

`/unban-user` follows the ownership model introduced by plan 73:

- a community owner can unban any active ban in their own community;
- a community owner can unban a row originally created by a super-admin or another prior authorized user;
- a super-admin can unban any active ban in any local community;
- unrelated users cannot unban;
- legacy NULL-owner communities remain manageable only by super-admins.

The runtime check must also handle guild boundaries:

- a normal owner can manage a community only when the command is invoked from that community's Discord guild;
- a manually entered slug from another guild is treated as unknown or inaccessible for non-super-admin callers;
- a super-admin can manage a community from any guild by manually entering the slug.

The precondition order should prevent unnecessary information leaks:

1. command is in a guild;
2. community exists and is accessible in this invocation context;
3. caller can manage the community;
4. user handle is valid;
5. active ban exists.

### `/unban-user` autocomplete

`/unban-user` has autocomplete for both arguments:

```text
community:<autocomplete>
user:<autocomplete>
```

`community` autocomplete behavior:

- normal owners see active local communities they own in the current guild;
- super-admins see all active local communities across all guilds;
- submitted values are stable slugs;
- labels may include enough context to distinguish communities, for example slug/display name/guild id if needed;
- autocomplete is UX only and must not replace runtime preconditions.

`user` autocomplete behavior:

- it is filtered by the selected `community` value;
- if `community` is empty, unknown, inaccessible, or not manageable by the caller, return an empty list;
- normal owners see all active bans in their own selected community;
- super-admins see all active bans in the selected community;
- labels include handle and reason;
- submitted values are the normalized actor handles.

Example user autocomplete labels:

```text
alice@example.com — spam
bob@example.org — reason not specified
```

Example submitted values:

```text
alice@example.com
bob@example.org
```

Autocomplete should follow Discord's choice limit. Return at most 25 choices and filter by the current typed value case-insensitively against the actor handle and, if useful, the reason text.

### `/list-banned-users` command shape

Add a new Discord slash command:

```text
/list-banned-users community:<slug>
```

This command is intentionally public to invoke: it should not require owner or super-admin status. The response is still ephemeral because active ban state and reasons are moderation data.

The command can be invoked from a guild. It should not use `@app_commands.default_permissions(manage_channels=True)`.

Expected responses:

When active bans exist:

```text
Banned users in community cats:
- alice@example.com — spam
- bob@example.org — reason not specified
```

When there are no active bans:

```text
Community cats has no active bans.
```

When the list is truncated:

```text
Banned users in community cats:
- user1@example.com — spam
...
Showing 20 of 57 active bans.
```

Expected errors:

```text
This command can only be used inside a guild.
```

```text
Unknown or inaccessible local community: cats
```

### `/list-banned-users` visibility and guild behavior

`/list-banned-users` is visible/invocable to everyone, but the selected community is scoped to the current guild for normal users.

Runtime behavior:

- normal users can list active bans only for active local communities in the current Discord guild;
- manually entering a slug from another guild returns the same unknown/inaccessible error for normal users;
- super-admins can manually list a community from another guild;
- super-admin cross-guild access is a runtime capability, not exposed through list command autocomplete.

`community` autocomplete behavior for `/list-banned-users`:

- show all active local communities in the current guild;
- do not filter to only communities with active bans;
- do not require owner or super-admin status;
- submitted values are stable slugs.

### List output rules

`/list-banned-users` shows active bans only:

- include `actor_handle`;
- include `reason`, or `reason not specified` when empty;
- do not include `actor_url`;
- do not include `created_by_discord_user_id`;
- do not include inactive rows;
- order by `created_at DESC` so newest bans appear first;
- limit the visible output to 20 rows;
- include `Showing 20 of N active bans.` when more than 20 active bans exist.

### Repeated ban/unban lifecycle

Update the ban repository and `/ban-user` behavior so the lifecycle is safe under the existing uniqueness rule.

Current simple lifecycle:

```text
ban -> active row
unban -> same row becomes inactive
ban again -> same row becomes active again
```

When reactivating an inactive row:

- preserve `created_at`;
- update `status` to `active`;
- update `reason` to the new reason or `None`;
- update `created_by_discord_user_id` to the caller who re-banned;
- update `actor_url` if the new ban call provides one and the row has no actor URL;
- update `updated_at`.

Do not implement full ban history in this plan. A later audit/history model can replace this compromise with a richer event log.

## Test Plan Summary

The implementation must be driven by concrete observable-behavior tests before the command code is completed. These tests should describe the system state, execute the command or autocomplete path, and assert the user-visible response plus final database state. The test suite must not rely only on isolated helper tests or on checking that functions were called.

Required test groups:

1. `/unban-user` command behavior.

   Cover owner success, super-admin success, non-owner rejection, unknown or inaccessible community, invalid handle, no active ban, legacy NULL-owner behavior, guild boundary behavior, cross-guild super-admin behavior, no-guild invocation, and user/community autocomplete.

2. `/list-banned-users` command behavior.

   Cover public current-guild listing, empty active list, reason fallback, inactive-row exclusion, newest-first ordering, 20-row truncation, ordinary cross-guild rejection, super-admin cross-guild listing by manual slug, unknown community, no-guild invocation, and community autocomplete.

3. Ban lifecycle regression.

   Cover ban -> unban -> ban again, proving the inactive row is reactivated rather than creating duplicate historical inactive rows under the existing uniqueness rule. Also preserve active duplicate rejection and inbound ban enforcement for reactivated rows.

4. Adapter-visible behavior.

   Cover that the Discord command adapters pass `interaction.user.id` and `interaction.guild_id`, return ephemeral responses, and expose autocomplete choices matching the plan's access rules. These tests should still assert observable command behavior, not Discord internals.

The detailed scenario list is in the `Tests` section below. Implementation steps must refer back to those scenarios rather than replacing them with a generic “run tests” instruction.

## Architecture

### Repository changes

Extend `src/db/repositories/community_actor_bans.py`.

Add methods similar to:

```python
def get_inactive_ban_by_handle(
    *,
    local_community_id: int,
    actor_handle: str,
) -> CommunityActorBan | None: ...


def deactivate_active_ban_by_handle(
    *,
    local_community_id: int,
    actor_handle: str,
) -> CommunityActorBan | None: ...


def list_active_bans_for_community(
    *,
    local_community_id: int,
    limit: int | None = None,
    offset: int = 0,
) -> list[CommunityActorBan]: ...


def count_active_bans_for_community(
    *,
    local_community_id: int,
) -> int: ...
```

Update `create_active_ban(...)` so it is no longer always insert-only:

1. Look for an inactive row by `(local_community_id, actor_handle)`.
2. If found, reactivate that row and update active-ban fields as described above.
3. If not found, insert a new active row as today.

The caller still checks duplicate active bans before creating/reactivating so duplicate messages can include the existing reason.

Repository ordering for list methods must be:

```python
order_by(CommunityActorBan.created_at.desc(), CommunityActorBan.id.desc())
```

The `id` tie-breaker keeps results stable when rows have the same timestamp.

### Local community repository changes

Extend `src/db/repositories/local_communities.py` with active-community listing helpers for autocomplete and guild-bound validation.

Suggested methods:

```python
def list_active_local_communities_by_guild(
    *,
    discord_guild_id: int,
) -> list[LocalCommunity]: ...


def list_active_local_communities_owned_by_user_in_guild(
    *,
    discord_guild_id: int,
    created_by_discord_user_id: str,
) -> list[LocalCommunity]: ...


def list_active_local_communities(self) -> list[LocalCommunity]: ...
```

Use `status == "active"`. Sort by `slug` or display name consistently; autocomplete should be stable and predictable.

Runtime command operations can continue loading by slug first because `slug` is globally unique, but they must then apply guild-access rules.

### Permission and guild-access helpers

Keep `src/local_community_permissions.py` as the owner/super-admin policy module. Add small helpers rather than duplicating role checks in each operation.

Suggested helpers:

```python
def is_super_admin(*, settings: Settings, discord_user_id: str) -> bool: ...


def can_access_local_community_from_guild(
    *,
    settings: Settings,
    discord_user_id: str,
    discord_guild_id: int | None,
    local_community: LocalCommunity,
    public_current_guild_only: bool = False,
) -> bool: ...
```

The exact function shape can differ, but implementation must preserve these contracts:

- super-admin check compares Discord ids as strings, with no integer coercion;
- owner management still uses `can_manage_local_community(...)`;
- non-super-admin command access to a guild-scoped community requires `interaction.guild_id == local_community.discord_guild_id`;
- `/list-banned-users` is public for the current guild and does not require owner status;
- super-admin can manually access cross-guild communities for both commands.

### `/unban-user` operation

Add `src/operations/unban_user.py` using `discordops.Operation` and ordered `Precondition` objects.

Suggested input/result dataclasses:

```python
@dataclass(slots=True)
class UnbanUserInput:
    database: Database
    settings: Settings
    discord_user_id: str
    discord_guild_id: int | None
    community_slug: str
    actor_handle: str


@dataclass(slots=True)
class UnbanUserResult:
    applied: bool
    message: str
    reason: str
```

The input can memoize the loaded community, normalized actor handle, and active ban the same way `BanUserInput` does. This avoids repeated lookups while keeping precondition order explicit.

Preconditions:

1. `guild_context` — fail with `This command can only be used inside a guild.` if `discord_guild_id is None`.
2. `community_accessible` — load by slug and apply guild-access rules. Fail with `Unknown or inaccessible local community: <slug>`.
3. `can_manage_community` — owner or super-admin. Fail with `You are not allowed to manage this local community.`.
4. `valid_actor_handle` — normalize `user@example.com`. Fail with `Invalid remote user handle. Use user@example.com.`.
5. `active_ban_exists` — active row exists. Fail with `User <handle> is not actively banned in community <slug>.`.

Body:

- deactivate the active ban row by handle;
- return `Unbanned <handle> from community <slug>.`;
- do not expose old reason or created date.

### `/list-banned-users` operation

Add `src/operations/list_banned_users.py` using `discordops.Operation`.

Suggested input/result dataclasses:

```python
@dataclass(slots=True)
class ListBannedUsersInput:
    database: Database
    settings: Settings
    discord_user_id: str
    discord_guild_id: int | None
    community_slug: str
    limit: int = 20


@dataclass(slots=True)
class ListBannedUsersResult:
    applied: bool
    message: str
    reason: str
```

Preconditions:

1. `guild_context` — fail with `This command can only be used inside a guild.`.
2. `community_accessible` — community exists and is accessible. For normal users this means the current guild; for super-admins manual cross-guild slug is allowed.

Body:

- count active bans for the community;
- if count is 0, return `Community <slug> has no active bans.`;
- fetch up to 20 active bans ordered newest first;
- build the ephemeral response with handle and reason;
- append `Showing 20 of N active bans.` when truncated.

The result can use `applied=True` for successful list execution, including empty lists. Rejections use `applied=False`.

### Discord command adapters

Add:

```text
src/commands/unban_user.py
src/commands/list_banned_users.py
```

Register both in:

```text
src/commands/__init__.py
src/discord_bot.py
```

Both adapters should:

- extract `str(interaction.user.id)`;
- pass `interaction.guild_id` into the operation input;
- call the operation;
- reply with `ephemeral=True`;
- define autocomplete callbacks as described above;
- avoid Discord default permission gates.

The adapters can keep autocomplete functions local to their modules unless reuse becomes clearer during implementation.

### Autocomplete implementation details

Use `discord.app_commands.Choice[str]` values.

Community choice values:

```text
cats
```

Community choice labels should stay short enough for Discord and can use one of these styles:

```text
cats
cats — Cats
cats — Cats — guild 123456789
```

Use extra guild context in labels only where needed to distinguish super-admin cross-guild choices.

User choice values:

```text
alice@example.com
```

User choice labels:

```text
alice@example.com — spam
alice@example.com — reason not specified
```

If a label would exceed Discord's choice-name limit, truncate the reason while preserving the full handle and submitted value.

Autocomplete callbacks should catch unexpected exceptions, log them, and return an empty list. Autocomplete must not raise errors into Discord for transient DB or state problems.

### Existing `/ban-user` change for reactivation

The `/ban-user` operation already rejects duplicate active bans. Keep that behavior.

Change the repository behavior beneath `create_active_ban(...)` so that, when no active duplicate exists but an inactive historical row exists for the same community and handle, the row is reactivated instead of inserting a new row.

This keeps `/ban-user` command behavior unchanged for active duplicates while making repeated unban/re-ban safe.

## Touched Files

```text
src/db/repositories/community_actor_bans.py
src/db/repositories/local_communities.py
src/local_community_permissions.py
src/operations/__init__.py
src/operations/ban_user.py
src/commands/__init__.py
src/discord_bot.py
docs/architecture/database-map.md
docs/development/navigation.md
dev/future_tasks.md
tests/operations/test_ban_user_operation.py
tests/commands/test_ban_user_command.py
tests/conftest.py
```

## New Files

```text
plans/74_unban_user_and_list_banned_users.md
src/operations/unban_user.py
src/operations/list_banned_users.py
src/commands/unban_user.py
src/commands/list_banned_users.py
tests/operations/test_unban_user_operation.py
tests/operations/test_list_banned_users_operation.py
tests/commands/test_unban_user_command.py
tests/commands/test_list_banned_users_command.py
```

If implementation keeps autocomplete helpers in a shared module, add it explicitly, for example:

```text
src/commands/local_community_autocomplete.py
tests/commands/test_local_community_autocomplete.py
```

Do not add that shared file unless it genuinely removes duplication between command adapters.

## Implementation Steps

1. Add failing observable behavior tests for `/unban-user` using the scenarios in the `Tests` section.

   These tests must set up concrete database state, invoke the command/operation path as a user action, and assert both the response and final ban-row state. At minimum, implement the owner success, super-admin success, non-owner rejection, no-active-ban, invalid-handle, guild-boundary, legacy NULL-owner, and autocomplete scenarios before writing the operation body.

2. Add failing observable behavior tests for `/list-banned-users` using the scenarios in the `Tests` section.

   These tests must set up concrete communities and ban rows, invoke the list command/operation path, and assert the exact visible output rules: ephemeral response, handles, reasons, inactive exclusion, ordering, truncation, guild scoping, and super-admin cross-guild behavior.

3. Add command adapter and autocomplete behavior tests.

   Test that both command adapters pass `interaction.user.id` and `interaction.guild_id`, always respond with `ephemeral=True`, and return autocomplete choices matching the access rules. Autocomplete tests must cover accessible, inaccessible, empty, inactive, owner, super-admin, and 25-choice cap cases.

4. Extend the ban repository.

   Add deactivate, list, count, inactive lookup, and reactivation behavior in `create_active_ban(...)`. Keep comments around the uniqueness constraint and repeated ban/unban lifecycle because this is subtle compatibility logic.

5. Extend local community repository helpers for active community autocomplete.

   Add explicit methods rather than scattering `select(LocalCommunity)` queries through command adapters.

6. Extend local-community permission helpers.

   Add a clear `is_super_admin(...)` helper and any guild-access helper needed by the command operations. Keep string-only Discord id comparison.

7. Implement `UnbanUserOperation` with `discordops`.

   Follow the precondition order from this plan. Memoize derived state in the input dataclass so tests can assert short-circuit behavior through observable side effects.

8. Implement `ListBannedUsersOperation` with `discordops`.

   Keep list output formatting in the operation unless Discord-specific rendering becomes necessary. The command adapter should remain thin.

9. Add Discord command adapters and autocomplete.

   Register `/unban-user` and `/list-banned-users` in the command package and bot startup.

10. Update `/ban-user` tests for reactivation.

    Add a scenario: active ban -> unban/deactivate via repository or operation -> `/ban-user` again -> same row id becomes active, reason and `created_by_discord_user_id` update, no extra row is created.

11. Update documentation.

    Update only documents whose purpose covers the changed behavior. At minimum inspect and likely update:

    ```text
    docs/architecture/database-map.md
    docs/development/navigation.md
    dev/future_tasks.md
    ```

    In `dev/future_tasks.md`, mark `/unban-user` and `/list-banned-users` as planned/being implemented rather than leaving them as undefined future work. Keep pagination, full audit/history, federated moderation, and inactive historical list views as future work.

12. Run focused tests, then full suite.

## Tests

Tests must be written as concrete user action under a defined system state leading to an observable result. Unit tests are allowed only where they reduce risk for narrow policy helpers; the main coverage must exercise command/operation behavior.

### `/unban-user` scenarios

1. Owner unbans an active ban in their own community.

   Given:
   - guild `10` has active local community `cats` owned by Discord user `111`;
   - `alice@example.com` is actively banned in `cats`;
   - caller is `111` and not super-admin.

   When:
   - `/unban-user community:cats user:alice@example.com` is invoked.

   Then:
   - response is ephemeral;
   - result message is `Unbanned alice@example.com from community cats.`;
   - the existing row has `status="inactive"`;
   - no row is deleted;
   - no new row is created.

2. Owner unbans a ban that was created by a super-admin.

   Assert that community ownership, not ban creator identity, controls unban authorization.

3. Super-admin unbans in another user's community.

4. Super-admin unbans a community from another guild by manually entering slug.

5. Ordinary non-owner is rejected and the active row remains active.

6. Owner is rejected for a manually entered slug from another guild.

7. Legacy NULL-owned community can be unbanned only by super-admin.

8. Unknown slug returns unknown/inaccessible and no ban rows change.

9. Invalid handle is rejected for an authorized caller and no rows change.

10. Unauthorized caller with invalid handle is rejected by permission before handle validation.

11. No active ban returns:

    ```text
    User alice@example.com is not actively banned in community cats.
    ```

    Assert this is the same whether no row exists or only an inactive row exists.

12. Command invoked without guild returns guild-context error and no rows change.

13. Autocomplete for `community`:

    - owner sees own active communities in current guild;
    - owner does not see another guild's communities;
    - super-admin sees all active communities across all guilds;
    - inactive communities are excluded.

14. Autocomplete for `user`:

    - empty selected community returns empty list;
    - inaccessible selected community returns empty list;
    - selected owned community returns all active bans in that community;
    - inactive bans are excluded;
    - labels include handle and reason/fallback;
    - values are handles;
    - result count is capped at 25.

### `/list-banned-users` scenarios

1. Public caller lists active bans for a current-guild community.

   Given:
   - guild `10` has active local community `cats`;
   - active bans exist for `alice@example.com` and `bob@example.org`.

   When:
   - `/list-banned-users community:cats` is invoked by any user in guild `10`.

   Then:
   - response is ephemeral;
   - output includes both handles;
   - output includes reason text or `reason not specified`;
   - output does not include actor URLs or moderator Discord user ids.

2. Empty community returns:

   ```text
   Community cats has no active bans.
   ```

3. Inactive rows are excluded.

4. Rows are sorted by `created_at DESC`, with `id DESC` tie-breaker.

5. More than 20 active bans returns exactly 20 visible rows plus:

   ```text
   Showing 20 of N active bans.
   ```

6. Ordinary user is rejected for a manually entered slug from another guild.

7. Super-admin can list a manually entered slug from another guild.

8. Unknown slug returns unknown/inaccessible.

9. Command invoked without guild returns guild-context error.

10. Community autocomplete:

    - shows all active local communities in the current guild;
    - does not require owner/super-admin;
    - excludes inactive communities;
    - returns stable slug values;
    - result count is capped at 25.

### Ban reactivation regression

1. Ban, unban, ban again for the same community and actor.

   Assertions:

   - the same `CommunityActorBan.id` is reused;
   - `status` returns to `active`;
   - `reason` updates to the latest ban reason;
   - `created_by_discord_user_id` updates to the latest banner;
   - `created_at` remains unchanged;
   - `updated_at` changes;
   - there is still exactly one row for that community and handle.

2. Active duplicate ban still rejects and includes the existing reason.

3. Inbound ban enforcement still matches reactivated rows.

### Focused test commands

Run the new and touched command/operation tests first:

```bash
./.venv/bin/pytest -q \
  tests/operations/test_unban_user_operation.py \
  tests/operations/test_list_banned_users_operation.py \
  tests/operations/test_ban_user_operation.py \
  tests/commands/test_unban_user_command.py \
  tests/commands/test_list_banned_users_command.py \
  tests/commands/test_ban_user_command.py
```

Then run the local-community user-ban behavior tests:

```bash
./.venv/bin/pytest -q \
  tests/behavior/test_local_community_user_ban_scenarios.py
```

Then run the full suite:

```bash
./.venv/bin/pytest -q
```

## Regression / Blind-Spot Analysis

### Runtime authorization vs autocomplete

Autocomplete is not a security boundary. Users can type arbitrary slugs and handles manually. Both new operations must enforce guild boundary and owner/super-admin policy at runtime.

### Cross-guild behavior

`slug` is globally unique, so a manually typed slug can reference another guild's community. Normal users must not list or unban another guild's community. Super-admins are explicitly allowed to do so. Tests must cover both paths.

### Public list command data exposure

`/list-banned-users` is invocable by ordinary users, but response output is ephemeral and must avoid moderator ids and actor URLs. Reasons are intentionally shown because the command's selected behavior includes reason display.

### Repeated ban/unban uniqueness collision

The current uniqueness rule includes `status`. Without reactivation, a second unban cycle can collide on another inactive row. Repository tests must prove the row is reused.

### Lost active duplicate behavior

Changing `create_active_ban(...)` to reactivate inactive rows must not make active duplicate bans silently update reasons. Active duplicate rejection remains a `/ban-user` precondition.

### Timestamp semantics

Reactivation preserves `created_at`, so `created_at DESC` means “original first ban time,” not latest re-ban time. This is acceptable for v1 because full audit/history is out of scope. The plan should not pretend `created_at` is a complete moderation-event timestamp.

### Long reasons and Discord limits

List output is limited to 20 rows, but long reasons can still threaten Discord message limits. Implementation should keep reason lines compact and may defensively truncate individual reasons in output while preserving stored DB values. If truncation is added, tests should assert visible output only, not stored reason mutation.

### Inactive historical visibility

Inactive bans are not shown in `/list-banned-users` and are not exposed by `/unban-user` no-active-ban errors. A future audit/list-history command can decide how much history to show.

### Federated semantics

Unban is local-only. It does not send ActivityPub moderation objects. Existing inbound enforcement simply stops matching the inactive row. This is local bridge behavior, not a Fediverse protocol claim.

## Future Work / Explicitly Out of Scope

- paginated `/list-banned-users` output beyond the first 20 active bans;
- inactive ban history listing;
- full ban audit/event model;
- owner transfer and per-community moderator roles;
- role-system split between creator permissions and super-admin permissions;
- federated moderation actions for ban or unban;
- dashboard ban list;
- actor URL / WebFinger identity resolution;
- editing ban reasons;
- Discord-originated moderation.

## Open Questions

None. The interaction decisions for this plan are fixed:

- `/unban-user` success response shows only the fact of unban;
- no-active-ban error is generic;
- `/unban-user user` autocomplete is filtered by selected community;
- `/list-banned-users` is public to invoke but ephemeral;
- list output includes handles and reasons;
- empty list says `Community <slug> has no active bans.`;
- list output shows 20 rows maximum;
- `/unban-user` owners can unban any active ban in their own community;
- `/list-banned-users` community autocomplete shows all active communities in current guild;
- `/unban-user` community autocomplete shows owner-owned current-guild communities for owners and all active communities across all guilds for super-admins;
- `/unban-user user` autocomplete labels include handle and reason;
- list ordering is newest first by `created_at DESC`;
- normal users are current-guild scoped, while super-admins can manually use cross-guild slugs;
- repeated ban/unban reuses the inactive ban row rather than creating multiple inactive historical rows;
- reactivation updates reason, banner id, and updated_at while preserving created_at.
