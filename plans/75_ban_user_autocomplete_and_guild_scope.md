# 75 — Ban user autocomplete and guild scope

## Problem / Goal

The bridge now has a consistent local-community moderation model for unban and
ban-list commands:

```text
/unban-user community:<autocomplete> user:<autocomplete>
/list-banned-users community:<autocomplete>
```

Those commands use guild-aware runtime checks and autocomplete helpers. The
older `/ban-user` command still accepts a manual community slug only, and its
operation input does not carry `interaction.guild_id`. That leaves `/ban-user`
behind the rest of the moderation surface:

- owners do not get autocomplete for the communities they can manage;
- super-admins do not get the same cross-guild autocomplete behavior available
  on `/unban-user`;
- owner callers can manually enter a slug without the same explicit guild-scope
  precondition used by `/unban-user`;
- the future-tasks journal still lists moderation-command autocomplete as future
  work even though `/unban-user` and `/list-banned-users` now implement it.

The goal of this plan is to bring `/ban-user` in line with the current
moderation-command model without changing the ban semantics introduced by plans
72, 73, and 74.

This plan adds:

```text
/ban-user community:<autocomplete> user:<user@example.com> reason:<optional>
```

It keeps these constraints:

- autocomplete is UX only;
- runtime `discordops` preconditions remain the security boundary;
- `/ban-user` remains local-only and does not send federated moderation
  activities;
- no user autocomplete is added for `/ban-user` because the target user is not
  selected from an existing local ban list;
- no dynamic Discord command visibility is added.

## Expected Behavior

### `/ban-user` command shape

The command shape remains:

```text
/ban-user community:<slug> user:<user@example.com> reason:<optional text>
```

The `community` argument gains autocomplete.

The `user` argument remains free text in the same normalized remote handle shape
already used by `/ban-user`:

```text
alice@example.com
```

The command continues to respond ephemerally for success and errors.

### `/ban-user community:` autocomplete

Autocomplete must use the same project policy as `/unban-user community:`:

- normal owner callers see active local communities they own in the current
  Discord guild;
- super-admin callers see active local communities across all guilds;
- inactive communities are not shown;
- legacy NULL-owner communities are not shown to normal users;
- legacy NULL-owner communities are shown to super-admins;
- choices are filtered by the typed `current` text against slug and display
  name;
- returned values are stable community slugs;
- this relies on the existing global uniqueness constraint on
  `local_communities.slug`; do not introduce `guild_id:slug` command values in
  this plan;
- labels may include slug and display name;
- labels for super-admin cross-guild results should include guild context to
  reduce ambiguity;
- return at most 25 Discord choices;
- autocomplete failures must be logged and return an empty list.

Suggested labels:

```text
cats — Cats
cats — Cats — guild 99999
```

Use the shorter label when guild context is not needed. Use the existing
100-character label truncation style from `src/commands/unban_user.py` and
`src/commands/list_banned_users.py`.

### Runtime guild-scope behavior

`/ban-user` must pass `interaction.guild_id` into `BanUserInput` and enforce the
same guild access model already used by `/unban-user`:

- owner callers can ban in active communities they own in the current guild;
- owner callers cannot manually enter a slug from another guild;
- owner callers cannot ban in inactive communities;
- owner callers cannot manage legacy NULL-owner communities;
- super-admin callers can manually enter any active local-community slug across
  guilds;
- super-admin callers can manage legacy NULL-owner active communities;
- no caller can manage inactive communities in this stage;
- if the community is missing, inactive, or inaccessible from the current guild,
  return the same unknown/inaccessible style used by the newer commands.

The command should reject guildless non-super-admin calls because owner access is
guild-scoped. Super-admin guildless behavior may follow the existing
`can_access_local_community_from_guild` helper: super-admins can access active
communities regardless of `discord_guild_id`.

### Precondition order

`BanUserOperation` must keep an information-safe precondition order. Update the
existing order to include guild accessibility before management and handle
validation:

```text
1. community is active and accessible from this guild for this caller
2. caller is owner or super-admin for that community
3. remote actor handle is valid
4. duplicate active ban does not exist
```

The first precondition may still load by slug internally, but its user-visible
message should not distinguish between missing, inactive, and inaccessible
communities.

Expected error for missing/inactive/inaccessible community:

```text
Unknown or inaccessible local community: cats
```

Expected error when the community is accessible but the caller is not allowed to
manage it:

```text
You are not allowed to manage this local community.
```

Invalid handle and duplicate active ban messages remain unchanged:

```text
Invalid remote user handle. Use user@example.com.
```

```text
User alice@example.com is already banned in community cats.
Reason: spam
```

### No behavior changes to ban persistence

Keep the plan-74 ban lifecycle behavior:

- duplicate active bans are rejected;
- inactive historical rows are reactivated by
  `CommunityActorBanRepository.create_active_ban()`;
- reactivation updates `reason`, `created_by_discord_user_id`, and `updated_at`;
- reactivation keeps the original `created_at`;
- no new inactive-history/audit model is introduced.

### Future tasks journal

Update `dev/future_tasks.md` so the moderation autocomplete item reflects the
new state:

- `/unban-user` autocomplete is implemented;
- `/list-banned-users` autocomplete is implemented;
- `/ban-user` autocomplete is implemented by this plan;
- remaining future work covers future management commands such as
  `/edit-community`, disable/archive, subscription approvals, and possible
  dynamic Discord command visibility.

Do not delete broader future-work items that still apply.

## Architecture

### Command adapter

Update:

```text
src/commands/ban_user.py
```

Add autocomplete helpers modeled on `src/commands/unban_user.py`:

```python
def _ban_community_autocomplete(database: Database, settings: Settings):
    """Build autocomplete for `/ban-user community` with owner/admin scope."""
```

The helper should use:

```python
is_super_admin(settings=settings, discord_user_id=discord_user_id)

database.local_communities.list_active_local_communities()

database.local_communities.list_active_local_communities_owned_by_user_in_guild(
    discord_guild_id=interaction.guild_id,
    created_by_discord_user_id=discord_user_id,
)
```

Then register it:

```python
@app_commands.autocomplete(
    community=_ban_community_autocomplete(database, settings),
)
```

Update the operation call to include guild context:

```python
BanUserInput(
    database=database,
    settings=settings,
    discord_user_id=str(interaction.user.id),
    discord_guild_id=interaction.guild_id,
    community_slug=community,
    actor_handle=user,
    reason=reason,
)
```

The command must continue sending `ephemeral=True`.

### Operation input and preconditions

Update:

```text
src/operations/ban_user.py
```

Add `discord_guild_id`:

```python
@dataclass(slots=True)
class BanUserInput:
    database: Database
    settings: Settings
    discord_user_id: str
    discord_guild_id: int | None
    community_slug: str
    actor_handle: str
    reason: str | None = None
```

Import and use:

```python
from ..local_community_permissions import (
    can_access_local_community_from_guild,
    can_manage_local_community,
)
```

Replace the first precondition with a community-access precondition that handles
all of these as the same user-visible condition:

- slug missing;
- community inactive;
- normal caller entered a community from another guild;
- guildless normal caller entered a community slug.

Suggested predicate:

```python
def _community_accessible(operation_input: BanUserInput) -> bool:
    community = operation_input.get_local_community()
    if community is None:
        return False
    return can_access_local_community_from_guild(
        settings=operation_input.settings,
        discord_user_id=operation_input.discord_user_id,
        discord_guild_id=operation_input.discord_guild_id,
        local_community=community,
    )
```

Then keep `_can_manage_community()` as the second precondition. This preserves a
separate rejection when the community is in the current guild but owned by
someone else.

Update rejection reason mapping:

```python
_REJECTION_REASONS = {
    "community_accessible": "unknown_or_inaccessible_community",
    "can_manage_community": "cannot_manage_community",
    "valid_actor_handle": "invalid_handle",
    "no_duplicate_active_ban": "duplicate_active_ban",
}
```

Use this message:

```python
message=lambda op: f"Unknown or inaccessible local community: {op.normalized_community_slug}"
```

### Permission helpers

Prefer reusing existing helpers in:

```text
src/local_community_permissions.py
```

Do not add another permission model if the current helpers already cover the
needed behavior.

A small helper is acceptable only if it removes duplication between ban and
unban autocomplete, for example:

```python
def list_manageable_active_local_communities_for_autocomplete(...):
    ...
```

If such a helper is added, keep it narrow and test it through command behavior
or existing permission tests. Do not do a broad permission refactor in this
plan.

### Repository

No new repository is required. Reuse existing methods:

```text
src/db/repositories/local_communities.py
```

Existing methods already needed for this plan:

```python
list_active_local_communities()
list_active_local_communities_owned_by_user_in_guild(...)
```

No schema migration is required.

### Documentation and journal

Review documentation as required by `AGENTS.md`. The likely affected files are:

```text
docs/development/navigation.md
dev/future_tasks.md
```

Update `docs/development/navigation.md` only if its command/operation reading
path mentions moderation commands or autocomplete. Update `dev/future_tasks.md`
for the now-completed `/ban-user` autocomplete item.

## Touched Files

```text
src/commands/ban_user.py
src/operations/ban_user.py
src/local_community_permissions.py
docs/development/navigation.md
dev/future_tasks.md
tests/commands/test_ban_user_command.py
tests/operations/test_ban_user_operation.py
tests/test_local_community_permissions.py
```

`src/local_community_permissions.py`, `docs/development/navigation.md`, and
`tests/test_local_community_permissions.py` are touched only if implementation
or documentation review shows a real need. Do not edit them just to satisfy this
list.

## New Files

```text
plans/75_ban_user_autocomplete_and_guild_scope.md
```

No new runtime module is expected.

## Implementation Steps

1. Write failing observable-behavior tests for `/ban-user` runtime guild scope.

   The tests should drive `ban_user_operation()` with concrete database state,
   not isolated predicates alone. Cover the matrix in the `Tests` section before
   changing production code.

2. Write failing command-adapter/autocomplete tests for `/ban-user`.

   Register the command with the existing `command_tree` fixture and call the
   real autocomplete callback using fake interaction objects. Assert returned
   `Choice` names and values, not just that a repository method was called.

3. Add `discord_guild_id` to `BanUserInput`.

   Update every caller and test fixture. The command adapter must pass
   `interaction.guild_id`.

4. Replace the first `BanUserOperation` precondition with a guild-aware
   community-access precondition.

   Keep the precondition order information-safe: no handle validation or
   duplicate-ban lookup before community access and management checks succeed.

5. Add `/ban-user community:` autocomplete.

   Follow the existing style in `src/commands/unban_user.py` and
   `src/commands/list_banned_users.py`: label truncation, typed filtering,
   exception logging, and 25-choice limit.

6. Preserve existing ban persistence behavior.

   Run the existing duplicate-ban and reactivation tests. Do not change
   `CommunityActorBanRepository.create_active_ban()` unless tests reveal a real
   bug.

7. Update docs and future-tasks journal.

   Mark `/ban-user` community autocomplete as implemented. Keep future entries
   for management-command autocomplete beyond the three moderation commands.

8. Run focused tests, then the full suite.

   Focused tests should include at least:

   ```bash
   ./.venv/bin/pytest -q \
     tests/commands/test_ban_user_command.py \
     tests/operations/test_ban_user_operation.py \
     tests/test_local_community_permissions.py
   ```

   Then run moderation regression tests:

   ```bash
   ./.venv/bin/pytest -q \
     tests/commands/test_unban_user_command.py \
     tests/commands/test_list_banned_users_command.py \
     tests/operations/test_unban_user_operation.py \
     tests/operations/test_list_banned_users_operation.py \
     tests/behavior/test_local_community_user_ban_scenarios.py
   ```

   Finally run:

   ```bash
   ./.venv/bin/pytest -q
   ```

## Tests

Tests must follow the project rule: user action in a defined system state ->
observable result. Do not rely on implementation-only predicate tests as the
main proof.

### `/ban-user` operation scenarios

1. Owner bans in own active community in current guild.

   Given:
   - local community `cats` exists;
   - `discord_guild_id=99999`;
   - `created_by_discord_user_id="owner-1"`;
   - caller is `owner-1`;
   - no active ban exists for `alice@example.com`.

   When:
   - `/ban-user community:cats user:alice@example.com reason:spam` is executed
     through `ban_user_operation()`.

   Then:
   - result is applied;
   - response says `Banned alice@example.com from community cats.`;
   - one active ban row exists;
   - `created_by_discord_user_id` on the ban row is `owner-1`.

2. Owner cannot ban in own community from another guild by manual slug.

   Given:
   - `cats` exists in guild `111`;
   - caller is the owner;
   - command context guild is `222`.

   Then:
   - result is rejected with
     `Unknown or inaccessible local community: cats`;
   - no ban row is created;
   - handle validation is not required to fail or pass before rejection.

3. Super-admin can ban cross-guild by manual slug.

   Given:
   - `cats` exists in guild `111`;
   - command context guild is `222`;
   - caller is in `settings.local_community_operator_allowlist`.

   Then:
   - result is applied;
   - active ban row is created.

4. Normal unrelated user in the same guild is rejected by management
   precondition.

   Given:
   - `cats` exists in current guild;
   - owner is `owner-1`;
   - caller is `user-2`;
   - caller is not super-admin.

   Then:
   - response is `You are not allowed to manage this local community.`;
   - no ban row is created.

5. Inactive community is unknown/inaccessible for owner and super-admin.

   Given:
   - `cats` exists with `status="inactive"`.

   Then:
   - owner is rejected with unknown/inaccessible;
   - super-admin is rejected with unknown/inaccessible;
   - no ban row is created.

6. Legacy NULL-owner community is manageable by super-admin only.

   Given:
   - `cats` exists in current guild;
   - `created_by_discord_user_id` is NULL;
   - status is active.

   Then:
   - normal caller is rejected;
   - super-admin can create the ban.

7. Unknown community rejects before handle validation.

   Given:
   - no `cats` community exists;
   - user input is an invalid handle.

   Then:
   - response is `Unknown or inaccessible local community: cats`;
   - no ban row is created.

8. Inaccessible community rejects before handle validation.

   Given:
   - `cats` exists in a different guild;
   - caller is the owner but command context is another guild;
   - user input is an invalid handle.

   Then:
   - response is unknown/inaccessible, not invalid-handle;
   - no ban row is created.

9. Authorized caller still gets invalid-handle errors.

   Given:
   - community is accessible and manageable;
   - user input is `@alice@example.com` or `https://example.com/u/alice`.

   Then:
   - response is `Invalid remote user handle. Use user@example.com.`;
   - no ban row is created.

10. Duplicate active ban behavior is unchanged.

    Given:
    - active ban exists for `alice@example.com` with reason `spam`;
    - caller is authorized.

    Then:
    - response includes the existing reason;
    - no second active row is created.

11. Re-ban after unban behavior is unchanged.

    Given:
    - inactive ban exists for `alice@example.com`;
    - caller is authorized;
    - new reason is `new spam`.

    Then:
    - inactive row is reactivated;
    - `reason` and `created_by_discord_user_id` update;
    - original `created_at` is preserved;
    - no duplicate inactive row is created.

### `/ban-user` command adapter and autocomplete scenarios

1. Command adapter passes `interaction.guild_id` to runtime policy.

   Given:
   - fake interaction has `guild_id=99999`;
   - community is owned by caller in guild `99999`.

   Then:
   - command succeeds and returns an ephemeral response.

2. Command adapter keeps responses ephemeral.

   Recheck both success and one rejection path.

3. Owner autocomplete returns owned active communities in current guild.

   Given:
   - owner has `cats` in guild `99999`;
   - owner has `dogs` in guild `11111`;
   - another user owns `birds` in guild `99999`;
   - `oldcats` is inactive.

   Then:
   - choices include `cats` only;
   - choices do not include `dogs`, `birds`, or `oldcats`.

4. Super-admin autocomplete returns active communities across all guilds.

   Given:
   - active communities exist in multiple guilds;
   - caller is super-admin;
   - one community is inactive.

   Then:
   - choices include active communities from all guilds;
   - choices exclude inactive communities;
   - labels include enough guild context to distinguish cross-guild entries;
   - values are slugs.

5. Autocomplete filters by typed `current` text.

   Given:
   - communities with slug/display names `cats`, `dogs`, `bird-watch`.

   Then:
   - `current="cat"` returns only matching slug or display-name choices.

6. Autocomplete returns at most 25 choices.

   Given:
   - more than 25 matching communities.

   Then:
   - exactly 25 choices are returned.

7. Autocomplete handles repository failures safely.

   Given:
   - repository raises an exception.

   Then:
   - autocomplete returns `[]`;
   - exception is logged.

8. Guildless owner autocomplete returns empty.

   Given:
   - non-super-admin caller;
   - `interaction.guild_id is None`.

   Then:
   - autocomplete returns `[]`.

9. Guildless super-admin autocomplete returns active communities across guilds.

   Given:
   - super-admin caller;
   - `interaction.guild_id is None`.

   Then:
   - autocomplete returns active communities across guilds.

### Regression tests

Run existing moderation tests to ensure this plan does not regress the behavior
introduced in plans 72, 73, and 74:

```text
tests/behavior/test_local_community_user_ban_scenarios.py
tests/commands/test_unban_user_command.py
tests/commands/test_list_banned_users_command.py
tests/operations/test_unban_user_operation.py
tests/operations/test_list_banned_users_operation.py
```

## Regression / Blind-Spot Analysis

### Autocomplete must not become authorization

Autocomplete narrows choices, but users can still type raw slugs or call command
callbacks directly in tests. The runtime operation must enforce all ownership,
super-admin, active-status, and guild-boundary rules.

Protection: operation tests must cover manual cross-guild slugs, missing guild
context, unrelated users, and inactive communities.

### Information leakage through precondition order

If handle validation or duplicate-ban lookup runs before community access, an
unauthorized caller could distinguish invalid handles or existing bans in an
inaccessible community.

Protection: tests must assert unknown/inaccessible is returned before
invalid-handle for inaccessible communities.

### Super-admin cross-guild labels

Super-admin autocomplete can show communities from every guild. The submitted
value stays the community slug because `local_communities.slug` is already
globally unique in the current schema. Do not introduce `guild_id:slug` values
in this plan.

Labels should still include guild context for super-admin global choices so an
operator can understand where the community lives before selecting it.

Protection: autocomplete tests must check labels include guild context for
super-admin global choices while submitted values remain plain slugs.

### Inactive community lifecycle is not implemented

This plan treats inactive communities as inaccessible. It does not define
community disable/archive semantics beyond command access.

Protection: inactive community behavior remains future work and must not be
interpreted as a full disable/archive implementation.

### Future-tasks drift

`dev/future_tasks.md` can become misleading if it still says moderation command
autocomplete is pending after this plan.

Protection: update the journal in the same change that implements this plan.

## Open Questions

None. The relevant policy choices are inherited from plans 73 and 74:

- owner autocomplete is current-guild scoped;
- super-admin autocomplete can be cross-guild;
- runtime preconditions are the security boundary;
- `/ban-user` has no user autocomplete;
- dynamic Discord command visibility remains future work.
