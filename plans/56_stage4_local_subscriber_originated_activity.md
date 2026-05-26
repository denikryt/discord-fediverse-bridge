# 56 — Stage 4 local-subscriber-originated activity

## Problem / Goal

Stage 1 established explicit participant state:

- `RemoteSubscriber` / `remote_subscribers` for ActivityPub actors following a bridge-owned local community;
- `LocalSubscriber` / `local_subscribers` for same-instance Discord forum channels subscribed to a bridge-owned local community.

Stage 2 split canonical community activity from concrete Discord surfaces:

- `LocalCommunityThread` / `LocalCommunityMessage` own canonical AP identity;
- `LocalCommunityThreadSurface` / `LocalCommunityMessageSurface` own per-Discord-surface thread/message ids.

Stage 3 made local subscribers receive new create-time fanout from already-supported sources:

- host forum posts/comments fan out to local subscriber surfaces;
- inbound remote subscriber posts/comments fan out to local subscriber surfaces;
- local subscriber forums remain non-source forums.

Stage 4 should complete the create-time participant model by making active local subscriber forums valid sources of canonical local-community activity:

```text
local subscriber forum post/comment
  -> one canonical local-community post/comment
  -> one source local-subscriber surface
  -> host forum surface
  -> other local subscriber surfaces
  -> existing remote federation delivery for Discord-authored local-community content
```

This closes the create-path gap left by Stage 3. After Stage 4, every participant type can create new posts/comments:

```text
host forum source            -> supported before Stage 4
remote subscriber source     -> supported before Stage 4
local subscriber forum source -> supported by Stage 4
```

Stage 4 must not implement participant-wide edit/delete propagation. Local-subscriber-originated edits and deletes still belong to Stage 5.

## Stage Boundary

### Stage 4 owns

Stage 4 owns only local-subscriber-originated create behavior:

- route new Discord thread/message events from active local subscriber forums into `LocalCommunityRuntime`;
- resolve the local community and `LocalSubscriber` row for one source forum;
- publish a local-subscriber-originated thread starter as one ActivityPub post through the existing local-community publish path;
- publish a local-subscriber-originated reply as one ActivityPub comment through the existing local-community publish path;
- create canonical `LocalCommunityThread` / `LocalCommunityMessage` rows for those source events;
- create source `role="local_subscriber"` surface rows for the originating forum/thread/message;
- create host forum surfaces for local-subscriber-originated posts/comments;
- create sibling local-subscriber surfaces for every other active local subscriber;
- avoid duplicate canonical rows and duplicate target surfaces on retry;
- preserve existing host-originated, remote-originated, and remote-subscription behavior.

### Stage 4 does not own

Stage 4 must not include later-stage behavior:

- no participant-wide edit propagation across host/local-subscriber surfaces;
- no participant-wide delete propagation across host/local-subscriber surfaces;
- no AP Update/Delete publishing from local subscriber source edits/deletes;
- no retry redesign for edit/delete failures;
- no retroactive history backfill into local subscriber forums;
- no changes to remote `BridgeActorFollow` or remote community subscription lifecycle;
- no new schema rewrite beyond helper/index additions that are directly required for source routing and idempotency.

### Stage 4 handoff contract

Stage 4 is complete only when these are all true:

- a local subscriber forum can create a new canonical post;
- a local subscriber forum can create a new canonical comment;
- the source local subscriber forum does not receive a duplicate copy of its own source activity;
- the host forum receives the local-subscriber-originated post/comment as a concrete surface;
- every other active local subscriber receives the local-subscriber-originated post/comment as a concrete surface;
- inactive local subscribers do not receive new surfaces;
- duplicate source events create only missing target surfaces, not duplicate canonical rows;
- local-subscriber-originated edit/delete behavior remains explicitly deferred to Stage 5 and does not accidentally propagate.

## Expected Behavior

### Local subscriber thread creates one canonical post

Given:

```text
LocalCommunity(id=7, discord_forum_channel_id=100)
LocalSubscriber(id=20, local_community_id=7, discord_channel_id=200, status="active")
LocalSubscriber(id=30, local_community_id=7, discord_channel_id=300, status="active")
```

When a registered Discord user creates a thread in local subscriber forum `200`:

```text
local subscriber forum 200 thread starter
  -> route to LocalCommunityRuntime
  -> publish one AP post through ContentPublishService.publish_local_thread_starter()
  -> create one canonical LocalCommunityThread row
  -> create one source LocalCommunityThreadSurface row for forum 200
  -> create one host LocalCommunityThreadSurface row for forum 100
  -> create one sibling LocalCommunityThreadSurface row for forum 300
```

Expected source-surface row shape:

```text
local_community_thread_surfaces
  local_community_thread_id = <canonical thread id>
  discord_forum_channel_id = 200
  discord_thread_id = <source Discord thread id>
  discord_starter_message_id = <source starter message id>
  role = local_subscriber
  local_subscriber_id = 20
```

Expected host-surface row shape:

```text
local_community_thread_surfaces
  local_community_thread_id = <canonical thread id>
  discord_forum_channel_id = 100
  discord_thread_id = <created host thread id>
  discord_starter_message_id = <created host starter message id>
  role = host
  local_subscriber_id = null
```

The source forum `200` must not receive a copied thread back from fanout.

### Local subscriber reply creates one canonical comment

When a registered Discord user replies in a local subscriber thread surface:

```text
local subscriber reply
  -> route to LocalCommunityRuntime
  -> resolve canonical thread through the source thread surface
  -> resolve parent AP object through the source surface's local reply context
  -> publish one AP comment through ContentPublishService.publish_local_thread_message()
  -> create one canonical LocalCommunityMessage row
  -> create one source LocalCommunityMessageSurface row for the source forum
  -> create one host LocalCommunityMessageSurface row under the host thread surface
  -> create sibling local subscriber message surfaces under each sibling thread surface
```

For root replies, the host/sibling copy should reply to that target thread surface's starter message.
For nested replies, the host/sibling copy should reply to the matching parent comment surface in that same target thread.
If a target does not have the required parent surface, skip that target instead of flattening the reply to the thread root.

### Local subscriber source routing is explicit

`DiscordEventRouter` should distinguish local-community source forums as:

```text
host_forum
local_subscriber
neither
```

Routing rules:

```text
host_forum thread/message
  -> LocalCommunityRuntime existing host path

active local_subscriber forum thread/message
  -> LocalCommunityRuntime new local-subscriber source path

inactive/deleted local_subscriber forum thread/message
  -> not a local-community source
```

Remote-subscription forums remain routed through `CommunityRuntime` unless they are explicitly rejected earlier by subscription-control invariants. Stage 4 must not silently let one forum participate in both remote-subscription and local-subscriber source modes.

### Duplicate local subscriber source events retry missing target surfaces only

If the same source local subscriber thread event is processed again:

- do not call `publish_local_community_content()` again;
- do not create another canonical `LocalCommunityThread` row;
- do not create another source surface;
- create any missing host or sibling local-subscriber thread surfaces if they were absent because an earlier fanout target failed.

If the same source local subscriber message event is processed again:

- do not call `publish_local_community_content()` again;
- do not create another canonical `LocalCommunityMessage` row;
- do not create another source surface;
- create any missing host or sibling local-subscriber message surfaces when their parent surfaces exist.

### Partial fanout failures do not block canonical publish

If the local subscriber source publish succeeds but one local Discord target fails:

- the canonical row and source surface remain persisted;
- healthy local targets still receive surfaces;
- failed targets can be retried by processing the same source event again;
- remote delivery behavior from the gateway publish path remains independent of local Discord fanout failure.

### Local subscriber source edits/deletes stay deferred

If a user edits or deletes a local-subscriber-originated source surface in Stage 4:

- do not publish AP Update/Delete yet;
- do not update/delete host or sibling surfaces yet;
- do not relay Update/Delete to remote subscribers yet.

This is intentionally incomplete until Stage 5. Stage 4 only makes create behavior participant-complete.

## Architecture

### Add participant-aware source resolution

Create or add a small routing boundary that resolves one Discord forum channel to a local-community source context.

Recommended new file:

```text
src/local_communities/participant_routing.py
```

Suggested shape:

```python
@dataclass(slots=True)
class ResolvedLocalCommunitySource:
    """Describe the local-community participant that owns one Discord forum."""

    local_community: object
    source_kind: str  # "host_forum" or "local_subscriber"
    local_subscriber: object | None
    discord_forum_channel_id: int
```

Required helper:

```python
def resolve_local_community_source_for_forum(
    database: Database,
    forum_channel_id: int | None,
) -> ResolvedLocalCommunitySource | None:
    """Resolve host or active local-subscriber ownership for one Discord forum."""
```

Resolution rules:

1. If `forum_channel_id` is the `LocalCommunity.discord_forum_channel_id`, return `source_kind="host_forum"`.
2. Else if `forum_channel_id` has an active `LocalSubscriber`, load its `LocalCommunity` and return `source_kind="local_subscriber"`.
3. Else return `None`.

Do not return inactive/deleted local subscribers as valid sources.

### Keep host paths stable while adding local-subscriber paths

`LocalCommunityRuntime.handle_discord_thread_create()` and `handle_discord_message()` may remain the public runtime entry points, but they must branch on the resolved source context:

```text
source_kind = host_forum
  -> existing Stage 3 behavior

source_kind = local_subscriber
  -> new Stage 4 behavior
```

Alternatively, add private helpers:

```python
_handle_host_thread_create(...)
_handle_local_subscriber_thread_create(...)
_handle_host_message(...)
_handle_local_subscriber_message(...)
```

The public runtime API should stay narrow so `DiscordEventRouter` does not need to know publish/fanout details.

### Persist source local-subscriber surfaces before fanout copies

For local-subscriber-originated posts, after successful AP publish and canonical row creation, create the source surface explicitly:

```python
database.create_local_community_thread_surface(
    local_community_thread_id=thread_row.id,
    discord_forum_channel_id=source_forum_id,
    discord_thread_id=thread.id,
    discord_starter_message_id=starter_message.id,
    role="local_subscriber",
    local_subscriber_id=source_local_subscriber.id,
)
```

For local-subscriber-originated comments, after successful AP publish and canonical row creation, create the source message surface explicitly under the source thread surface:

```python
database.create_local_community_message_surface(
    local_community_message_id=message_row.id,
    local_community_thread_surface_id=source_thread_surface.id,
    discord_forum_channel_id=source_forum_id,
    discord_message_id=message.id,
    parent_discord_message_id=reply_context.parent_discord_message_id,
    role="local_subscriber",
    local_subscriber_id=source_local_subscriber.id,
)
```

Do not rely on Stage 2 canonical-row creation helpers to create a host surface for local-subscriber source events. Source events need source surfaces first, then explicit fanout to host and siblings.

### Extend local Discord fanout to include the host as a target

`LocalCommunityDiscordFanout` currently fans out from host/remote sources to local subscribers only. Stage 4 needs the same helper to support:

```text
local subscriber source
  -> host target
  -> sibling local subscriber targets
  -> exclude source local subscriber forum
```

Recommended design:

```python
@dataclass(slots=True)
class LocalDiscordFanoutTarget:
    """Describe one Discord forum target for local-community surface fanout."""

    role: str  # "host" or "local_subscriber"
    discord_forum_channel_id: int
    local_subscriber_id: int | None
```

`LocalCommunityDiscordFanout` should select targets through one internal method, for example:

```python
def _select_targets(
    *,
    local_community: object,
    include_host: bool,
    source_forum_channel_id: int | None,
) -> list[LocalDiscordFanoutTarget]: ...
```

Target selection rules:

```text
host/remote origin
  include_host = False
  targets = active local subscribers except source forum if any

local subscriber origin
  include_host = True
  targets = host forum + active local subscribers except source forum
```

Surface rows created for host targets must use:

```text
role = host
local_subscriber_id = null
```

Surface rows created for local subscriber targets must use:

```text
role = local_subscriber
local_subscriber_id = target.local_subscriber_id
```

### Resolve local-subscriber reply context from the source surface

`resolve_outbound_reply_context()` currently resolves root replies through the host thread surface. Stage 4 must resolve parent Discord ids through the source thread surface instead.

Add or extend a helper such as:

```python
def resolve_outbound_reply_context_for_surface(
    *,
    database: Database,
    thread_row: object,
    source_thread_surface: object,
    message: object,
) -> ResolvedReplyTarget: ...
```

Rules:

```text
no Discord reference
  -> parent AP object = thread_row.ap_object_id
  -> parent Discord message id = source_thread_surface.discord_starter_message_id

reference points to source thread starter
  -> parent AP object = thread_row.ap_object_id
  -> parent Discord message id = source_thread_surface.discord_starter_message_id

reference points to a mapped message surface in the same source thread surface
  -> parent AP object = canonical parent message ap_object_id
  -> parent Discord message id = referenced Discord message id

reference points to an unknown message or a message surface from another thread surface
  -> fall back to thread_row.ap_object_id for AP parent, matching the current host behavior
```

The source-surface constraint prevents a reply in subscriber forum `200` from accidentally using a parent message surface from host forum `100` or sibling forum `300`.

### Remote delivery should use the existing local-community publish path

Local-subscriber-originated AP creates should use the same gateway boundary as host-originated local-community creates:

```python
ContentPublishService.publish_local_thread_starter(...)
ContentPublishService.publish_local_thread_message(...)
```

These call `fedify_gateway.publish_local_community_content()` and persist generic `PublishedActivityObject` rows for the actual source Discord message id. Stage 4 should not introduce a separate remote relay path for Discord-authored local subscriber creates.

### Keep surface idempotency as the retry boundary

Stage 4 must reuse the Stage 3 surface idempotency contract:

- existing source thread surface means the source thread is already canonical;
- existing source message surface means the source message is already canonical;
- missing host/sibling target surfaces can be retried without republishing AP.

This probably requires a helper that can fan out from an existing canonical row when duplicate source processing is detected:

```text
source duplicate thread
  -> load canonical thread from source thread surface
  -> fanout missing host/sibling thread surfaces
  -> return duplicate/ignored result

source duplicate message
  -> load canonical message from source message surface
  -> fanout missing host/sibling message surfaces
  -> return duplicate/ignored result
```

## Touched Files

```text
plans/51_local_community_channel_subscriptions.md
src/discord_event_router.py
src/local_communities/runtime.py
src/local_communities/discord_fanout.py
src/local_communities/delivery_mapping.py
src/local_communities/reply_mapping.py
src/local_communities/README.md
src/db.py
docs/architecture/bridge-modes.md
docs/architecture/event-flows.md
docs/development/navigation.md
notes/known_issues.md
tests/support/discord.py
tests/behavior/test_local_community_stage3_local_subscriber_sync_scenarios.py
tests/behavior/test_local_community_publish_scenarios.py
tests/behavior/test_local_community_inbound_scenarios.py
tests/behavior/test_subscription_scenarios.py
```

## New Files

```text
plans/56_stage4_local_subscriber_originated_activity.md
src/local_communities/participant_routing.py
tests/behavior/test_local_community_stage4_local_subscriber_origin_scenarios.py
```

### Required new behavior test file

Stage 4 must create a dedicated behavior test module:

```text
tests/behavior/test_local_community_stage4_local_subscriber_origin_scenarios.py
```

This file is required because Stage 4 introduces a new runtime source type. It
should not be folded into Stage 3 tests, because Stage 3 proves host/remote
create fanout into local subscribers, while Stage 4 proves local-subscriber
source create behavior.

The file must cover, at minimum:

- local subscriber thread create creates one canonical post, one source surface,
  one host surface, and sibling local-subscriber surfaces;
- duplicate local subscriber thread create retries missing host/sibling surfaces
  without republishing ActivityPub or creating another canonical row;
- local subscriber root reply creates one canonical comment and per-target
  message surfaces with target-local starter parent ids;
- local subscriber nested reply maps source parent surface to each target's
  matching parent surface;
- duplicate local subscriber reply retries missing target message surfaces only;
- inactive local subscribers are not valid local-community sources;
- local-subscriber-originated edit/delete remains deferred and does not publish
  AP Update/Delete or mutate host/sibling surfaces in Stage 4;
- Stage 3 host-originated and remote-originated create fanout still remains
  green after widening source routing.

These scenarios must assert persisted canonical rows, persisted surface rows,
surface `role` / `local_subscriber_id` values, fake Discord effects, and gateway
publish call counts where relevant. Mock-only assertions are not sufficient.

## Implementation Steps

### 1. Add failing Stage 4 behavior tests first

Create `tests/behavior/test_local_community_stage4_local_subscriber_origin_scenarios.py`.

Required scenarios:

1. Local subscriber thread create becomes one canonical post.
   - Given host forum `100`, local subscriber forums `200` and `300`, and a registered user.
   - When a thread starter is created in forum `200`.
   - Then exactly one canonical `LocalCommunityThread` row exists.
   - Then one source local-subscriber surface exists for forum `200`.
   - Then one host surface exists for forum `100`.
   - Then one sibling local-subscriber surface exists for forum `300`.
   - Then `publish_local_community_content()` is called once.

2. Local subscriber thread duplicate retries missing host/sibling surfaces only.
   - Given the source surface already exists but the host or sibling surface is missing.
   - When the same thread create event is handled again.
   - Then no new canonical row is created.
   - Then no second AP publish happens.
   - Then the missing target surface is created.

3. Local subscriber root reply becomes one canonical comment and fans out to host/siblings.
   - Given a canonical thread with source, host, and sibling thread surfaces.
   - When a root reply is created in source forum `200`.
   - Then one canonical `LocalCommunityMessage` row exists.
   - Then source, host, and sibling message surfaces exist.
   - Then host/sibling copies reference their own starter message ids.

4. Local subscriber nested reply preserves surface-local parent ids.
   - Given a parent canonical comment with source, host, and sibling message surfaces.
   - When a nested reply is created in source forum `200` referencing the source parent message.
   - Then host copy references the host parent message id.
   - Then sibling copy references the sibling parent message id.
   - Then no target falls back to the root starter when the parent surface exists.

5. Local subscriber reply duplicate retries missing target message surfaces only.
   - Given the source message surface already exists and one target surface is missing.
   - When the same source reply event is handled again.
   - Then no new canonical message row is created.
   - Then no second AP publish happens.
   - Then only the missing target message surface is created if its parent surface exists.

6. Inactive local subscribers are not source forums.
   - Given a `LocalSubscriber(status="inactive")` for forum `200`.
   - When a thread/message event arrives from forum `200`.
   - Then it is not routed as local-community source.
   - Then no canonical local-community row is created.

7. Local subscriber source edit/delete remains deferred.
   - Given a local-subscriber-originated source surface.
   - When that source message is edited or deleted.
   - Then no AP Update/Delete is sent.
   - Then no host/sibling surface is edited/deleted.

8. Existing Stage 3 behavior remains green.
   - Host-originated create still fans out to local subscribers.
   - Inbound remote-originated create still fans out to local subscribers.
   - Local subscriber source routing does not alter remote-subscription forums.

Tests must use runtime/router paths where possible, not isolated method calls. Assert real DB rows and fake Discord observable effects rather than only mock calls.

### 2. Add participant source routing helper

Create `src/local_communities/participant_routing.py` with the `ResolvedLocalCommunitySource` dataclass and forum-resolution helper.

Update `DiscordEventRouter` to use this helper for thread/message create routing:

- host forum -> `LocalCommunityRuntime`;
- active local subscriber forum -> `LocalCommunityRuntime`;
- neither -> existing remote-subscription/default path.

Do not change edit/delete router policy except where needed to keep existing surface ownership checks working.

### 3. Split runtime host and local-subscriber source paths

Refactor `LocalCommunityRuntime.handle_discord_thread_create()` and `handle_discord_message()` into source-aware branches.

For host source, preserve the Stage 3 behavior.

For local-subscriber source, implement new private helpers that:

- validate the source thread/message belongs to an active `LocalSubscriber`;
- use the local community actor URL from that subscriber's community;
- publish through `ContentPublishService`;
- create canonical rows;
- create the source `role="local_subscriber"` surface;
- call local Discord fanout with `include_host=True` and `source_forum_channel_id=<source forum>`.

### 4. Extend thread fanout to support host targets

Update `LocalCommunityDiscordFanout` so it can create host and local-subscriber thread surfaces from one canonical thread.

Requirements:

- host target uses `LocalCommunity.discord_forum_channel_id`;
- host target row has `role="host"` and `local_subscriber_id=None`;
- local subscriber target rows keep `role="local_subscriber"`;
- source forum is always excluded;
- existing target surfaces are skipped;
- per-target failures are counted and do not stop other targets.

### 5. Extend message fanout to support host targets

Update `LocalCommunityDiscordFanout.fanout_message_to_local_subscribers()` or replace it with a more general method that can target host + local subscribers.

Requirements:

- select host and sibling local subscriber target thread surfaces for local-subscriber source comments;
- skip any target without a thread surface;
- resolve the parent Discord message id per target surface;
- skip nested targets missing the matching parent surface;
- create `LocalCommunityMessageSurface` rows with correct `role` and `local_subscriber_id`.

### 6. Add source-surface reply resolution

Extend `src/local_communities/reply_mapping.py` so outbound replies can be resolved from a non-host source thread surface.

The existing host path can keep using the host helper, but local-subscriber source replies must use the source thread surface so nested replies resolve within the source forum.

### 7. Preserve generic published-object mapping for source Discord ids

Do not introduce new generic publish persistence. The existing `ContentPublishService` should persist `PublishedActivityObject` for the local subscriber source starter/reply message id.

Tests should verify local-subscriber source edit/delete containment against this mapping: even though a `PublishedActivityObject` exists, Stage 4 must still not publish Update/Delete for `role="local_subscriber"` surfaces.

### 8. Update docs and umbrella plan

Update only docs whose stated purpose is touched:

- `plans/51_local_community_channel_subscriptions.md`: mark Stage 4 as local-subscriber-originated create behavior;
- `docs/architecture/bridge-modes.md`: local subscribers are now create-capable participants;
- `docs/architecture/event-flows.md`: add local subscriber post/comment create flow;
- `docs/development/navigation.md`: point to participant routing and source-aware runtime paths;
- `src/local_communities/README.md`: describe Stage 4 boundary and Stage 5 deferral;
- `notes/known_issues.md`: rewrite the Stage 3 local-subscriber source limitation as Stage 5 edit/delete limitation, if present.

## Tests

Follow TDD. Run at least:

```bash
pytest tests/behavior/test_local_community_stage4_local_subscriber_origin_scenarios.py
pytest tests/behavior/test_local_community_stage3_local_subscriber_sync_scenarios.py
pytest tests/behavior/test_local_community_surface_stage2_scenarios.py
pytest tests/behavior/test_local_subscriber_stage1_scenarios.py
pytest tests/behavior/test_local_community_publish_scenarios.py
pytest tests/behavior/test_local_community_inbound_scenarios.py
pytest tests/behavior/test_local_community_remote_fanout_scenarios.py
pytest tests/behavior/test_local_community_edit_delete_scenarios.py
pytest tests/behavior/test_subscription_scenarios.py
pytest
cd fedify-gateway && npm run check
```

The full `pytest` run is mandatory at the end of Stage 4. The green gate for
Stage 4 is the new Stage 4 behavior test file:

```bash
pytest tests/behavior/test_local_community_stage4_local_subscriber_origin_scenarios.py
```

Those tests describe the new behavior introduced by this stage and must be green
before the stage can be considered implemented.

Failure handling rules for the full `pytest` run:

- if the new Stage 4 behavior tests fail, fix Stage 4 before producing the bundle;
- if other tests fail, do not automatically fix them as part of this stage;
- analyze each non-Stage-4 failure and report it to the user with the exact
  failing test name, command, error summary, and whether it appears unrelated,
  indirectly related, stale because of earlier stage behavior, or environment-only;
- only change non-Stage-4 tests or code if the analysis shows the failure is
  directly caused by Stage 4 and the changed behavior is inside this plan's
  responsibility boundary;
- environment-only failures should be reported with the exact command, error,
  and why they are not code failures.

The final response must distinguish clearly between:

- the result of the new Stage 4 behavior tests;
- full `pytest` failures that were analyzed but intentionally not changed;
- environment failures that prevented a check from completing.

Gateway `npm test` is not required for Stage 4 unless gateway-facing contracts are changed. If a code change touches gateway files unexpectedly, stop and update the plan before proceeding.

Tests must assert:

- canonical row counts;
- source, host, and sibling surface rows;
- surface roles and `local_subscriber_id` values;
- AP publish call count for duplicate source events;
- parent Discord message ids per target surface;
- router dispatch behavior for active and inactive local subscriber forums;
- no update/delete propagation from local subscriber source surfaces yet.

Do not rely mainly on “mock was called” tests. Mock the gateway and Discord SDK boundaries only.

## Expected Conflicts / Compatibility Risks

### Router widening risk

Stage 4 deliberately widens local-community create routing from host-only to host-or-active-local-subscriber. If the helper treats inactive subscribers as active, or if it runs after remote-subscription fallback, events can route to the wrong runtime.

Mitigation: router tests must cover host, active local subscriber, inactive local subscriber, and ordinary remote-subscription forum cases.

### Duplicate AP publish risk

The first implementation temptation is to reuse the host duplicate check by Discord thread/message id without noticing that source local-subscriber surfaces are the idempotency boundary. If duplicate processing does not check source surfaces first, retries can publish a second AP Create.

Mitigation: duplicate tests must assert both canonical row count and `publish_local_community_content()` call count.

### Host surface creation risk

Stage 2/3 often assume host surfaces already exist before local-subscriber fanout. Local-subscriber-originated posts invert that: the source surface exists first and the host surface must be created by fanout.

Mitigation: Stage 4 fanout must support explicit host targets and tests must assert one host surface exists after local-subscriber source events.

### Parent mapping risk

Nested replies from a local subscriber forum must resolve parent ids in the source surface and then map to parent ids in each target surface. Accidentally using host-only reply mapping would either fail to resolve source parents or cross-link to host/sibling messages.

Mitigation: nested reply tests must assert source, host, and sibling parent ids separately.

### Edit/delete false-positive risk

After Stage 4, local-subscriber source messages have `PublishedActivityObject` rows. Current Stage 3 containment logic skips AP Update/Delete for any `role="local_subscriber"` surface. That remains the intended Stage 4 behavior, even for source surfaces, until Stage 5 explicitly designs participant-wide edit/delete propagation.

Mitigation: add negative edit/delete assertions for local-subscriber-originated source surfaces.

### Remote delivery ambiguity

Host-originated Discord creates currently use `publish_local_community_content()`. Stage 4 should use the same path for local-subscriber-originated creates instead of adding a second federation relay path. Adding a second path could duplicate remote delivery.

Mitigation: tests should assert only one local-community publish call for source creates and no direct `send_local_community_relay()` call from the local Discord fanout layer.

## Regression / Blind-Spot Analysis

### Regression: host forum source path can break during source branching

Refactoring `handle_discord_thread_create()` and `handle_discord_message()` into source-aware branches can accidentally change the long-standing host path.

Required regression coverage:

- existing host thread create still creates one canonical row, one host surface, and subscriber surfaces;
- existing host reply create still resolves root/nested replies and subscriber fanout as before.

### Regression: remote inbound create path can lose local-subscriber fanout

Stage 4 should not touch inbound remote source behavior, but fanout helper changes can accidentally remove local-subscriber targets for remote-originated creates.

Required regression coverage:

- Stage 3 inbound remote post/comment tests remain unchanged and green.

### Blind spot: local subscriber source without target thread surfaces

A local subscriber comment can only fan out to targets that already have the canonical thread surface. If a target missed the thread fanout earlier, comment fanout should skip that target instead of creating an orphan message.

Required test:

- missing host/sibling thread surface causes comment target skip, not orphan message creation.

### Blind spot: source surface may be created before target fanout fails

This is intentional: source surface persistence is the durable proof that the source event became canonical. Retry should use that source surface to recover missing targets.

Required test:

- simulate one target failure, then replay the same source event and verify only the missing target surface appears.

### Blind spot: forum role conflicts

Stage 1 rejects a forum hosting another local community or remote subscription when subscribing locally, but existing databases can still contain unexpected mixed state. Stage 4 routing should prefer explicit local-community host ownership over local-subscriber ownership and should not route inactive local-subscriber rows as sources.

Required test:

- host forum still resolves as `host_forum` even if bad data also contains a local subscriber row for the same channel.

## Open Questions

1. Should Stage 4 change the moderator-facing `/list-subscriptions` text to say local subscribers are now active participants?

   Recommendation: yes, if the current copy still implies read-only mirrors. This is documentation/copy only; it should not change command data shape.

2. Should local-subscriber-originated source surfaces use a new role such as `local_subscriber_source` instead of `local_subscriber`?

   Recommendation: no. Keep `role="local_subscriber"` and use the canonical row `origin_kind` plus source surface lookup to understand origin. Adding another role would complicate target selection and Stage 5 update/delete propagation without adding a necessary invariant.
