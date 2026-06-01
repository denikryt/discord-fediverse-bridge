# 77 — Edit local community lifecycle status

## Problem / Goal

The bridge can create, edit, and moderate bridge-owned local communities, but a
community owner still has no stop switch. A local community that should no
longer accept content or subscriptions remains operational unless the database
is edited manually.

The goal of this plan is to extend the existing `/edit-community` flow so the
same owner/super-admin management UI can change the local community lifecycle
status:

```text
/edit-community community:<local-community-slug>
```

The command continues to open a Discord modal. The modal should now contain:

```text
Display name      text input
Summary           text input
Status            select dropdown: active | disabled
```

`status=disabled` is a local operational freeze, not a federated delete, not a
tombstone, and not a subscriber cleanup. Disabled communities remain readable
through existing HTTP routes and keep existing database state, but they do not
accept new inbound ActivityPub side effects, Discord-originated content changes,
local subscriptions, or moderation-command changes until re-enabled through
`/edit-community`.

This plan intentionally keeps v1 local-only:

- no outbound ActivityPub `Delete`, `Update`, `Reject`, or `Undo`;
- no new receipt/outcome taxonomy such as `ignored_by_disabled_community`;
- no audit log or disabled reason;
- no automatic Discord message/thread changes;
- no subscriber row cleanup.

Future federation behavior should be added behind a narrow lifecycle decision
helper so this v1 local skip behavior can evolve without rewriting every
handler.

## Expected Behavior

### `/edit-community` modal

The existing command remains:

```text
/edit-community community:<slug>
```

It remains guild-only. DM invocation returns:

```text
This command can only be used inside a guild.
```

The slash-command handler must still open the modal as the initial interaction
response:

```python
await interaction.response.send_modal(modal)
```

It must not call `defer()` before `send_modal()`, and it must not try to open
the modal from a follow-up message.

The modal must be prefilled from the selected community row:

- `Display name` default is current `local_communities.display_name`;
- `Summary` default is current `local_communities.summary` or empty string when
  `NULL`;
- `Status` default is current `local_communities.status`.

Modal submit overwrites all three editable fields:

- display name;
- summary;
- status.

Saving unchanged values is a successful update. V1 keeps last-write-wins
semantics: if two valid modal submits happen sequentially, the later submit is
the current state.

### Status select

The status control must be a Discord select dropdown inside the modal:

```text
active
inactive is not valid
archived is not valid
disabled
```

Use the current Discord modal/select support available to the project. Discord
API supports select menus in modals via Label components. If the installed
`discord.py` high-level API cannot express select-in-modal directly, do not
replace the select with a free-text status field. Instead, implement the status
select through the supported library path available in this project, or stop and
report the missing library capability before changing behavior.

The submitted value must be one of:

```text
active
disabled
```

Invalid or missing modal status values are rejected ephemerally and do not
mutate the database.

### Success response

The modal submit response is ephemeral and always includes the final saved
state:

```text
Updated community cats.
Display name: Cats
Summary: Cat discussion
Status: disabled
New posts, comments, follows, and subscriptions are now blocked.
```

If the community is active:

```text
Updated community cats.
Display name: Cats
Summary: not specified
Status: active
New posts, comments, follows, and subscriptions are now allowed.
```

The lifecycle note is shown even when only metadata changed, because it confirms
the effective operational state after save.

### Autocomplete

`/edit-community community:` must show both `active` and `disabled` communities
so disabled communities can be re-enabled.

Policy:

- owner/non-super-admin callers see communities they own in the current guild,
  regardless of whether status is `active` or `disabled`;
- super-admin callers see all `active` and `disabled` communities across all
  guilds;
- submitted values remain stable globally unique community slugs;
- labels include slug and display name;
- labels for super-admin cross-guild choices include guild id context;
- labels for disabled communities should include status context, for example
  `cats — Cats — disabled` or `cats — Cats — guild 123 — disabled`;
- autocomplete returns at most 25 choices;
- autocomplete failures are logged and return an empty list.

Other management/moderation command autocompletes remain active-only:

```text
/ban-user
/unban-user
/list-banned-users
```

Disabled communities must not appear in those autocomplete lists.

### Runtime authorization

Runtime preconditions remain the security boundary. Autocomplete is only UX.

`/edit-community` keeps the existing owner/super-admin model:

- owner can edit their own community from that community's guild;
- super-admin can edit any community from any guild context;
- unrelated users are rejected;
- DM invocation is rejected.

Authorization must still run twice:

1. before showing the modal, so unauthorized callers do not see existing
   metadata or status;
2. on modal submit, so stale modal submissions cannot mutate state after
   permissions or community status have changed.

Unlike plan 76, `disabled` communities are accessible to `/edit-community` so
owners and super-admins can re-enable them.

### Disabled community behavior — inbound ActivityPub

When an inbound ActivityPub event targets a disabled local community, Python
must ACK and skip it before domain side effects.

Applies to all inbound local-community event types:

- remote post create;
- remote comment create;
- remote post update;
- remote comment update;
- remote post delete;
- remote comment delete;
- remote Follow;
- remote Undo(Follow);
- any other supported inbound event type whose target local community is known.

For disabled communities, inbound handling must:

- not create or update local-community thread/message rows;
- not create or update Discord surfaces;
- not edit or delete existing Discord messages;
- not create `RemoteSubscriber` rows;
- not send `Accept(Follow)`;
- not mutate existing subscriber rows;
- not create new relay/fanout state;
- not perform Discord fanout;
- not call outbound federation clients for disabled-community rejection in v1.

Use the existing skipped result/receipt path with detail/log reason. Do not add
new receipt status values such as:

```text
ignored_by_disabled_community
```

Reasoning: the ban implementation already avoided adding `ignored_by_ban`.
Disabled-community skip should follow the same rule. A dedicated outcome/status
taxonomy remains future work.

Log one INFO record with enough context to diagnose the skip without logging raw
ActivityPub payloads:

```text
Skipping inbound ActivityPub activity for disabled local community
community=<slug-or-actor-url> event_type=<type> delivery_id=<id>
```

### Disabled community behavior — Discord-originated local content

When a Discord event targets a disabled local community, it must be rejected or
ignored before local/federated side effects.

Disabled communities block Discord-originated:

- new posts/thread starters;
- new comments/messages;
- edits of existing local-community content;
- deletes of existing local-community content.

Expected runtime result details should be stable enough for tests, for example:

```text
community is disabled
```

Existing Discord threads/messages must not be deleted, archived, edited, or
annotated automatically when the community becomes disabled.

### Disabled community behavior — local subscriber subscription

Local same-bridge subscription/follow to a disabled local community is rejected
by the local subscribe operation/command with:

```text
Community cats is disabled and no longer available.
```

No `LocalSubscriber` row is created for the failed subscription.

### Disabled community behavior — moderation commands

The disabled community is closed to moderation commands except `/edit-community`.

For manual slug invocation against a disabled community:

```text
/ban-user
/unban-user
/list-banned-users
```

return:

```text
Community cats is disabled. Use /edit-community to re-enable it first.
```

No ban rows are created, deactivated, listed, or otherwise mutated by those
commands while the community is disabled.

### Existing state and re-enable behavior

Disabling a community does not destroy state:

- existing remote subscriber rows remain unchanged;
- existing local subscriber rows remain unchanged;
- existing local content rows remain unchanged;
- existing Discord surfaces remain unchanged;
- existing ban rows remain unchanged.

After `status` returns to `active`, existing subscriber rows participate in
fanout again according to current fanout logic. No resubscription is required.

### HTTP routes and dashboard

Disabled communities remain readable:

- actor routes remain readable;
- object/content routes remain readable;
- routes return current DB state;
- disabled does not mean `Delete`, `Tombstone`, `404`, or `410 Gone`.

If dashboard/public UI already reads `local_communities.status`, make sure
`disabled` does not break rendering. Do not add new dashboard UI in this plan;
that remains future work.

## Architecture

### Data model

Use the existing field:

```python
LocalCommunity.status
```

Allowed lifecycle values for local communities become:

```text
active
disabled
```

Do not add in this plan:

- `disabled_reason`;
- `disabled_at`;
- separate lifecycle/audit table;
- ownership transfer fields;
- new receipt status fields.

If existing model docs describe status as active-only or creation-only, update
them to state that `status` is the local lifecycle gate for management and event
processing.

### Repository

Extend `src/db/repositories/local_communities.py` so metadata and status can be
updated together.

Suggested method replacement or extension:

```python
update_local_community_settings(
    *,
    local_community_id: int,
    display_name: str,
    summary: str | None,
    status: str,
) -> LocalCommunity | None
```

Responsibilities:

- load by primary key;
- update only `display_name`, `summary`, and `status`;
- reject or avoid invalid status values before persistence;
- flush and return the updated row;
- return `None` if the row no longer exists.

Keep the older `update_local_community_metadata` only if needed for
compatibility, but `/edit-community` should use the status-aware method.

Add list methods for autocomplete that include disabled communities:

```python
list_manageable_local_communities()
list_manageable_local_communities_owned_by_user_in_guild(...)
```

or equivalent names. They should include rows whose status is `active` or
`disabled`, but exclude any future non-manageable status if such a status is
introduced later.

Existing active-only list methods must stay active-only because ban/unban/list
autocomplete should not show disabled communities.

### Lifecycle policy helper

Add a narrow helper module rather than scattering `status == "disabled"` checks
through handlers.

Suggested file:

```text
src/local_community_lifecycle.py
```

Suggested functions/classes:

```python
@dataclass(frozen=True, slots=True)
class LocalCommunityLifecycleDecision:
    allowed: bool
    reason: str
    detail: str


def evaluate_local_community_lifecycle(local_community: object) -> LocalCommunityLifecycleDecision:
    ...


def is_local_community_disabled(local_community: object) -> bool:
    ...
```

V1 decisions:

```text
active   -> allowed
missing/unknown status -> allowed only if existing code depends on legacy rows, or treat as disabled if the project already validates status strictly
disabled -> not allowed, reason="community_disabled", detail="community is disabled"
```

Prefer strict validation in edit operations and permissive handling of legacy
unknown statuses only if needed by existing data. The implementation should not
invent more lifecycle values.

This helper is the future extension point for federated responses. Future work
can map `community_disabled` to a Lemmy-compatible Reject/Update/Delete path
without changing every call site.

### Edit-community operation

Update `src/operations/edit_community.py`:

- `EditCommunityInput` gains `status: str`;
- operation caches/normalizes status similarly to display name and summary;
- preconditions validate guild context, community existence/access, owner or
  super-admin permission, display name, summary, and status;
- disabled communities are allowed through this operation, unlike moderation
  commands;
- body updates display name, summary, and status atomically in one repository
  call;
- result message always includes display name, summary, status, and lifecycle
  note.

Validation messages:

```text
Community status must be active or disabled.
```

Keep existing display-name and summary validation messages from plan 76.

### Edit-community Discord command and modal

Update `src/commands/edit_community.py`:

- autocomplete uses manageable active+disabled community lists;
- modal is prefilled with current status;
- modal contains a status select dropdown with values `active` and `disabled`;
- modal submit passes the selected status into `EditCommunityInput`;
- modal submit re-runs authorization and validation before mutating state;
- modal submit response remains ephemeral.

If implementing status select requires raw/modal component support not currently
wrapped by `discord.py`, keep the adapter code narrow and documented. Do not
turn status into a free-text field.

### Inbound ActivityPub lifecycle check

Integrate disabled-community decision after normal parse/idempotency and before
any domain mutation or fanout, matching the existing ban check placement.

Likely touched file:

```text
src/activitypub_handlers.py
```

Expected flow:

```text
parse event
receipt/idempotency gate accepts delivery
resolve target local community if possible
check local community disabled
if disabled: INFO log + HandlerResult(status="skipped", detail="community is disabled")
else: continue existing ban check and event dispatch
```

Ordering relative to ban check should be explicit. Recommended order:

1. resolve target local community;
2. disabled-community lifecycle check;
3. banned-actor check;
4. existing event dispatch.

This makes disabled community a broader lifecycle gate. If the local community
cannot be resolved, keep existing behavior; do not invent a global disabled
state.

### Local-community runtime gates

Update `src/local_communities/runtime.py` and closely related helpers so disabled
communities block Discord-originated side effects:

- host forum thread starter;
- host forum message/comment;
- local subscriber forum thread starter;
- local subscriber forum message/comment;
- Discord-originated edit;
- Discord-originated delete.

The check should happen after resolving the relevant local community but before:

- calling `ContentPublishService`;
- creating canonical content rows;
- creating Discord surface rows;
- creating relay rows;
- delivering fanout;
- editing/deleting Discord messages for mirrored content.

Where a runtime method returns `LocalCommunityRuntimeResult`, return a stable
ignored/skipped result such as:

```text
status="ignored", reason="community_disabled"
```

or the existing closest convention, with detail/reason `community is disabled`.
Tests should assert observable no-side-effect behavior rather than only the
exact internal reason string.

### Local subscriber subscribe gate

Update `src/operations/subscribe_local_community.py` to reject disabled target
communities after confirming the local community exists and before duplicate or
row creation preconditions that would obscure the disabled-community reason.

Expected message:

```text
Community cats is disabled and no longer available.
```

Use `local_community_name` from the command context where appropriate, but make
sure tests cover the exact message for a slug/display label such as `cats`.

### Moderation commands disabled gate

Update these operations and/or shared preconditions:

```text
src/operations/ban_user.py
src/operations/unban_user.py
src/operations/list_banned_users.py
```

Disabled-community behavior:

- community exists but `status == "disabled"` returns:

  ```text
  Community cats is disabled. Use /edit-community to re-enable it first.
  ```

- no mutation occurs;
- `/list-banned-users` does not list rows for disabled communities;
- autocomplete for these commands remains active-only and therefore normally
  hides disabled communities.

Place this precondition after community existence/access checks and before
actor-handle validation or ban-row mutation.

### Fanout guard

Disabled communities should not create new fanout while disabled. Most of this
is achieved by blocking local/remote inputs before domain side effects. Add a
small guard in fanout paths only if tests show that existing queued or direct
fanout can still run for a disabled community without a fresh input event.

Do not delete queued rows in this plan. If existing pending relay rows need a
policy, record it as future work unless current tests expose unsafe behavior.

## Touched Files

```text
src/models.py
src/activitypub_handlers.py
src/commands/edit_community.py
src/operations/edit_community.py
src/operations/subscribe_local_community.py
src/operations/ban_user.py
src/operations/unban_user.py
src/operations/list_banned_users.py
src/db/repositories/local_communities.py
src/local_communities/runtime.py
src/local_communities/discord_fanout.py
src/local_community_lifecycle.py
src/local_community_permissions.py
docs/architecture/database-map.md
docs/development/navigation.md
docs/architecture/event-flows.md
dev/future_tasks.md
tests/behavior/test_local_community_disabled_scenarios.py
tests/commands/test_edit_community_command.py
tests/operations/test_edit_community_operation.py
tests/operations/test_subscribe_operation.py
tests/operations/test_ban_user_operation.py
tests/operations/test_unban_user_operation.py
tests/operations/test_list_banned_users_operation.py
```

Some paths may not need changes if the existing implementation centralizes the
behavior elsewhere, but implementation must update every affected runtime path.

## New Files

```text
plans/77_edit_community_lifecycle_status.md
src/local_community_lifecycle.py
tests/behavior/test_local_community_disabled_scenarios.py
```

## Implementation Steps

1. Write failing observable-behavior tests first.

   Start with `/edit-community` status tests, then disabled side-effect tests.
   Each scenario must define the initial DB state, the user action, and the
   final observable result: command response, DB rows, gateway calls, Discord
   fake calls, and fanout absence.

2. Add lifecycle helper module.

   Implement `src/local_community_lifecycle.py` with documented helpers for
   `active` and `disabled` decisions. Use this helper in all gates added by this
   plan.

3. Extend local-community repository methods.

   Add status-aware update and manageable-list methods. Preserve active-only
   list methods for moderation autocompletes.

4. Extend `/edit-community` operation.

   Add status input, validation, persistence, and success-message formatting.
   Ensure disabled communities are editable and re-enableable through this
   operation.

5. Extend `/edit-community` Discord modal.

   Add status select dropdown with `active` and `disabled`. Prefill current
   status. Keep `send_modal()` as initial response and re-run authorization on
   submit.

6. Add disabled gate to inbound ActivityPub dispatch.

   Resolve the target local community, check lifecycle, and return the existing
   skipped path with detail/log reason before any mutation or fanout. Do not add
   a new receipt status.

7. Add disabled gates to Discord-originated local-community runtime paths.

   Block create/edit/delete paths before publishing, persistence, relay, or
   Discord fanout side effects.

8. Add disabled gate to local subscriber subscribe operation.

   Return the exact disabled-unavailable error and do not create rows.

9. Add disabled preconditions to ban/unban/list-banned-users.

   Return the exact re-enable-first error and do not mutate or list ban rows.

10. Check dashboard and HTTP route behavior.

   Ensure readable routes do not break for `status="disabled"`. Do not add a
   new dashboard UI unless existing output already exposes status and needs a
   narrow compatibility fix.

11. Update documentation and future tasks.

   Update database/event/navigation docs where they own the concept. Add future
   tasks for federated disabled-community behavior, subscriber cleanup policy,
   disabled-community dashboard UI, and receipt/outcome taxonomy if not already
   present.

12. Run focused tests, then full suite.

   Run new disabled-community tests first, then existing local-community inbound,
   publish, edit/delete, moderation command, subscription, dashboard, and full
   suite tests.

## Tests

Use observable behavior tests. Unit tests are allowed only where they make a
small helper easier to validate, but they are not a substitute for runtime
scenario coverage.

### `/edit-community` status tests

1. Owner disables own active community from its guild.

   Given:
   - local community `cats` exists in guild `100`;
   - `created_by_discord_user_id="owner"`;
   - status is `active`.

   When:
   - owner opens `/edit-community community:cats`;
   - modal is prefilled;
   - owner submits same display name/summary and status `disabled`.

   Then:
   - response is ephemeral;
   - DB row has `status="disabled"`;
   - display name and summary are preserved;
   - response includes display name, summary, status, and blocked lifecycle note.

2. Owner re-enables own disabled community.

   Assertions:
   - status changes to `active`;
   - existing subscriber rows remain unchanged;
   - response includes allowed lifecycle note.

3. Owner edits metadata and status in the same modal submit.

   Assertions:
   - display name, summary, and status all update in one operation;
   - empty summary stores `NULL`;
   - unchanged fields still succeed.

4. Super-admin edits status cross-guild.

   Assertions:
   - super-admin can disable or enable a community from a different guild
     context;
   - super-admin autocomplete includes all active and disabled communities
     across guilds.

5. Non-owner cannot open modal or submit stale modal.

   Assertions:
   - pre-open path rejects unauthorized caller;
   - submit path also rejects unauthorized caller;
   - DB is unchanged.

6. Invalid status is rejected.

   Assertions:
   - `archived`, `inactive`, empty, or missing values fail;
   - DB is unchanged;
   - response says `Community status must be active or disabled.`

7. `/edit-community` autocomplete includes disabled communities.

   Assertions:
   - owner sees owned active and disabled communities in current guild;
   - owner does not see disabled communities from another guild;
   - super-admin sees active and disabled communities across guilds;
   - disabled labels include status context;
   - choices are capped at 25.

### Inbound ActivityPub disabled tests

1. Disabled community remote post create is ACKed/skipped.

   Assertions:
   - receipt follows existing skipped/result path;
   - no `LocalCommunityThread` row is created;
   - no Discord thread/message fake receives calls;
   - no relay/fanout rows are created;
   - INFO log includes disabled-community skip context.

2. Disabled community remote comment create is ACKed/skipped.

   Assertions:
   - no message row;
   - no Discord fanout;
   - no relay rows.

3. Disabled community remote update/delete are ACKed/skipped.

   Assertions:
   - existing Discord surfaces are not edited or deleted;
   - local content rows are not mutated;
   - result uses existing skipped path with disabled detail/log reason.

4. Disabled community remote Follow is ACKed/skipped.

   Assertions:
   - no `RemoteSubscriber` row is created;
   - no `Accept(Follow)` is sent;
   - no federated Reject is sent in v1.

5. Disabled community remote Undo(Follow) is ACKed/skipped.

   Assertions:
   - existing subscriber rows remain unchanged.

6. Duplicate delivery of skipped disabled event remains idempotent.

   Assertions:
   - first delivery records through existing receipt behavior;
   - second delivery is treated as duplicate;
   - no side effects on either delivery.

### Discord-originated disabled tests

1. Disabled host forum thread starter is blocked.

   Assertions:
   - no publish call to Fedify gateway;
   - no local-community thread row;
   - no surface row;
   - no fanout.

2. Disabled host forum comment is blocked.

   Assertions:
   - no publish call;
   - no message row;
   - no fanout.

3. Disabled local-subscriber forum post/comment is blocked.

   Assertions:
   - no canonical content rows;
   - no surface rows;
   - no relay/fanout rows.

4. Disabled Discord-originated edit/delete are blocked.

   Assertions:
   - no outbound update/delete publish call;
   - no DB mutation for local content;
   - no Discord edit/delete fanout;
   - existing Discord messages remain untouched.

5. Re-enabled community resumes existing subscriber fanout.

   Assertions:
   - existing local/remote subscriber rows created before disable remain;
   - after setting status back to active, new content follows the normal path
     and fanout happens to existing subscribers.

### Local subscribe disabled tests

1. Local subscriber subscribe to disabled community fails.

   Assertions:
   - response is `Community cats is disabled and no longer available.`;
   - no `LocalSubscriber` row is created.

2. Active community subscribe behavior is unchanged.

   Assertions:
   - existing happy-path local subscribe tests still pass.

### Moderation command disabled tests

For `/ban-user`, `/unban-user`, and `/list-banned-users`:

1. Manual slug against disabled community returns:

   ```text
   Community cats is disabled. Use /edit-community to re-enable it first.
   ```

2. No ban row is created, deactivated, or listed.

3. Disabled communities do not appear in moderation command autocomplete.

4. Active community behavior remains unchanged.

### Route/dashboard regression tests

1. Actor route for disabled community remains readable.

2. Existing content/object route for disabled community remains readable.

3. Dashboard/public UI tests pass with a disabled local community present; if the
   dashboard already exposes status, expected output includes or tolerates
   `disabled` without adding a new UI feature.

### Focused existing tests to run

Run at least:

```bash
./.venv/bin/pytest -q \
  tests/commands/test_edit_community_command.py \
  tests/operations/test_edit_community_operation.py \
  tests/behavior/test_local_community_disabled_scenarios.py
```

Then run local-community hot paths:

```bash
./.venv/bin/pytest -q \
  tests/behavior/test_local_community_inbound_scenarios.py \
  tests/behavior/test_local_community_publish_scenarios.py \
  tests/behavior/test_local_community_edit_delete_scenarios.py \
  tests/behavior/test_local_community_remote_fanout_scenarios.py \
  tests/behavior/test_local_subscriber_stage1_scenarios.py \
  tests/behavior/test_local_community_stage3_local_subscriber_sync_scenarios.py \
  tests/behavior/test_local_community_stage4_local_subscriber_origin_scenarios.py \
  tests/behavior/test_local_community_stage5_participant_edit_delete_scenarios.py
```

Then run moderation/subscription/dashboard tests:

```bash
./.venv/bin/pytest -q \
  tests/commands/test_ban_user_command.py \
  tests/commands/test_unban_user_command.py \
  tests/commands/test_list_banned_users_command.py \
  tests/operations/test_ban_user_operation.py \
  tests/operations/test_unban_user_operation.py \
  tests/operations/test_list_banned_users_operation.py \
  tests/operations/test_subscribe_operation.py \
  tests/behavior/test_dashboard_scenarios.py
```

Finally run:

```bash
./.venv/bin/pytest -q
```

## Regression / Blind-Spot Analysis

### Accidental deletion semantics

Disabled must not behave like delete/archive/tombstone. Existing routes,
content rows, subscribers, and Discord surfaces must remain intact.

Protection: route/readability tests and assertions that no existing rows or
Discord surfaces are deleted/edited when disabling.

### Incomplete hot-path gating

It is easy to block inbound Create but forget Update/Delete, Follow, local
subscriber fanout, or Discord-originated edit/delete.

Protection: tests must cover every supported event family and both host and
local-subscriber Discord-origin paths.

### Fanout after disabled

Existing queued or direct fanout could still deliver if disable is checked only
at command boundaries.

Protection: side-effect tests assert no publish/gateway/fanout calls and no new
relay rows for disabled communities.

### Re-enable fails to restore normal behavior

If disable mutates subscribers or content rows, re-enable may not restore fanout.

Protection: existing subscriber rows must remain unchanged and re-enable tests
must verify normal fanout resumes.

### Moderation command inconsistency

Autocomplete may hide disabled communities, but manual slug invocation can still
reach operations.

Protection: runtime preconditions for ban/unban/list must reject disabled
communities with the exact re-enable-first message.

### Receipt taxonomy creep

Adding `ignored_by_disabled_community` in this plan would duplicate a broader
future observability redesign.

Protection: plan explicitly uses existing skipped path with detail/log reason
and records outcome taxonomy as future work.

### Discord modal select support

Discord API supports select menus in modals, but the installed `discord.py`
version must expose a safe implementation path.

Protection: implementation must not fall back to free-text status; if library
support is missing, stop and report the missing technical capability before
changing the plan.

## Future Work / Explicitly Out of Scope

These items must be recorded or updated in `dev/future_tasks.md` if not already
present:

1. Federated disabled-community behavior.

   Research and implement Lemmy-compatible outbound behavior for disabled local
   communities. Open questions include whether remote Follow should receive
   Reject, whether community metadata should emit Update, and whether disabled
   should ever federate Delete/Tombstone-like objects.

2. Receipt/outcome taxonomy.

   Design explicit status categories such as:

   ```text
   ignored_by_ban
   ignored_by_disabled_community
   ignored_unknown_subscription
   ignored_unmapped_context
   ```

   This should be a separate observability/receipt semantics plan.

3. Subscriber cleanup/deactivation on disable.

   Decide whether disabled communities should eventually mark existing
   subscribers inactive, preserve rows with disabled fanout, notify subscribers,
   or require resubscription after re-enable.

4. Dashboard disabled-community UI.

   Decide how disabled communities should appear in any public or operator UI.

5. Audit log for lifecycle changes.

   Track who disabled/re-enabled a community, when, and why if there is a real
   operator requirement. This plan does not add `disabled_reason`.

6. Richer lifecycle states.

   Consider `archived`, `deleted`, or `read_only` only in a later plan. V1 uses
   only `active` and `disabled`.

## Open Questions

None. The interactive planning decisions for v1 are fixed in this document.
