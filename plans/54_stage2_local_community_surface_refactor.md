# 54 — Stage 2 local community surface refactor

## Problem / Goal

Stage 1 introduced explicit participant naming and control-plane persistence:

- `RemoteSubscriber` / `remote_subscribers`
- `LocalSubscriber` / `local_subscribers`

That was necessary, but it did not change the local-community content model.
The current runtime still assumes that each canonical local-community activity
has exactly one Discord surface:

- `LocalCommunityThread.discord_thread_id`
- `LocalCommunityThread.discord_starter_message_id`
- `LocalCommunityMessage.discord_message_id`
- `LocalCommunityMessage.parent_discord_message_id`

That assumption still leaks through the active code:

- `LocalCommunityRuntime` creates exactly one `LocalCommunityThread` row per
  post and one `LocalCommunityMessage` row per comment;
- `src/local_communities/delivery_mapping.py` resolves canonical ownership by
  looking up one Discord thread/message id directly on those rows;
- `src/local_communities/reply_mapping.py` resolves outbound and inbound reply
  context against that same single-surface mapping;
- `DiscordEventRouter.is_local_community_message()` decides whether an edit or
  delete belongs to local-community mode by checking those single ids.

This shape is fine for the current host-forum-only runtime, but it blocks the
next step from `51_local_community_channel_subscriptions.md`: a local
community post/comment must eventually exist on several Discord surfaces at
once:

- the host forum surface;
- zero or more local subscriber forum surfaces.

Stage 2 should refactor the persistence and lookup model so the system can
represent several Discord surfaces for one canonical local-community
post/comment while preserving all currently supported host-forum behavior.

Stage 2 is still not the stage that makes local subscriber forums active
sources or local fanout targets. It only prepares the canonical/surface data
model required for later stages.

## Stage Boundary

### Stage 2 owns

Stage 2 owns only the canonical/surface refactor:

- keep `LocalCommunityThread` and `LocalCommunityMessage` as canonical
  community-activity rows;
- remove direct Discord surface ownership from those canonical rows;
- add per-surface Discord mapping tables for local-community threads/messages;
- move runtime lookups, reply resolution, and edit/delete ownership checks onto
  the new surface tables;
- backfill existing databases so current local-community history remains
  addressable through the new schema;
- keep current host-forum behavior green through the refactor.

### Stage 2 does not own

Stage 2 must not introduce new participant-sync semantics:

- no host forum -> local subscriber fanout;
- no remote follower -> local subscriber fanout;
- no local subscriber forum -> host/remote publish path;
- no `DiscordEventRouter` acceptance of local subscriber forums as valid local
  community sources;
- no changes to `LocalCommunityFederationFanout` target set;
- no participant-wide edit/delete propagation beyond the current host-only
  model.

### Stage 2 handoff contract

Stage 2 is complete only when these are all true:

- the runtime still behaves exactly as today for host-forum-created content,
  inbound remote follower content, and existing edit/delete paths;
- canonical local-community rows no longer assume a single Discord surface;
- every existing canonical thread/message row has at least one surface row
  representing the host-forum mapping;
- later stages can add more local Discord surfaces without another destructive
  schema rewrite.

This boundary is intentionally sufficient for the current codebase. The active
write paths already converge on one host Discord surface only:

- outbound local posts/comments are created from
  `LocalCommunity.discord_forum_channel_id`;
- inbound remote follower posts/comments are delivered only into that same host
  forum;
- edit/delete ownership checks only need to recognize the existing host starter
  and reply messages.

Because of that, Stage 2 can be implemented as a pure storage-and-lookup
refactor:

- backfill one `"host"` thread surface for every existing canonical thread;
- backfill one `"host"` message surface for every existing canonical message;
- move runtime/router/reply lookups to those host surfaces;
- leave participant fanout and local-subscriber source routing entirely for
  later stages.

If implementation appears to require host -> local-subscriber fanout or
local-subscriber-originated routing just to finish Stage 2, that means the code
change is crossing the intended stage boundary and should be pushed into the
later participant-sync stage instead of being folded into this one.

## Expected Behavior

### Existing host-forum create behavior remains unchanged

When a user creates a thread in the host forum:

```text
Discord host forum thread starter
  -> publish one AP Create as today
  -> create one canonical LocalCommunityThread row
  -> create one LocalCommunityThreadSurface row for the host forum thread
```

When a user replies in the host forum thread:

```text
Discord host forum reply
  -> publish one AP Create as today
  -> create one canonical LocalCommunityMessage row
  -> create one LocalCommunityMessageSurface row for the host forum message
```

User-visible behavior does not change in Stage 2. The difference is only that
Discord ids move from canonical rows into surface rows.

### Existing inbound remote follower behavior remains unchanged

When an accepted remote follower posts into a local community:

```text
remote follower post
  -> create one canonical LocalCommunityThread row
  -> create one LocalCommunityThreadSurface row for the host forum thread
  -> continue remote relay behavior as today
```

When an accepted remote follower comments into a local community:

```text
remote follower comment
  -> create one canonical LocalCommunityMessage row
  -> create one LocalCommunityMessageSurface row for the host forum message
  -> continue remote relay behavior as today
```

Again, Stage 2 changes storage shape only. It does not add new Discord targets.

### Reply mapping still resolves against the visible host-forum surface

Outbound host-forum replies must still:

- target the thread root AP object when replying to the starter message or root;
- target the parent AP comment object when replying to a mapped local message.

Inbound remote replies must still:

- reference the host thread starter message for root replies;
- reference the host-f orum mapped reply message for nested replies.

The difference is that reply resolution must read those Discord ids from host
surface rows, not directly from canonical rows.

### Edit/delete ownership still works for current host-forum surfaces

The router and local-community runtime must still be able to decide:

- whether one Discord message belongs to local-community mode;
- whether that message is a starter/root surface or a comment surface;
- which canonical AP object to update/delete.

After the refactor those checks must flow through the new surface tables
instead of direct ids on `local_community_threads` / `local_community_messages`.

### No new local-subscriber routing appears yet

If a `LocalSubscriber` row exists in Stage 2:

- no local thread/message surfaces should be created for it yet;
- no local fanout should target it yet;
- Discord events from that subscriber forum should still be ignored by the
  local-community runtime.

Stage 2 only ensures the storage model can express those future surfaces.

## Expected Conflicts / Compatibility Risks

### Canonical-row lookups are currently coupled to Discord ids

The largest compatibility risk is that several active code paths still assume
Discord ids live directly on canonical rows:

- `DiscordEventRouter.is_local_community_message()`
  reads `get_local_community_thread_by_starter_message_id()` and
  `get_local_community_message_by_discord_message_id()`;
- `LocalCommunityRuntime.handle_discord_message()` checks
  `thread_row.discord_starter_message_id`;
- `LocalCommunityRuntime.handle_inbound_comment()` fetches the target thread
  from `thread_row.discord_thread_id`;
- inbound update/delete paths still send edits/deletes to
  `thread_row.discord_thread_id`, `thread_row.discord_starter_message_id`, and
  `message_row.discord_message_id`;
- `reply_mapping.resolve_outbound_reply_context()` and
  `resolve_inbound_reply_target()` still treat canonical rows as the place where
  Discord parent ids live.

Stage 2 must switch all of those paths together. A partial switch would leave
some code reading surface rows while other code still expects canonical rows to
own the same ids, which would create split-brain routing bugs.

### SQLite migration order is correctness-critical

`Database.migrate()` currently owns schema evolution for this project. Stage 2
requires a multi-step migration:

1. create surface tables;
2. backfill host surfaces from existing canonical rows;
3. switch runtime/repository reads to the new tables;
4. rebuild canonical tables without the old Discord-id columns.

If those steps happen in the wrong order, the code can temporarily boot against
an incomplete schema and either:

- fail lookups for existing local-community history;
- duplicate host surfaces on repeated startup;
- lose starter/reply message ownership for edit/delete.

The migration therefore has to be idempotent and must tolerate databases that
have:

- only old canonical tables;
- old canonical tables plus already-created surface tables from an interrupted
  deployment;
- partially backfilled surface rows.

### Host-only runtime behavior is easy to widen accidentally

The Stage 2 refactor touches the same modules that will later be used for
participant-wide sync:

- `src/local_communities/runtime.py`
- `src/local_communities/delivery_mapping.py`
- `src/local_communities/reply_mapping.py`
- `src/discord_event_router.py`

That creates a real compatibility risk: while switching to surface helpers, it
would be easy to also start consulting `local_subscribers` and accidentally
change runtime behavior in the same patch.

Stage 2 must keep a strict rule:

- surface tables are introduced now;
- local subscriber rows remain unused by runtime routing now.

### Backfilled host surfaces must preserve current published-object contracts

Local-community update/delete behavior depends on stable resolution from Discord
message ids back to canonical AP ids through:

- `PublishedActivityObject`
- local-community thread/message mappings
- local-community router ownership checks

If the Stage 2 backfill creates host surface rows that point at the wrong
canonical rows, update/delete paths can silently target the wrong AP object or
stop recognizing messages as local-community-owned.

### Gateway compatibility should remain unchanged

Stage 2 does not change gateway-visible contracts, but the local-community
runtime still produces the AP activity/object ids that gateway tests verify.
The compatibility risk is indirect:

- if Stage 2 accidentally changes the canonical AP ids or origin semantics
  produced by local-community runtime paths,
- gateway outbound and publish-contract verification can fail even though no
  gateway source files were modified.

## Required Mitigations

### Switch read paths in one bounded refactor, not piecemeal

To avoid split-brain lookup behavior, Stage 2 should change these code paths in
one bounded refactor before old canonical Discord-id helpers are removed:

- `src/local_communities/delivery_mapping.py`
- `src/local_communities/reply_mapping.py`
- `src/local_communities/runtime.py`
- `src/discord_event_router.py`

Concrete rule:

- once one runtime/router path starts reading surface rows for Discord ids,
  every other local-community Discord-id lookup path in the same stage must do
  the same;
- no patch in Stage 2 should leave some edit/delete/reply paths reading
  canonical Discord columns while others read surface rows.

### Backfill surfaces before any runtime code depends on them

`Database.migrate()` must create and backfill host surfaces before Stage 2 code
starts using surface-based lookups in production.

Concrete requirements:

1. create `local_community_thread_surfaces` and
   `local_community_message_surfaces`;
2. backfill one `"host"` thread surface for every canonical thread row;
3. backfill one `"host"` message surface for every canonical message row;
4. only after that switch runtime/repository lookups to the new tables;
5. only after the lookup switch is complete rebuild canonical tables to remove
   old Discord-id columns.

That ordering prevents a deploy where new code boots against a DB that still
lacks the host surfaces it now expects.

### Make backfill idempotent and validate its invariants

The migration must treat partial/interrupted deployments as a first-class case.

Concrete requirements:

- use uniqueness constraints so repeated surface inserts become safe or
  naturally no-op;
- when a canonical thread row already has a host surface, do not create a
  second one;
- when a canonical message row already has a host surface, do not create a
  second one;
- verify after migrate that every canonical thread has exactly one host surface
  in Stage 2;
- verify after migrate that every canonical message has exactly one host
  surface in Stage 2.

If a row violates those invariants, `migrate()` should fail loudly instead of
continuing with ambiguous ownership.

### Preserve starter-vs-reply ownership explicitly

The migration and runtime rewrite must preserve the current distinction between:

- thread root starter message;
- nested reply message.

Concrete requirements:

- each host thread surface must persist `discord_starter_message_id`;
- each host message surface must persist `parent_discord_message_id`;
- `resolve_inbound_reply_target()` must read starter ids from host thread
  surfaces and parent message ids from host message surfaces;
- `resolve_outbound_reply_context()` must reach canonical rows through the
  mapped message surface rather than old canonical Discord columns.

This is the specific guardrail that prevents root replies and nested replies
from collapsing into the same path.

### Keep local subscribers out of runtime on purpose

Stage 2 touches local-community routing code, so the plan must actively prevent
accidental Stage 3 behavior.

Concrete requirements:

- do not query `local_subscribers` inside:
  - `DiscordEventRouter.is_local_community_forum()`
  - `DiscordEventRouter.handle_thread_create()`
  - `DiscordEventRouter.handle_message()`
  - `LocalCommunityRuntime.handle_discord_thread_create()`
  - `LocalCommunityRuntime.handle_discord_message()`
- do not create any `role="local_subscriber"` surface rows in Stage 2;
- do not add any fanout loop over `list_local_subscribers(...)` in Stage 2.

That makes the stage boundary enforceable in code review.

### Add negative assertions, not only happy-path assertions

To avoid the blind spots in this refactor, tests must prove both what still
works and what still must not happen.

Concrete requirements:

- host-forum create/reply tests must assert exactly one host surface row, not
  just “a surface row exists”;
- migration tests must assert that re-running `Database.migrate()` does not
  create duplicate surface rows;
- runtime tests with existing `LocalSubscriber` rows must assert that no local
  subscriber surfaces are created yet;
- router tests must assert that Discord events from local subscriber forums are
  still ignored in Stage 2.

### Keep published-object and remote-relay contracts unchanged

Stage 2 should treat canonical AP ids as immutable.

Concrete requirements:

- do not regenerate `ap_activity_id` / `ap_object_id` during migration;
- do not derive new canonical AP ids from surface rows;
- keep `origin_kind` and `direction` semantics unchanged on canonical rows;
- verify in behavior tests that edit/delete and relay fanout still target the
  same AP ids as before the refactor.

This is the guardrail that keeps gateway checks green even though the gateway
itself is not changing.

## Regression / Blind-Spot Analysis

### Regression: starter message ownership can disappear

The easiest regression is losing the distinction between:

- a thread starter message that owns the post root surface;
- a normal reply message inside the thread.

Today that distinction is enforced by
`LocalCommunityThread.discord_starter_message_id`. After the refactor it must
be preserved by host thread-surface rows. If that mapping is wrong, these
behaviors can regress:

- outbound root replies target the wrong AP parent;
- inbound remote root replies attach to the wrong Discord message;
- edit/delete of the starter message no longer routes as a post update/delete.

### Regression: nested reply mapping can silently flatten

`resolve_outbound_reply_context()` and `resolve_inbound_reply_target()` currently
rely on one canonical Discord parent chain. After Stage 2 that parent linkage
becomes surface-local. A likely blind spot is preserving only the root/starter
mapping and forgetting nested parent message ids during backfill or runtime
writes.

Observable failure modes:

- nested Discord replies publish as top-level replies to the thread root;
- inbound remote nested replies appear as root replies in Discord;
- update/delete code still finds the message row, but the reconstructed reply
  chain is wrong.

### Regression: duplicate-suppression can weaken during the lookup switch

Current local-community duplicate checks are keyed off direct Discord-id
lookups:

- thread duplicate check by Discord thread id;
- message duplicate check by Discord message id;
- edit/delete ownership by starter/reply message id.

After the switch, those checks must continue to be one-to-one through surface
rows. If the plan leaves any old helper active in parallel with new surface
helpers, a duplicate event can slip through one path and be ignored by the
other.

### Regression: inbound remote comments can fail even when threads still map

Inbound remote comment delivery is a particularly fragile path because it needs
both:

- canonical thread lookup by AP post object id;
- host thread surface lookup by Discord thread id;
- host message surface lookup for nested reply references.

A partial migration can leave post-thread delivery working while nested comment
delivery breaks. That is an important blind spot because simple “post created”
checks would stay green while reply routing is already wrong.

### Blind spot: old databases vs fresh databases

Fresh databases created after Stage 2 and existing databases migrated into
Stage 2 can fail in different ways:

- fresh install path can miss host-surface creation because migration never ran;
- migrated path can carry stale canonical columns or partial backfill state.

The plan therefore needs both:

- fresh-install behavior tests;
- migration/backfill tests from pre-Stage-2 snapshots.

### Blind spot: runtime still ignores local subscriber forums by design

Because Stage 2 intentionally does not activate local subscriber forums, it is
easy for maintainers to misread a passing test suite as “local subscribers are
now wired into runtime”.

That would be a false signal. The test suite for this stage must explicitly
assert that:

- `LocalSubscriber` rows do not receive any thread/message surfaces yet;
- Discord events from local subscriber forums are still ignored.

Without those negative assertions, Stage 2 could accidentally drift toward
Stage 3 behavior without anyone noticing.

## Architecture

### Keep `LocalCommunityThread` and `LocalCommunityMessage` as canonical rows

Do not replace the canonical activity rows. Keep them, but narrow their
responsibility to the canonical community activity identity.

Current responsibility of `LocalCommunityThread` is mixed:

- canonical AP activity/object ids;
- canonical origin metadata;
- one Discord surface id pair.

Stage 2 should split those responsibilities.

#### `LocalCommunityThread`

After Stage 2, the canonical thread row should keep only community-level
identity:

```python
class LocalCommunityThread(Base):
    __tablename__ = "local_community_threads"
    __table_args__ = (
        UniqueConstraint("ap_activity_id"),
        UniqueConstraint("ap_object_id"),
    )

    id: Mapped[int]
    local_community_id: Mapped[int]
    ap_activity_id: Mapped[str]
    ap_object_id: Mapped[str]
    direction: Mapped[str]
    origin_kind: Mapped[str]
    created_at: Mapped[datetime]
```

Fields to remove from the canonical row:

- `discord_thread_id`
- `discord_starter_message_id`

#### `LocalCommunityMessage`

After Stage 2, the canonical message row should keep only community-level
identity and canonical reply linkage:

```python
class LocalCommunityMessage(Base):
    __tablename__ = "local_community_messages"
    __table_args__ = (
        UniqueConstraint("ap_activity_id"),
        UniqueConstraint("ap_object_id"),
    )

    id: Mapped[int]
    local_community_thread_id: Mapped[int]
    ap_activity_id: Mapped[str]
    ap_object_id: Mapped[str]
    parent_ap_object_id: Mapped[str | None]
    direction: Mapped[str]
    created_at: Mapped[datetime]
```

Fields to remove from the canonical row:

- `discord_message_id`
- `parent_discord_message_id`

`parent_discord_message_id` becomes a per-surface concern because different
local Discord surfaces will later have different parent message ids for the
same canonical comment.

### Add per-surface local Discord mapping tables

Stage 2 should introduce two new tables.

#### `local_community_thread_surfaces`

```python
class LocalCommunityThreadSurface(Base):
    """Map one canonical local-community thread into one Discord forum surface."""

    __tablename__ = "local_community_thread_surfaces"
    __table_args__ = (
        UniqueConstraint("local_community_thread_id", "discord_forum_channel_id"),
        UniqueConstraint("discord_thread_id"),
        UniqueConstraint("discord_starter_message_id"),
    )

    id: Mapped[int]
    local_community_thread_id: Mapped[int]
    discord_forum_channel_id: Mapped[int]
    discord_thread_id: Mapped[int]
    discord_starter_message_id: Mapped[int]
    role: Mapped[str]  # "host" in Stage 2; later "local_subscriber"
    created_at: Mapped[datetime]
```

#### `local_community_message_surfaces`

```python
class LocalCommunityMessageSurface(Base):
    """Map one canonical local-community comment into one Discord surface message."""

    __tablename__ = "local_community_message_surfaces"
    __table_args__ = (
        UniqueConstraint("local_community_message_id", "discord_forum_channel_id"),
        UniqueConstraint("discord_message_id"),
    )

    id: Mapped[int]
    local_community_message_id: Mapped[int]
    local_community_thread_surface_id: Mapped[int]
    discord_forum_channel_id: Mapped[int]
    discord_message_id: Mapped[int]
    parent_discord_message_id: Mapped[int | None]
    role: Mapped[str]  # "host" in Stage 2; later "local_subscriber"
    created_at: Mapped[datetime]
```

Why `local_community_thread_surface_id` should be stored on message surfaces:

- it keeps each message explicitly bound to the concrete Discord thread surface
  it was posted into;
- later stages can create several thread surfaces for one canonical thread and
  then several message surfaces under each one;
- reply/edit/delete routing can stay surface-local instead of inferring the
  thread from only the canonical message row.

### Prefer additive migration plus canonical-column cleanup in one stage

Because Stage 2 changes table responsibility, the migration should be explicit:

1. create `local_community_thread_surfaces`;
2. create `local_community_message_surfaces`;
3. backfill one host surface row for every existing canonical thread row;
4. backfill one host surface row for every existing canonical message row;
5. switch Python lookups/runtime to the new surface tables;
6. only then drop the old Discord-specific columns from canonical tables.

The codebase uses `Database.migrate()` rather than Alembic, so the migration
must be written as ordered SQLite-safe DDL/data-copy steps there.

For SQLite this likely means table rebuild rather than `DROP COLUMN`.

### Repository helpers should become surface-aware

`src/db.py` needs a clean split between canonical rows and surface rows.

Canonical helpers should stay close to current names:

```python
create_local_community_thread(...)
get_local_community_thread_by_ap_object_id(...)
get_local_community_thread_by_id(...)

create_local_community_message(...)
get_local_community_message_by_ap_object_id(...)
get_local_community_message_by_id(...)
```

Surface helpers should be added explicitly:

```python
create_local_community_thread_surface(...)
get_local_community_thread_surface_by_discord_thread_id(...)
get_local_community_thread_surface_by_starter_message_id(...)
list_local_community_thread_surfaces(local_community_thread_id)
get_host_local_community_thread_surface(local_community_thread_id)

create_local_community_message_surface(...)
get_local_community_message_surface_by_discord_message_id(...)
list_local_community_message_surfaces(local_community_message_id)
get_host_local_community_message_surface(local_community_message_id)
```

The old `get_local_community_thread_by_discord_thread_id()` and
`get_local_community_message_by_discord_message_id()` should not survive as
primary APIs after Stage 2. If compatibility wrappers are needed during the
transition, they should delegate to the new surface helpers and be deleted in a
follow-up cleanup before Stage 3.

### Runtime and router should keep host-only policy, but read through surfaces

#### `LocalCommunityRuntime`

`src/local_communities/runtime.py` should continue to treat only the host forum
as a valid local Discord source in Stage 2. The runtime changes are mechanical:

- `handle_discord_thread_create()`
  - create canonical thread row
  - then create host thread surface row
- `handle_discord_message()`
  - resolve canonical thread through thread surface lookup
  - create canonical message row
  - then create host message surface row
- `handle_inbound_post()`
  - create canonical thread row
  - create host thread surface row
- `handle_inbound_comment()`
  - resolve canonical thread by AP object id
  - resolve host target thread via host thread surface
  - create canonical message row
  - create host message surface row

The important constraint is that Stage 2 should not change which runtime paths
run, only which persistence rows they read and write.

#### `DiscordEventRouter`

`src/discord_event_router.py` should still use host-forum-only source routing:

- `is_local_community_forum()` can stay anchored to
  `LocalCommunity.discord_forum_channel_id`;
- `handle_thread_create()` and `handle_message()` should remain unchanged in
  policy.

But `is_local_community_message()` must stop reading canonical
`local_community_threads` / `local_community_messages` by direct Discord ids.
It should instead ask the new surface helpers:

```python
thread_surface = database.get_local_community_thread_surface_by_starter_message_id(message_id)
message_surface = database.get_local_community_message_surface_by_discord_message_id(message_id)
```

### Delivery/reply lookup helpers should shift from canonical ids to surfaces

`src/local_communities/delivery_mapping.py` currently exposes:

- `get_local_community_thread_for_discord_thread()`
- `get_local_community_message_for_discord_message()`

Stage 2 should split that helper layer:

- thread/message lookup by AP object id still returns canonical rows;
- thread/message lookup by Discord ids should return surface rows first, then
  canonical rows if needed through the foreign key.

Example shape:

```python
def get_local_community_thread_surface_for_discord_thread(...)
def get_local_community_message_surface_for_discord_message(...)
def get_canonical_thread_for_surface(...)
def get_canonical_message_for_surface(...)
```

`src/local_communities/reply_mapping.py` must then resolve:

- outbound parent AP context from canonical message rows reached via surface
  lookups;
- inbound parent Discord context from host message surface rows, not from
  `LocalCommunityMessage.parent_discord_message_id`.

## Touched Files

plans/51_local_community_channel_subscriptions.md
src/models.py
src/db.py
src/local_communities/runtime.py
src/local_communities/delivery_mapping.py
src/local_communities/reply_mapping.py
src/discord_event_router.py
tests/behavior/test_local_community_publish_scenarios.py
tests/behavior/test_local_community_inbound_scenarios.py
tests/behavior/test_local_community_edit_delete_scenarios.py
tests/behavior/test_local_community_remote_fanout_scenarios.py
docs/architecture/database-map.md
docs/architecture/event-flows.md
docs/development/navigation.md
src/local_communities/README.md

## New Files

plans/54_stage2_local_community_surface_refactor.md

## Implementation Steps

### 1. Freeze Stage 2 regression coverage around existing host-only behavior

Before refactoring, add or extend behavior tests that exercise:

- host forum thread create;
- host forum reply create;
- inbound remote follower post -> host forum thread;
- inbound remote follower comment -> host forum message;
- host-forum edit/delete of starter and reply messages.

These tests should assert observable effects through runtime paths:

- canonical rows still exist;
- the correct Discord thread/message ids are still reachable;
- relay fanout still sees the same canonical AP ids.

The point is to protect behavior while the storage shape changes under it.

### 2. Add new surface models without changing runtime yet

Update `src/models.py`:

- add `LocalCommunityThreadSurface`;
- add `LocalCommunityMessageSurface`;
- keep old Discord-id columns on canonical rows temporarily until migration and
  code switch are complete.

Update `Database.create_all()` path so new installs create the new tables.

### 3. Add `Database.migrate()` steps to backfill host surfaces

In `src/db.py`, extend `migrate()` with ordered steps:

1. create new surface tables if missing;
2. for every `local_community_threads` row, insert:

```text
local_community_thread_surfaces
  local_community_thread_id = thread.id
  discord_forum_channel_id = local_community.discord_forum_channel_id
  discord_thread_id = thread.discord_thread_id
  discord_starter_message_id = thread.discord_starter_message_id
  role = host
```

3. for every `local_community_messages` row, insert:

```text
local_community_message_surfaces
  local_community_message_id = message.id
  local_community_thread_surface_id = host thread surface id
  discord_forum_channel_id = local_community.discord_forum_channel_id
  discord_message_id = message.discord_message_id
  parent_discord_message_id = message.parent_discord_message_id
  role = host
```

Backfill must be idempotent. Re-running `migrate()` must not duplicate rows.

### 4. Add surface-aware repository helpers

Implement new `src/db.py` helper families for:

- create/get/list thread surfaces;
- create/get/list message surfaces;
- resolving canonical thread/message by surface ids.

Keep comments and docstrings explicit about Stage 2 constraints:

- only host surfaces exist in Stage 2;
- helper naming is intentionally future-proof for Stage 3+ local subscriber
  surfaces.

### 5. Switch local-community lookup helpers to surfaces

Refactor `src/local_communities/delivery_mapping.py` and
`src/local_communities/reply_mapping.py`:

- AP-object lookups still use canonical rows;
- Discord-id lookups go through surface rows;
- reply parent resolution reaches canonical rows through surface rows.

This step should remove runtime dependence on direct Discord ids in canonical
rows.

### 6. Switch `LocalCommunityRuntime` create/edit/delete paths to write/read surfaces

Refactor `src/local_communities/runtime.py`:

- create canonical rows first;
- create host surface rows immediately after;
- inbound comment posting resolves the target Discord thread through host thread
  surface;
- edit/delete helpers resolve canonical AP objects by surface rows.

No new participant fanout should appear here.

### 7. Switch `DiscordEventRouter` local message ownership checks to surfaces

Refactor `src/discord_event_router.py`:

- `is_local_community_message()` must query surface helpers, not canonical rows
  by Discord ids;
- thread/message create routing policy remains host-forum-only.

This preserves Stage 2 boundary while making edit/delete ownership future-safe.

### 8. Remove old Discord-id columns from canonical tables

Once all Python read/write paths use surface tables:

- rebuild `local_community_threads` without:
  - `discord_thread_id`
  - `discord_starter_message_id`
- rebuild `local_community_messages` without:
  - `discord_message_id`
  - `parent_discord_message_id`

Because SQLite has limited `ALTER TABLE` support, this likely means:

- create temp replacement table;
- copy canonical columns;
- drop old table;
- rename temp table.

Do this only after code no longer depends on the removed columns.

### 9. Update documentation to describe canonical rows vs surfaces

Update docs only where their scope matches:

- `docs/architecture/database-map.md`
  - show canonical rows plus surface rows separately;
- `docs/architecture/event-flows.md`
  - explain that host-forum behavior now writes both canonical and host-surface
    rows;
- `docs/development/navigation.md`
  - point maintainers to surface lookup helpers;
- `src/local_communities/README.md`
  - describe Stage 2 storage shape and its boundary.

## Tests

### Python behavior/regression

Add or update runtime scenarios so they assert:

1. Host forum thread create:
   - canonical `LocalCommunityThread` row exists;
   - exactly one host `LocalCommunityThreadSurface` row exists;
   - no extra local subscriber surfaces exist.

2. Host forum reply create:
   - canonical `LocalCommunityMessage` row exists;
   - exactly one host `LocalCommunityMessageSurface` row exists;
   - reply AP parent resolution still matches current behavior.

3. Inbound remote follower post/comment:
   - canonical rows exist;
   - host surface rows exist;
   - remote relay behavior remains unchanged.

4. Discord edit/delete:
   - router still recognizes host starter/reply messages as local-community
     messages through surface lookups;
   - update/delete publish path still resolves canonical AP object ids.

### Migration tests

Add database-level tests that start from a pre-Stage-2 SQLite schema snapshot:

- old `local_community_threads` / `local_community_messages` with embedded
  Discord ids;
- run `Database.migrate()`;
- assert backfilled surface rows exist;
- assert re-running migrate is idempotent.

### Full regression

Run at minimum:

```bash
./.venv/bin/pytest -q
cd fedify-gateway && npm run check
cd fedify-gateway && npm test
```

Gateway behavior should stay green because Stage 2 does not change gateway
contracts.

## Implementation Decisions

### Switch call sites fully inside Stage 2

Stage 2 should switch all local-community call sites to surface-aware helpers
in the same stage and remove the old Discord-id canonical lookup helpers before
Stage 2 is considered complete.

Short-lived internal wrappers are acceptable only during the refactor itself if
they keep the migration readable, but they must not remain as supported
repository APIs after the stage lands.

This is an intentional boundary cleanup:

- `get_local_community_thread_by_discord_thread_id()`
- `get_local_community_thread_by_starter_message_id()`
- `get_local_community_message_by_discord_message_id()`

describe the pre-Stage-2 storage model and should not survive as primary
interfaces once Discord-id lookups are surface-based.

### Store `role` on surfaces in Stage 2

`role` should be stored on surface rows already in Stage 2 even though only
`"host"` exists at first.

That keeps the schema future-proof for later local-subscriber surfaces and
avoids another migration just to distinguish host vs local-subscriber surface
rows.

## Open Questions

None at this stage.
