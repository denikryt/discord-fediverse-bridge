# 76 — Edit local community metadata v1

## Problem / Goal

The bridge can create Discord-backed local communities and now has an owner/super-admin management model for moderation commands, but community metadata cannot be edited after creation. A community owner who needs to correct a display name or clear/update the description currently has to edit the database manually.

The goal of this plan is to add a small owner-scoped local metadata editor:

```text
/edit-community community:<local-community-slug>
```

The slash command opens a Discord modal prefilled with the current community metadata. Submitting the modal overwrites the stored `display_name` and `summary` values. This is a local database update only. It does not rename the slug, move the Discord forum binding, change visibility/status, transfer ownership, or emit a federated ActivityPub `Update`.

This plan also changes community summary semantics: `summary` becomes optional/nullable. `/create_community` should allow an omitted or blank description, and `/edit-community` should allow clearing the summary.

## Expected Behavior

### Command shape

Add a new slash command:

```text
/edit-community community:<slug>
```

The command must be usable only inside a Discord guild. DM invocation is rejected with:

```text
This command can only be used inside a guild.
```

The `community` argument is the stable globally unique local-community slug. It must not accept a `guild_id:slug` composite value in v1.

The command response itself should open a Discord modal rather than immediately editing fields through slash command arguments.

### Community autocomplete

`/edit-community community:` must use the same management-command autocomplete policy established by moderation commands:

- owner/non-super-admin callers see active local communities they own in the current guild;
- super-admin callers see all active local communities across all guilds;
- submitted values are stable community slugs;
- labels should include slug and display name;
- labels for super-admin cross-guild choices should include guild id context;
- autocomplete returns at most 25 choices;
- autocomplete failures are logged and return an empty list.

Example labels:

```text
cats — Cats
cats — Cats — guild 1234567890
```

### Authorization and guild scope

The command uses runtime preconditions as the security boundary. Autocomplete is UX only.

Runtime behavior:

- owner may edit their own active community only when invoked from that community's Discord guild;
- super-admin may edit any active community from any guild context, but the command must still be invoked inside a guild;
- unrelated users cannot edit communities;
- inactive communities are inaccessible in v1;
- unknown or inaccessible communities return:

```text
Unknown or inaccessible local community: cats
```

If a community is accessible but the caller is not owner/super-admin, return:

```text
You are not allowed to manage this local community.
```

### Modal behavior

After preconditions pass, the command opens a Discord modal with two fields:

```text
Display name
Summary
```

The modal must follow the `discord.py` interaction flow:

- the slash-command handler must open the modal with `interaction.response.send_modal(...)`;
- the slash-command handler must not `defer()` before opening the modal;
- the modal must not be opened through a follow-up response;
- the modal should be implemented as a narrow `discord.ui.Modal` subclass;
- the modal should create/add `discord.ui.TextInput` fields dynamically in `__init__` so the `default` values can come from the selected community row;
- the modal submit handler must respond with an ephemeral message through the submit interaction.

Modal fields:

- `Display name` is required;
- `Display name` is prefilled with current `local_communities.display_name`;
- `Display name` is trimmed before validation and persistence;
- empty/whitespace-only `Display name` is rejected;
- `Display name` max length is 100 characters;
- `Summary` is optional;
- `Summary` is prefilled with current `local_communities.summary` or an empty string if it is `NULL`;
- `Summary` is trimmed before persistence;
- empty/whitespace-only `Summary` stores `NULL`;
- non-empty `Summary` max length is 1000 characters.

Submitting the modal overwrites both stored fields. If the user submits unchanged values, treat it as a successful update and return the saved values. V1 uses last-write-wins semantics: if two valid submits happen sequentially, the later submit becomes the current state. Do not add optimistic locking, stale-form detection, version columns, or conflict resolution.

### Success response

The modal submit response is ephemeral and includes the saved values:

```text
Updated community cats.
Display name: Cats
Summary: Cat discussion
```

If summary is cleared or absent:

```text
Updated community cats.
Display name: Cats
Summary: not specified
```

### Validation errors

Display-name validation failures must be returned ephemerally from modal submit and must not mutate the database.

Expected validation messages:

```text
Community display name is required.
```

```text
Community display name must be 100 characters or fewer.
```

```text
Community summary must be 1000 characters or fewer.
```

If the community disappears, becomes inactive, or becomes inaccessible before modal submit, the submit path must re-check access and return the same runtime-access errors rather than assuming the initial slash-command check is sufficient. The slash command and the modal submit are separate interactions, so authorization must run twice: once before showing the modal to avoid exposing current metadata to unauthorized callers, and again on submit before mutating the database.

### Create-community summary behavior

`/create_community description` becomes optional.

Expected create behavior:

- omitted description stores `NULL`;
- blank/whitespace-only description stores `NULL`;
- non-empty description is trimmed and stored;
- description max length is 1000 characters;
- existing slug/name/channel/allowlist behavior does not change.

The create-community success message does not need to include the summary.

### Local-only edit

This plan does not send any outbound ActivityPub `Update`, does not notify remote subscribers, and does not mutate Fedify gateway behavior.

Actor/document routes and dashboard payloads that read directly from Python DB should naturally reflect the updated data on future reads. If any route assumes `summary` is non-null, update it to serialize `NULL` safely.

## Architecture

### Data model

Change `LocalCommunity.summary` from required to optional:

```python
summary: Mapped[str | None] = mapped_column(String, nullable=True)
```

No new `updated_at` or audit columns are added in this plan. The model already has `updated_at`; if SQLAlchemy updates it through normal ORM `onupdate`, that is acceptable, but this plan must not add a new edit-specific metadata field.

### Migration

Existing SQLite databases may have `local_communities.summary` as `NOT NULL`. SQLite cannot simply `ALTER COLUMN` to drop `NOT NULL`, so migration must be explicit and tested.

Implement a migration helper in `src/db/migrations.py` that:

1. detects whether `local_communities.summary` is still `NOT NULL` via `PRAGMA table_info(local_communities)`;
2. if it is already nullable, does nothing;
3. if it is non-null, rebuilds `local_communities` with the same columns/constraints but `summary` nullable;
4. copies existing rows unchanged;
5. preserves unique constraints on `discord_forum_channel_id`, `slug`, and `actor_url`;
6. remains idempotent.

This migration should be narrow and heavily commented because table rebuild migrations are more fragile than additive column migrations.

### Repository

Extend `src/db/repositories/local_communities.py` with an update method:

```python
update_local_community_metadata(
    *,
    local_community_id: int,
    display_name: str,
    summary: str | None,
) -> LocalCommunity | None
```

Responsibilities:

- load by primary key;
- update only `display_name` and `summary`;
- flush and return the updated row;
- return `None` if the row no longer exists.

Also update `create_local_community` to accept `summary: str | None`.

### Shared validation

Add narrow validation helpers rather than duplicating rules in create and edit operations. A suitable location is `src/local_communities/service.py` because this module already owns local-community validation.

Suggested functions or static methods:

```python
normalize_display_name(value: str) -> str
normalize_summary(value: str | None) -> str | None
```

Rules:

- display name: trim, require non-empty, max 100;
- summary: treat `None` as `None`, trim strings, empty -> `None`, max 1000.

Keep error messages stable because tests assert observable command behavior.

### Create-community operation and command

Update:

```text
src/commands/create_community.py
src/operations/create_community.py
src/local_communities/service.py
```

`CreateCommunityInput.description` becomes `str | None`.

The Discord command argument should become optional:

```python
description: str | None = None
```

Keep the command in guild-only mode and keep existing create permission behavior. Do not convert `/create_community` to `discordops` in this plan unless implementation of optional description requires a very small local cleanup.

### Edit-community operation

Add:

```text
src/operations/edit_community.py
```

Suggested input/result shapes:

```python
@dataclass(slots=True)
class EditCommunityInput:
    database: Database
    settings: Settings
    discord_user_id: str
    discord_guild_id: int | None
    community_slug: str
    display_name: str
    summary: str | None

@dataclass(slots=True)
class EditCommunityResult:
    applied: bool
    message: str
    reason: str
```

Implement as a `discordops.Operation` with ordered preconditions:

1. guild context exists;
2. community exists and is accessible from the guild context;
3. caller can manage the community;
4. display name is valid;
5. summary is valid.

The operation must re-load and re-check access on modal submit. Do not rely only on the slash-command precheck used before opening the modal.

Expected rejection reasons:

```text
missing_guild_context
unknown_or_inaccessible_community
cannot_manage_community
invalid_display_name
invalid_summary
invalid_operation_state
```

The operation body calls `update_local_community_metadata` and returns the success response with saved values.

### Edit-community command adapter

Add:

```text
src/commands/edit_community.py
```

Register it in:

```text
src/commands/__init__.py
src/discord_bot.py
src/operations/__init__.py
```

Command flow:

1. slash command receives `community` slug;
2. command rejects DM invocation before opening modal;
3. command loads/checks enough state to show the modal only for accessible/manageable active communities;
4. command opens a `discord.ui.Modal` with prefilled current values by calling `await interaction.response.send_modal(modal)`;
5. command must not call `interaction.response.defer()` before `send_modal`;
6. modal `on_submit` calls `edit_community_operation` using current `interaction.user.id`, `interaction.guild_id`, the original slug, and submitted values;
7. modal sends an ephemeral response.

The modal class should be narrow and testable. Avoid putting persistence rules directly in the UI class; it should delegate to the operation. Use dynamic `discord.ui.TextInput` construction in the modal initializer so `default=community.display_name` and `default=community.summary or ""` come from the selected row.

### Future tasks journal

Update `dev/future_tasks.md`:

- mark `/edit-community` metadata v1 as planned/implemented once appropriate;
- add/keep a future task for federated community metadata `Update` after local edits;
- add future task for richer community settings: visibility/subscription policy/status/disable/archive;
- add future task for edit audit/version/conflict handling only if the implementation discovers a real need.

## Touched Files

```text
src/models.py
src/db/migrations.py
src/db/repositories/local_communities.py
src/local_communities/service.py
src/commands/__init__.py
src/commands/create_community.py
src/discord_bot.py
src/operations/__init__.py
src/operations/create_community.py
src/dashboard.py
src/community_discovery.py
dev/future_tasks.md
docs/architecture/database-map.md
docs/development/navigation.md
tests/commands/test_create_community_command.py
tests/commands/test_edit_community_command.py
tests/operations/test_edit_community_operation.py
tests/behavior/test_local_community_edit_metadata_scenarios.py
tests/behavior/test_dashboard_scenarios.py
tests/behavior/test_local_community_discovery_scenarios.py
```

The exact test file split may vary, but observable behavior coverage must exist for command/modal flow, operation preconditions, create optional summary, nullable migration, and downstream reads that expose community metadata.

## New Files

```text
plans/76_edit_local_community.md
src/commands/edit_community.py
src/operations/edit_community.py
tests/commands/test_edit_community_command.py
tests/operations/test_edit_community_operation.py
tests/behavior/test_local_community_edit_metadata_scenarios.py
```

## Implementation Steps

1. Write failing observable-behavior tests first.

   Start with scenarios that describe the full user action:

   ```text
   given a local community owned by user 123 in guild 999
   when user 123 invokes /edit-community community:cats and submits modal values
   then the command responds ephemerally and local_communities has the new display_name/summary
   ```

   Also write create-community tests for optional/blank descriptions and migration tests for nullable `summary` before changing implementation.

2. Make `summary` nullable in the model and migration.

   Update `LocalCommunity.summary` typing and add the SQLite table-rebuild migration if existing schema has `summary NOT NULL`. Test migration idempotency and preservation of unique constraints.

3. Add shared display-name and summary normalization.

   Put validation in the local-community domain layer so create and edit use the same limits and trimming behavior.

4. Update create-community flow.

   Make command `description` optional, update operation input typing, and update service/repository to persist `None` for omitted or blank summaries.

5. Add repository metadata update method.

   Implement and test `update_local_community_metadata` through operation behavior, not only as an isolated method.

6. Add `EditCommunityOperation` using `discordops`.

   Follow the same ordered-precondition style as ban/unban/list management operations. Re-check guild context, accessibility, management permission, and validation on modal submit.

7. Add Discord command adapter and modal.

   Register `/edit-community`, add community autocomplete, prefill modal fields, and make submit delegate to the operation. Open the modal with `interaction.response.send_modal(...)` as the initial slash-command response; do not defer before sending the modal and do not use follow-up responses to open it.

8. Update metadata readers for nullable summary.

   Check dashboard, community discovery, and any actor-route/gateway-facing payload builder for assumptions that summary is always a string. Adjust serialization so `NULL` is safe and intentional.

9. Update documentation and future tasks.

   Update database map, development navigation, and `dev/future_tasks.md`. Do not document outbound federation because v1 explicitly does not implement it except as future work.

10. Run focused tests, then the full suite.

   Run command/operation tests for create/edit/community management first, then dashboard/discovery/local-community behavior tests, then the full suite.

## Tests

Use TDD and observable behavior as the default. Unit tests are acceptable only for narrow validation helpers or migration helpers where scenario coverage would be awkward.

### Create-community optional summary scenarios

1. Super-admin creates a community without `description`.

   Given:
   - caller is in `local_community_operator_allowlist`;
   - guild and forum channel are valid;
   - slug is unused.

   When:
   - `/create_community slug:cats name:Cats channel:<forum>` is invoked with no description.

   Then:
   - command succeeds;
   - response behavior matches current create command policy;
   - `local_communities.summary IS NULL`;
   - `created_by_discord_user_id` is still populated;
   - actor URLs/keypair behavior is unchanged.

2. Super-admin creates a community with whitespace-only description.

   Then:
   - command succeeds;
   - summary is stored as `NULL`.

3. Super-admin creates a community with non-empty description.

   Then:
   - description is trimmed;
   - summary stores the trimmed value.

4. Description longer than 1000 characters is rejected.

   Then:
   - no community row is created;
   - response is ephemeral failure;
   - error message is stable.

5. Existing create-community failures still work.

   Cover at least: non-allowlisted caller, duplicate slug, duplicate forum channel, invalid slug, empty name.

### Edit-community command/modal scenarios

1. Owner opens modal for owned current-guild community.

   Given:
   - active community `cats` in guild `999`;
   - `created_by_discord_user_id="123"`;
   - current display name `Cats` and summary `Old summary`.

   When:
   - user `123` invokes `/edit-community community:cats` in guild `999`.

   Then:
   - command opens a modal through the initial interaction response;
   - command does not defer before opening the modal;
   - modal display-name field is prefilled with `Cats`;
   - modal summary field is prefilled with `Old summary`.

2. Owner submits changed display name and summary.

   Then:
   - response is ephemeral;
   - `display_name` and `summary` are updated in DB;
   - success response shows the saved values.

3. Owner clears summary.

   Then:
   - DB stores `summary=NULL`;
   - success response says `Summary: not specified`.

4. Owner submits unchanged values.

   Then:
   - command succeeds;
   - DB remains equivalent;
   - response shows current values.

5. Empty/whitespace-only display name is rejected.

   Then:
   - DB does not change;
   - response is ephemeral;
   - message is `Community display name is required.`

6. Too-long display name is rejected.

   Then:
   - DB does not change;
   - message is `Community display name must be 100 characters or fewer.`

7. Too-long summary is rejected.

   Then:
   - DB does not change;
   - message is `Community summary must be 1000 characters or fewer.`

8. Non-owner cannot open or submit edit.

   Given:
   - community owner is `123`;
   - caller is `456` and not super-admin.

   Then:
   - slash command does not open modal, or modal submit recheck rejects if directly exercised;
   - DB does not change;
   - message is `You are not allowed to manage this local community.`

9. Owner cannot edit same-owned community from another guild.

   Then:
   - community is treated as unknown/inaccessible;
   - DB does not change.

10. Super-admin can edit any active community from a guild context.

    Then:
    - cross-guild manual slug works;
    - DB updates.

11. DM invocation is rejected.

    Then:
    - no modal is opened;
    - DB does not change;
    - message is `This command can only be used inside a guild.`

12. Unknown slug is rejected.

    Then:
    - no modal is opened;
    - DB does not change;
    - message is `Unknown or inaccessible local community: <slug>`.

13. Inactive community is rejected.

    Then:
    - no modal is opened;
    - DB does not change.

14. Community disappears or becomes inaccessible before modal submit.

    Then:
    - submit path rejects through operation preconditions;
    - DB does not change.

### Autocomplete scenarios

1. Owner sees owned active communities in the current guild.
2. Owner does not see owned communities from another guild.
3. Owner does not see communities owned by another user.
4. Super-admin sees active communities across all guilds with guild context in labels.
5. Inactive communities are not listed.
6. Typed text filters by slug or display name.
7. Results are capped at 25 choices.
8. Repository errors return an empty list and log an exception.

### Nullable summary migration scenarios

1. Existing database with `summary NOT NULL` migrates to nullable summary.
2. Existing rows and values are preserved.
3. Unique constraints on `slug`, `actor_url`, and `discord_forum_channel_id` still prevent duplicates after migration.
4. Running migration twice is safe.
5. Fresh database created from models has nullable summary without migration rewrite.

### Metadata read regression scenarios

1. Dashboard payload handles `summary=NULL` without crashing.
2. Dashboard output for a cleared summary is either `null` or the existing documented empty/description behavior; tests must assert the chosen observable payload.
3. Community discovery/search does not crash when summary is `NULL`.
4. Gateway-facing local actor data, if read through Python DB in tests, serializes missing summary safely.

### Focused tests to run

Run focused tests first:

```bash
./.venv/bin/pytest -q \
  tests/commands/test_create_community_command.py \
  tests/commands/test_edit_community_command.py \
  tests/operations/test_edit_community_operation.py \
  tests/behavior/test_local_community_edit_metadata_scenarios.py
```

Run metadata/read-model regressions:

```bash
./.venv/bin/pytest -q \
  tests/behavior/test_dashboard_scenarios.py \
  tests/behavior/test_local_community_discovery_scenarios.py
```

Run existing management command tests because autocomplete and permission policy should stay aligned:

```bash
./.venv/bin/pytest -q \
  tests/commands/test_ban_user_command.py \
  tests/commands/test_unban_user_command.py \
  tests/commands/test_list_banned_users_command.py
```

Then run the full suite:

```bash
./.venv/bin/pytest -q
```

## Regression / Blind-Spot Analysis

### Summary nullability can break readers

Several existing paths assume `community.summary` is a string. Changing it to nullable can break dashboard, discovery, or gateway-facing payloads.

Protection: add regression tests for nullable summary reads and update serialization intentionally.

### Slash-command precheck is not enough

A user could open a modal and submit later. The submit path must re-run permission and validation preconditions.

Protection: modal submit delegates to `EditCommunityOperation`; tests directly exercise submit-time rejection.

### Accidental slug editing

Changing slug would affect actor URLs, WebFinger/discovery, command values, mappings, and federated identity.

Protection: v1 has no slug field in modal and no repository method for slug update.

### Accidental federation claims

Local DB edits may change what the bridge returns on future reads, but they do not notify remote servers.

Protection: plan explicitly excludes outbound ActivityPub `Update` and records it as future work.

### Over-broad community autocomplete

Autocomplete should not expose unrelated owner choices, but it is not the security boundary.

Protection: tests cover autocomplete visibility and runtime preconditions reject manual inaccessible slugs.

### Create-community behavior drift

Making description optional must not weaken slug/name/channel/allowlist rules.

Protection: regression tests cover existing create-community failure cases.

### Table rebuild migration risk

SQLite table rebuilds can accidentally drop constraints, columns, or data.

Protection: migration tests assert preserved values and unique constraints after migration.

## Future Work / Explicitly Out of Scope

1. Federated ActivityPub `Update` for changed community metadata.

   A later plan should research the correct ActivityPub/Lemmy-compatible shape for community actor metadata updates and delivery to followers before implementing outbound federation.

2. Editing slug/actor URL.

   Out of scope because slug is a stable route, handle, actor URL component, and command value.

3. Editing status/disable/archive.

   This needs separate lifecycle semantics for inbound events, outbound fanout, dashboard visibility, and actor/object routes.

4. Editing visibility/subscription policy.

   This belongs with private/public communities and manual subscription approval.

5. Ownership transfer, moderators, and role-system integration.

   This belongs to the broader role-system future work.

6. Audit log and version/conflict tracking.

   V1 uses last-write-wins. A later audit model can record edit history if operator needs justify it.

7. Dashboard editing UI.

   V1 is Discord-command-only.

## Open Questions

None. Product decisions fixed for v1:

- edit only `display_name` and `summary`;
- use Discord modal with prefilled fields;
- summary is nullable and optional on create/edit;
- empty summary stores `NULL`;
- empty display name is rejected;
- guild invocation required;
- owner current-guild and super-admin all-guild autocomplete policy;
- local-only edit, federated metadata updates deferred.
