# 55 — Stage 3 host/remote to local-subscriber sync

## Problem / Goal

Stage 1 introduced explicit participant control-plane state:

- `RemoteSubscriber` / `remote_subscribers` for remote ActivityPub actors;
- `LocalSubscriber` / `local_subscribers` for same-instance Discord forum subscribers.

Stage 2 refactored local-community content persistence so one canonical
`LocalCommunityThread` or `LocalCommunityMessage` can have explicit Discord
surface rows instead of embedding exactly one Discord thread/message id on the
canonical row.

Stage 3 should use that prepared model for the first runtime-visible local
subscriber behavior:

```text
host forum activity or inbound remote subscriber activity
  -> existing host forum behavior remains unchanged
  -> existing remote federation behavior remains unchanged
  -> additionally create Discord surfaces in active local subscriber forums
```

Stage 3 is intentionally one-way for local subscribers. A local subscriber forum
is a synchronized read surface in this stage, not a source of canonical
community activity yet. Local-subscriber-originated posts/comments belong to
Stage 4. Participant-wide edit/delete propagation belongs to Stage 5.

This stage closes the gap between Stage 2's surface model and Stage 4's local
subscriber source routing: after Stage 3, the database can contain real
`role="local_subscriber"` surfaces created by runtime fanout, and later stages
can safely route source events and edits/deletes through those surfaces without
another storage redesign.

## Stage Boundary

### Stage 3 owns

Stage 3 owns create-time local Discord fanout into active local subscriber
forums for content whose source is already supported:

- host forum post create -> local subscriber thread surfaces;
- host forum comment create -> local subscriber message surfaces;
- inbound remote subscriber post create -> local subscriber thread surfaces;
- inbound remote subscriber comment create -> local subscriber message surfaces;
- idempotent retry of missing local subscriber surfaces when the same source
  event is processed again;
- per-target partial failure handling so one failed local subscriber forum does
  not block the host surface, other local subscriber surfaces, or remote relay;
- explicit negative behavior for local subscriber forums as sources.

### Stage 3 does not own

Stage 3 must not introduce these later-stage behaviors:

- no Discord thread/message created in a local subscriber forum becomes a
  canonical local-community post/comment source;
- no `DiscordEventRouter` source routing from local subscriber forums into
  `LocalCommunityRuntime.handle_discord_thread_create()` or
  `LocalCommunityRuntime.handle_discord_message()`;
- no ActivityPub publish from local subscriber forum source events;
- no participant-wide edit/delete propagation across local subscriber surfaces;
- no retroactive backfill of old community history into newly subscribed local
  forums;
- no changes to remote `BridgeActorFollow` or remote community subscription
  semantics.

### Stage 3 handoff contract

Stage 3 is complete only when these are all true:

- active local subscribers receive new host-originated posts/comments;
- active local subscribers receive new inbound remote-originated posts/comments;
- every created local subscriber copy is represented by an explicit surface row;
- duplicate source processing creates only missing surfaces and does not create
  duplicate canonical rows or duplicate surfaces;
- local subscriber forum source events remain non-sources by design;
- Stage 4 can make local subscriber forums valid sources by reusing the same
  surface rows and fanout helper instead of changing the schema again.

## Expected Behavior

### Host forum post creates local subscriber thread surfaces

Given:

```text
LocalCommunity(id=7, discord_forum_channel_id=100)
LocalSubscriber(local_community_id=7, discord_channel_id=200, status="active")
LocalSubscriber(local_community_id=7, discord_channel_id=300, status="active")
```

When a registered Discord user creates a thread in host forum `100`:

```text
host forum thread starter
  -> existing AP publish through publish_local_community_content
  -> one canonical LocalCommunityThread row
  -> one host LocalCommunityThreadSurface row
  -> one local-subscriber LocalCommunityThreadSurface row for forum 200
  -> one local-subscriber LocalCommunityThreadSurface row for forum 300
```

Expected row shape for the subscriber surface:

```text
local_community_thread_surfaces
  local_community_thread_id = <canonical thread id>
  discord_forum_channel_id = 200
  discord_thread_id = <created subscriber thread id>
  discord_starter_message_id = <created subscriber starter message id>
  role = local_subscriber
  local_subscriber_id = <LocalSubscriber.id>
```

The host forum must not receive a second copy of its own source thread.

### Host forum comment creates local subscriber message surfaces

When a registered user replies in a host forum thread that already has local
subscriber thread surfaces:

```text
host forum reply
  -> existing AP publish through publish_local_community_content
  -> one canonical LocalCommunityMessage row
  -> one host LocalCommunityMessageSurface row
  -> one local-subscriber LocalCommunityMessageSurface under every existing
     local-subscriber thread surface for the canonical thread
```

For root replies, the local subscriber message should reply to that target
surface's starter message. For nested replies, the local subscriber message
should reply to the matching parent comment surface in the same target thread.
If the target parent comment surface is missing, skip that target instead of
flattening the reply to the thread root.

### Inbound remote post creates host and local subscriber surfaces

When an accepted remote subscriber posts into a local community:

```text
remote subscriber post
  -> existing host forum thread creation
  -> existing canonical LocalCommunityThread row
  -> existing host LocalCommunityThreadSurface row
  -> local subscriber thread surfaces for active local subscribers
  -> existing remote relay fanout to other remote subscribers
```

Local Discord fanout must not prevent remote relay fanout. If one local
subscriber forum fails, the runtime should continue creating other local
subscriber surfaces and continue the remote relay path.

### Inbound remote comment creates host and local subscriber message surfaces

When an accepted remote subscriber comments on a canonical thread:

```text
remote subscriber comment
  -> existing host forum message creation
  -> existing canonical LocalCommunityMessage row
  -> existing host LocalCommunityMessageSurface row
  -> local subscriber message surfaces for active local subscribers that already
     have the canonical thread's local subscriber thread surface
  -> existing remote relay fanout to other remote subscribers
```

A remote comment must not create an orphan local subscriber message if that
subscriber forum does not have a thread surface for the parent canonical thread.
Skipping that target is correct in Stage 3.

### Duplicate or replayed creates retry missing surfaces only

ActivityPub deliveries and Discord events are replayable. Stage 3 must use the
surface tables as idempotency boundaries.

If a duplicate host thread event arrives and the canonical thread already
exists, the runtime must not republish ActivityPub and must not create another
canonical row. It may use the supplied Discord source event to create any
missing local subscriber thread surfaces.

If a duplicate inbound remote post arrives and the canonical thread already
exists, the runtime must not create another host thread. It may create any
missing local subscriber thread surfaces from the normalized inbound event body
and should keep the existing remote relay idempotency behavior.

The same rule applies to comments: duplicate processing may create missing
local subscriber message surfaces, but it must not create duplicate canonical
messages or duplicate surfaces.

### Local subscriber forums are still not source forums

If a user creates a new thread or message inside a local subscriber forum in
Stage 3:

```text
local subscriber forum source event
  -> not routed as local-community source
  -> no ActivityPub publish
  -> no canonical LocalCommunityThread/LocalCommunityMessage row
  -> no remote relay
```

This is intentional. Local-subscriber-originated activity is Stage 4.

### Local subscriber mirror edits/deletes are contained

Stage 3 creates local subscriber surfaces, so edit/delete routing may recognize
those mirrored messages as local-community-owned surfaces. That must not make
subscriber mirrors authoritative.

If a user edits or deletes a local subscriber mirror surface in Stage 3:

- do not publish an ActivityPub Update/Delete;
- do not update/delete host or sibling local subscriber surfaces;
- do not relay to remote subscribers.

Participant-wide edit/delete behavior is Stage 5.

### No retroactive history sync

When a forum becomes a local subscriber, Stage 3 does not backfill old threads
or old comments into it. Only new creates, or replayed creates that still carry
usable source content, can create local subscriber surfaces.

## Architecture

### Add `local_subscriber_id` to surface rows before creating subscriber surfaces

Stage 2 surface rows currently store `role`, `discord_forum_channel_id`, and the
Discord ids. Before Stage 3 creates `role="local_subscriber"` rows, add a
nullable `local_subscriber_id` column to both surface tables:

```text
local_community_thread_surfaces.local_subscriber_id nullable integer
local_community_message_surfaces.local_subscriber_id nullable integer
```

Invariants:

```text
role = host
  -> local_subscriber_id is null

role = local_subscriber
  -> local_subscriber_id points to the LocalSubscriber row active when the
     surface was created
```

Do not hard-delete historical surfaces when a local subscriber is later removed.
The stored `local_subscriber_id` preserves the historical target identity even
if future runtime selection only uses currently active local subscribers.

### Add a local Discord fanout service

Create `src/local_communities/discord_fanout.py`.

Responsibility:

- select active `LocalSubscriber` rows for one local community;
- exclude the source forum when a source forum is provided;
- create missing thread surfaces for canonical posts;
- create missing message surfaces for canonical comments;
- resolve reply parents per target surface;
- continue after per-target Discord failures;
- return a summary that tests can assert without depending only on mocks.

Suggested shape:

```python
@dataclass(slots=True)
class LocalDiscordFanoutSummary:
    """Summarise local Discord fanout attempts for one canonical activity."""

    attempted: int = 0
    delivered: int = 0
    skipped_existing: int = 0
    skipped_missing_thread_surface: int = 0
    skipped_missing_parent_surface: int = 0
    failed: int = 0


class LocalCommunityDiscordFanout:
    """Create local subscriber Discord surfaces for canonical community activity."""

    async def fanout_thread_to_local_subscribers(
        self,
        *,
        local_community: object,
        thread_row: object,
        title: str,
        content: str,
        source_forum_channel_id: int | None,
    ) -> LocalDiscordFanoutSummary: ...

    async def fanout_message_to_local_subscribers(
        self,
        *,
        local_community: object,
        thread_row: object,
        message_row: object,
        content: str,
        source_forum_channel_id: int | None,
    ) -> LocalDiscordFanoutSummary: ...
```

This service is local-Discord-only. It must not call the Fedify gateway and must
not choose remote subscribers.

### Keep canonical rows as the single source of AP identity

Stage 3 must continue the Stage 2 model:

- canonical `LocalCommunityThread` owns the AP post activity/object ids;
- canonical `LocalCommunityMessage` owns the AP comment activity/object ids and
  AP parent object id;
- Discord surface rows own concrete Discord thread/message ids.

Local subscriber copies must never create extra canonical activity rows.

### Use surface rows as idempotency and retry boundaries

For thread fanout, before creating a subscriber copy, check for an existing
surface by canonical thread and target forum:

```python
existing = database.get_local_community_thread_surface(
    local_community_thread_id=thread_row.id,
    discord_forum_channel_id=local_subscriber.discord_channel_id,
)
```

For comment fanout, first resolve the target thread surface, then check for an
existing message surface by canonical message and target thread surface:

```python
existing = database.get_local_community_message_surface(
    local_community_message_id=message_row.id,
    local_community_thread_surface_id=target_thread_surface.id,
)
```

These helpers are required for safe duplicate processing. They must be used
before every Discord create/send call.

### Resolve comment parents per target surface

Do not reuse the host forum parent Discord id for local subscriber surfaces.
Parent ids are surface-local.

Suggested helper:

```python
def resolve_local_subscriber_parent_message_id(
    *,
    database: Database,
    thread_row: object,
    message_row: object,
    target_thread_surface: object,
) -> int | None:
    """Resolve the parent Discord message id inside one target surface."""
```

Rules:

```text
message_row.parent_ap_object_id is None
or message_row.parent_ap_object_id == thread_row.ap_object_id
  -> return target_thread_surface.discord_starter_message_id

message_row.parent_ap_object_id references a known canonical message
  -> find that canonical parent message's surface under target_thread_surface
  -> return that surface.discord_message_id

parent canonical message or target parent surface missing
  -> return a sentinel that causes the target to be skipped
```

Skipping is safer than flattening, because flattening would silently corrupt
nested reply structure.

### Runtime integration points

`LocalCommunityRuntime` should instantiate `LocalCommunityDiscordFanout` next to
`LocalCommunityFederationFanout`.

Host-origin thread path:

```text
handle_discord_thread_create
  -> existing duplicate check
  -> if duplicate: fanout missing local subscriber thread surfaces and return ignored/duplicate
  -> existing publish_local_thread_starter
  -> existing canonical thread + host surface create
  -> local Discord fanout to local subscribers
```

Host-origin comment path:

```text
handle_discord_message
  -> existing duplicate check
  -> if duplicate: fanout missing local subscriber message surfaces and return ignored/duplicate
  -> existing publish_local_thread_message
  -> existing canonical message + host surface create
  -> local Discord fanout to local subscribers
```

Inbound remote post path:

```text
handle_inbound_post
  -> accepted remote subscriber check
  -> if canonical post already exists:
       local Discord fanout for missing subscriber thread surfaces
       existing remote relay idempotency path
       return skipped/post already mapped
  -> existing host forum create
  -> existing canonical thread + host surface create
  -> local Discord fanout to local subscribers
  -> existing remote relay fanout
```

Inbound remote comment path:

```text
handle_inbound_comment
  -> accepted remote subscriber check
  -> if canonical comment already exists:
       local Discord fanout for missing subscriber message surfaces
       existing remote relay idempotency path
       return skipped/comment already mapped
  -> existing host forum send
  -> existing canonical message + host surface create
  -> local Discord fanout to local subscribers
  -> existing remote relay fanout
```

### Formatting source content for local subscriber copies

Stage 3 should use existing formatting boundaries rather than inventing a new
rendering policy.

Recommended rules:

- host-origin post copy content: use the host starter message content, preserving
  the same body that was published to ActivityPub;
- host-origin comment copy content: use the host reply message content;
- inbound remote post copy content: use `_format_inbound_post_body(event)` so
  host and local subscriber copies use the same remote-author formatting;
- inbound remote comment copy content: use `_format_inbound_comment_body(event)`.

If a later stage wants different local mirror formatting, that should be a
separate presentation change, not part of Stage 3.

### Keep remote federation fanout separate

Do not merge `LocalCommunityDiscordFanout` with
`LocalCommunityFederationFanout`.

Remote federation fanout continues to own:

- remote subscriber target selection;
- relay source activity rows;
- relay delivery rows;
- gateway `send_local_community_relay()` calls.

Local Discord fanout owns only Discord forum/thread/message surfaces.

### Router boundary remains host-source-only for creates

`DiscordEventRouter.is_local_community_forum()` should remain anchored to
`LocalCommunity.discord_forum_channel_id` in Stage 3.

Do not query `local_subscribers` inside create routing methods yet:

- `handle_thread_create()`;
- `handle_message()`.

For edit/delete routing, local subscriber surfaces may be recognized as
local-community-owned only to contain mirror edits/deletes. The runtime must not
publish Update/Delete for mirrored local subscriber surfaces in this stage.

## Touched Files

```text
plans/51_local_community_channel_subscriptions.md
src/models.py
src/db.py
src/discord_event_router.py
src/local_communities/runtime.py
src/local_communities/delivery_mapping.py
src/local_communities/reply_mapping.py
src/local_communities/README.md
docs/architecture/database-map.md
docs/architecture/event-flows.md
docs/development/navigation.md
notes/known_issues.md
tests/support/discord.py
tests/behavior/test_local_community_publish_scenarios.py
tests/behavior/test_local_community_inbound_scenarios.py
tests/behavior/test_local_community_surface_stage2_scenarios.py
```

## New Files

```text
plans/55_stage3_host_remote_to_local_subscriber_sync.md
src/local_communities/discord_fanout.py
tests/behavior/test_local_community_stage3_local_subscriber_sync_scenarios.py
```

## Implementation Steps

### 1. Add failing Stage 3 behavior tests first

Create `tests/behavior/test_local_community_stage3_local_subscriber_sync_scenarios.py`.

Required scenarios:

1. Host forum thread creates local subscriber thread surfaces.
   - Given host forum `100` and local subscriber forums `200` and `300`.
   - When a registered user creates a thread in `100`.
   - Then the existing host surface exists.
   - Then one `role="local_subscriber"` thread surface exists for `200`.
   - Then one `role="local_subscriber"` thread surface exists for `300`.
   - Then no second host surface exists.

2. Host forum reply creates local subscriber message surfaces.
   - Given a canonical thread with host and local subscriber thread surfaces.
   - When a registered user replies in the host thread.
   - Then local subscriber message surfaces are created under each target thread
     surface.
   - Then root reply references target starter messages, not the host starter id.

3. Host nested reply preserves per-surface parent mapping.
   - Given a prior canonical comment has host and local subscriber message
     surfaces.
   - When a nested host reply targets that prior comment.
   - Then each subscriber copy references the prior comment surface in the same
     subscriber thread.

4. Inbound remote post creates local subscriber thread surfaces and still relays
   remotely.
   - Given an accepted remote subscriber origin and at least one other accepted
     remote subscriber target.
   - When a remote post arrives.
   - Then host and local subscriber thread surfaces exist.
   - Then existing remote relay delivery behavior still happens and excludes
     the origin remote actor.

5. Inbound remote comment creates local subscriber message surfaces.
   - Given a canonical thread with local subscriber thread surfaces.
   - When a remote comment arrives.
   - Then host and local subscriber message surfaces exist.
   - Then remote relay behavior remains unchanged.

6. Partial local Discord fanout failure does not block healthy targets.
   - Given local subscribers `200` and `300`.
   - Given `200` raises during `create_thread()` or `send()`.
   - When content is processed.
   - Then `300` still gets a surface row.
   - Then the runtime result does not fail the whole source action.
   - Then the failed target has no surface row.

7. Duplicate source processing retries only missing local subscriber surfaces.
   - Given the first processing created the host surface and one local subscriber
     surface, but another subscriber target failed.
   - When the same source event is processed again.
   - Then no new canonical row is created.
   - Then no duplicate existing surface is created.
   - Then the previously missing target is attempted again and can get a surface.

8. Local subscriber forum thread/message creates are still not local-community
   sources.
   - Given forum `200` is a `LocalSubscriber`.
   - When a user creates a thread or message in `200`.
   - Then `DiscordEventRouter` does not dispatch that create into
     `LocalCommunityRuntime`.

9. Local subscriber mirror edit/delete is contained.
   - Given a `role="local_subscriber"` surface exists.
   - When the mirrored Discord message is edited or deleted.
   - Then no gateway `update_content()` or `delete_content()` call occurs.
   - Then no sibling surfaces are edited/deleted in Stage 3.

### 2. Add `local_subscriber_id` columns to surface models and migration

Update `src/models.py`:

```python
class LocalCommunityThreadSurface(Base):
    local_subscriber_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

class LocalCommunityMessageSurface(Base):
    local_subscriber_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
```

Update `Database.migrate()` with idempotent `ALTER TABLE ADD COLUMN` steps for
existing SQLite databases. Existing host surfaces should keep
`local_subscriber_id = NULL`.

Update `Database.create_all()` path naturally through the ORM model.

### 3. Add surface lookup helpers required for idempotent fanout

Add or extend repository helpers in `src/db.py`:

```python
get_local_community_thread_surface(
    local_community_thread_id: int,
    discord_forum_channel_id: int,
) -> LocalCommunityThreadSurface | None

get_local_community_message_surface(
    local_community_message_id: int,
    local_community_thread_surface_id: int,
) -> LocalCommunityMessageSurface | None
```

Extend create helpers with `local_subscriber_id`:

```python
create_local_community_thread_surface(..., role: str, local_subscriber_id: int | None = None)
create_local_community_message_surface(..., role: str, local_subscriber_id: int | None = None)
```

The helpers should keep uniqueness violations out of normal retry paths by
checking existing rows before insert where the runtime needs idempotent fanout.

### 4. Implement `LocalCommunityDiscordFanout`

Create `src/local_communities/discord_fanout.py` with docstrings and comments
around target selection, parent resolution, idempotency, and partial failure.

Thread fanout requirements:

- list active local subscribers for the community;
- skip `source_forum_channel_id` if it matches a subscriber channel;
- skip targets that already have a thread surface for the canonical thread;
- fetch each target forum through `bot.fetch_forum_channel()`;
- call `create_thread(name=title, content=content)`;
- support both tuple and object return shapes, matching
  `LocalCommunityRuntime._unpack_created_thread()` behavior;
- create `role="local_subscriber"` thread surface rows with
  `local_subscriber_id` set.

Message fanout requirements:

- list active local subscribers for the community;
- resolve the target thread surface for each subscriber;
- skip if target thread surface is missing;
- skip if the message surface already exists under that target thread surface;
- resolve the parent message id inside the target thread surface;
- skip if a nested parent surface is missing;
- send the message to the target Discord thread;
- create `role="local_subscriber"` message surface rows with
  `local_subscriber_id` set.

Partial failure rule:

```text
catch/log per target -> increment failed -> continue other targets
```

Do not catch programming errors outside a target boundary in a way that hides
broken invariants.

### 5. Add per-surface parent resolution for local subscriber fanout

Extend `src/local_communities/reply_mapping.py` or keep the helper private to
`discord_fanout.py` if it is only used there.

Required behavior:

- root comment copies target the subscriber thread starter message;
- nested comment copies target the mapped parent comment surface in the same
  subscriber thread;
- missing nested parent surface means skip target, not fallback-to-root.

Add tests for both root and nested behavior.

### 6. Wire host-origin create paths to local Discord fanout

Update `LocalCommunityRuntime.handle_discord_thread_create()`:

- when no duplicate exists, keep existing publish and canonical/host-surface
  behavior;
- after canonical thread creation, call local Discord fanout for subscribers;
- if duplicate exists, resolve the existing canonical thread and call fanout for
  missing subscriber surfaces without republishing ActivityPub.

Update `LocalCommunityRuntime.handle_discord_message()` similarly:

- keep existing publish and canonical/host-surface behavior;
- after canonical message creation, call local Discord fanout;
- if duplicate exists, resolve the existing canonical message/thread and call
  fanout for missing subscriber surfaces without republishing ActivityPub.

The method result can remain compatible with existing callers, but tests should
assert the new surface side effects.

### 7. Wire inbound remote create paths to local Discord fanout

Update `LocalCommunityRuntime.handle_inbound_post()`:

- after host thread creation and canonical row creation, call local Discord
  fanout for active local subscribers;
- when the canonical post already exists, call fanout for missing subscriber
  thread surfaces before returning the existing duplicate/skipped result;
- preserve the existing accepted-remote-subscriber check;
- preserve existing remote relay behavior.

Update `LocalCommunityRuntime.handle_inbound_comment()` similarly:

- after host message creation and canonical row creation, call local Discord
  fanout for active local subscribers;
- when the canonical comment already exists, call fanout for missing subscriber
  message surfaces before returning the duplicate/skipped result;
- preserve existing remote relay behavior.

### 8. Contain local subscriber mirror edit/delete behavior

Review `DiscordEventRouter.is_local_community_message()` and
`LocalCommunityRuntime.handle_discord_message_edit/delete()` with the new
subscriber surfaces present.

Stage 3 acceptable behavior:

- router may classify subscriber mirror surfaces as local-community-owned so
  they do not leak into remote subscription edit/delete paths;
- runtime must not publish AP Update/Delete for subscriber mirror surfaces;
- runtime must not update/delete sibling surfaces.

Implementation options:

1. keep relying on absence of `PublishedActivityObject` for mirrored subscriber
   surfaces, and add explicit comments/tests; or
2. add an explicit surface-role check and return early for `role="local_subscriber"`.

Prefer explicit role checks if they make the stage boundary clearer.

### 9. Update docs and known issues within their boundaries

Read the purpose paragraph of each relevant doc before editing.

Update only docs whose scope is affected:

- `docs/architecture/database-map.md`
  - surface rows can now include `role="local_subscriber"` and
    `local_subscriber_id`;
- `docs/architecture/event-flows.md`
  - host-origin and inbound remote creates now fan out to local subscriber
    surfaces;
- `docs/development/navigation.md`
  - point local subscriber fanout work to `discord_fanout.py`;
- `src/local_communities/README.md`
  - Stage 3 local subscribers are synchronized read surfaces, not sources;
- `plans/51_local_community_channel_subscriptions.md`
  - mark the Stage 3 boundary as host/remote create fanout only if that file is
    being kept as the umbrella plan;
- `notes/known_issues.md`
  - record any intentionally deferred Stage 4/Stage 5 behavior if it is not
    already documented.

## Tests

Follow TDD. Run at least:

```bash
pytest tests/behavior/test_local_community_stage3_local_subscriber_sync_scenarios.py
pytest tests/behavior/test_local_subscriber_stage1_scenarios.py
pytest tests/behavior/test_local_community_surface_stage2_scenarios.py
pytest tests/behavior/test_local_community_publish_scenarios.py
pytest tests/behavior/test_local_community_inbound_scenarios.py
pytest tests/behavior/test_local_community_remote_fanout_scenarios.py
pytest tests/behavior/test_local_community_edit_delete_scenarios.py
pytest tests/behavior/test_subscription_scenarios.py
pytest
```

The Stage 3 tests must assert real effects:

- canonical row counts;
- host and local subscriber surface row counts;
- per-target Discord fake observable effects;
- partial failure does not block healthy targets;
- duplicate/replay behavior creates missing surfaces only;
- remote relay targeting remains unchanged;
- local subscriber source events remain non-source events.

Do not rely mainly on mock-call assertions. Mock only the Discord and gateway
outer boundaries.

## Compatibility Risks / Regression Analysis

### Risk: duplicate creates return before retrying missing subscriber surfaces

Existing duplicate checks return early for already mapped posts/comments. Stage
3 must change those duplicate paths enough to retry missing local subscriber
surfaces while still avoiding duplicate AP publishes and duplicate canonical
rows.

### Risk: nested replies can flatten on subscriber surfaces

Host surface parent ids are not valid inside subscriber forum threads. If fanout
reuses host parent ids, subscriber replies will point at non-existent or wrong
messages. If fanout falls back to the starter when a nested parent surface is
missing, it silently corrupts the reply tree. Missing nested parent surfaces
must skip that target.

### Risk: local subscriber mirrors become accidental source messages

Once local subscriber surfaces exist, router edit/delete ownership checks may
recognize them. That is acceptable only if runtime treats them as non-source
mirrors in Stage 3. Thread/message create routing must not consult
`local_subscribers` until Stage 4.

### Risk: remote relay behavior regresses while adding local Discord fanout

Inbound remote post/comment paths already relay to other remote subscribers.
Local Discord fanout must not replace or block `LocalCommunityFederationFanout`.
A failed Discord subscriber target must not prevent remote relay delivery rows
from being created.

### Risk: partial local Discord failures leave hidden gaps

Stage 3 does not add a durable local Discord delivery table. A failed target is
represented by the absence of a surface row. The runtime must therefore make
reprocessing safe and useful: duplicate source processing should retry missing
surfaces rather than treating the whole activity as finished.

### Risk: stale local subscriber rows target deleted Discord forums

A local subscriber may point to a forum that no longer exists or cannot be
fetched. That target should fail independently and should not block other local
subscribers. Stage 3 should log the failure and leave no surface row for that
failed target.

### Risk: public dashboard semantics overstate live participation

Stage 1 dashboard counts local subscribers as control-plane rows. Stage 3 makes
new creates fan out to local subscribers, but it still does not make subscriber
forums valid sources and does not backfill history. Documentation and dashboard
labels must not imply full participant parity until Stage 4/5 are complete.

## Regression / Blind-Spot Analysis

### Blind spot: only testing host-origin creates misses remote-origin fanout

Host-origin and inbound remote-origin content use different formatting and
runtime branches. Stage 3 tests must cover both, otherwise local subscriber
fanout can work for Discord posts but fail for remote posts/comments.

### Blind spot: root comment tests do not prove nested reply correctness

Root replies only need the target starter message. Nested replies require a
parent comment surface in the same target thread. Tests must include nested
host-origin and/or remote-origin replies to catch surface-local parent bugs.

### Blind spot: green happy-path tests do not prove idempotency

Surface rows are the retry boundary. Tests must process the same source twice
and assert canonical row counts and surface row counts stay stable, while a
previously missing target can still be repaired.

### Blind spot: local subscriber source behavior can drift early

Because local subscriber forums now receive copied threads/messages, it is easy
to accidentally route their new Discord events as local-community source events.
Negative router/runtime tests must prove that Stage 4 behavior did not leak into
Stage 3.

## Open Questions

1. Should failed local Discord fanout targets be recorded in a dedicated retry table?

   Recommendation: not in Stage 3. Use missing surface rows as the retry signal
   during duplicate/replayed source processing. A dedicated retry table belongs
   in Stage 5 if operational retry needs exceed this simple model.

2. Should Stage 3 expose local subscriber surface details on the public dashboard?

   Recommendation: no. Keep the dashboard to aggregate local subscriber counts
   unless a later UI plan explicitly decides that Discord forum ids or names are
   safe to expose publicly.
