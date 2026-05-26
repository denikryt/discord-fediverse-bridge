# 51 — Local community local subscribers

## Problem / Goal

The current local-community mode assumes one canonical local Discord surface:

- `LocalCommunity.discord_forum_channel_id` is the host forum;
- `LocalCommunityRuntime` only treats that host forum as a valid local source;
- `LocalCommunityThread` and `LocalCommunityMessage` each embed one Discord thread/message mapping, so one ActivityPub object currently maps to one canonical local Discord surface;
- remote federation fanout is modeled separately through `local_community_followers`.

That shape is sufficient for:

- host forum -> ActivityPub;
- remote subscriber -> host forum;
- remote subscriber -> other remote subscribers.

It is not sufficient for this target behavior:

```text
host forum, local subscriber forums, and remote subscribers are all participants
in the same local community.
```

Specifically, a local subscriber forum must not be a read-only mirror. It must
behave like another community participant:

- a post created in a local subscriber forum is a real community post;
- a comment created in a local subscriber forum is a real community comment;
- that activity must synchronize to the host forum, other local subscriber
  forums, and remote subscribers;
- inbound remote subscriber activity must still synchronize into the host forum
  and all local subscriber forums;
- updates and deletes must follow the same participant-wide sync rules.

The goal of this plan is to extend local-community mode from:

```text
host forum -> mirror copies
```

to:

```text
community participants -> synchronized activity across all participant surfaces
```

The unified `/subscribe-channel` discovery work from
`52_unified_community_discovery.md` remains the entry point for adding
same-instance local subscribers. This plan only changes what the resolved
`source="local_bridge"` branch means at runtime and in persistence.

## Expected Behavior

### One `/subscribe-channel` command adds a local subscriber

Moderators should subscribe a Discord forum to a bridge-owned local community
through the existing command:

```text
/subscribe-channel instance_domain:<bridge origin> community:<local community choice/url/handle> channel:<forum channel>
```

Expected branch behavior:

```text
ResolvedCommunity(source="remote_lemmy")
  -> existing remote subscribe path

ResolvedCommunity(source="remote_bridge")
  -> existing remote subscribe path

ResolvedCommunity(source="local_bridge")
  -> create LocalSubscriber
  -> do not send ActivityPub Follow
  -> do not create BridgeActorFollow
  -> do not create RemoteSubscriber for the bridge actor
```

The operation should reject:

- target channel equal to the host forum;
- target channel already subscribed remotely through
  `channel_community_subscriptions`;
- target channel already subscribed locally to the same community;
- target channel already hosting another local community, unless a later plan
  explicitly designs multi-role forums.

### One `/unsubscribe-channel` command removes a local subscriber

Expected branch behavior:

```text
channel has remote subscription only
  -> existing remote unsubscribe path

channel has local subscriber only
  -> delete/deactivate LocalSubscriber
  -> do not send ActivityPub Undo(Follow)
```

Unsubscribe must stop future synchronization into that forum, but it does not
need to retroactively delete already created thread/message surfaces.

### Host forum activity synchronizes to all other participants

When a user creates a post or comment in the host forum:

```text
host forum activity
  -> publish one ActivityPub Create/Update/Delete as today
  -> sync to every other local subscriber forum
  -> relay to remote subscribers according to existing federation policy
```

The source host forum must not receive a duplicate local copy back.

### Local subscriber activity is first-class community activity

When a user creates a post or comment in a local subscriber forum:

```text
local subscriber activity
  -> publish one ActivityPub Create/Update/Delete for the local community
  -> sync to the host forum
  -> sync to every other local subscriber forum
  -> relay to remote subscribers
```

The source local subscriber forum must not receive a duplicate local copy back.

This is the critical change from the earlier mirror-only design:
local-subscriber-originated activity is canonical community activity, not a
read-only echo surface.

### Remote subscriber activity still fans out locally and remotely

When an accepted remote subscriber posts or comments into a local community:

```text
remote subscriber activity
  -> sync to the host forum
  -> sync to every local subscriber forum
  -> relay to other remote subscribers, excluding the origin actor
```

This preserves the current remote relay semantics while extending local
participant coverage beyond the host forum.

### Updates and deletes propagate across all participant types

For a post/comment created by any participant type:

- host forum edit/delete -> other local subscribers + remote subscribers;
- local subscriber edit/delete -> host forum + other local subscribers + remote subscribers;
- remote subscriber update/delete -> host forum + local subscribers + other remote subscribers.

The implementation must use durable per-surface mappings so retries and
late-arriving updates/deletes can target the correct Discord thread/message
surfaces.

### `/list-subscriptions` distinguishes participant types

The listing command should keep remote community subscriptions separate from
local community subscribers. Example:

```text
Remote community subscriptions
• #worldnews -> !worldnews@lemmy.ml

Local community subscribers
• #great-community-copy -> Great Community (!great_community@bot.example.com)
• #great-community-backup -> Great Community (!great_community@bot.example.com)
```

### Dashboard terminology stays explicit

The dashboard does not need a deep unified persisted subscriber count in this
plan. If it needs a total later, it can compute:

```text
total participants visible on dashboard
  = remote subscriber count + local subscriber count
```

Keep the labels explicit instead of storing one merged subscriber count in the
core runtime model.

## Architecture

### Keep `LocalCommunity` as the host-forum anchor

Do not introduce a separate `HostForumParticipant` entity.

The current `LocalCommunity` row already owns:

- the community slug and display metadata;
- the ActivityPub actor identity and key material;
- the host forum channel id.

That row should remain the anchor that says:

```text
this Discord forum is the home surface for this local community.
```

The host forum is special because it anchors community metadata and moderation,
but it is not the only valid local source of community activity after this
plan.

### Use explicit participant entity names

This plan should use two subscriber entity names end to end:

- `RemoteSubscriber`: a remote ActivityPub actor following a bridge-owned local community.
- `LocalSubscriber`: a local Discord forum channel subscribed to a bridge-owned local community.

These names should be used for:

- ORM model names;
- repository helper names;
- operation DTO names;
- inline comments/docstrings;
- dashboard payload labels;
- moderation and command messages.

The current `LocalCommunityFollower` name is misleading once local subscribers
exist. Rename it to `RemoteSubscriber` and rename the table from
`local_community_followers` to `remote_subscribers`.

Add the new local participant table as `local_subscribers`.

Do not add one generic mixed subscriber table. Remote subscribers and local
subscribers have different identities, different fanout targets, and different
failure modes.

### Refactor canonical activity to be community-centric, not host-surface-centric

The biggest architectural blocker in the current code is that
`LocalCommunityThread` and `LocalCommunityMessage` embed one Discord surface:

- `LocalCommunityThread.discord_thread_id`
- `LocalCommunityThread.discord_starter_message_id`
- `LocalCommunityMessage.discord_message_id`
- `LocalCommunityMessage.parent_discord_message_id`

That model assumes each community activity has exactly one canonical Discord
thread/message surface. Bidirectional local subscribers break that assumption.

This plan should therefore refactor local-community activity into two layers:

1. canonical community activity rows
2. per-participant Discord surface rows

#### Canonical activity rows

Keep `LocalCommunityThread` and `LocalCommunityMessage` as the canonical
community activity tables, but change their responsibility:

- one row per canonical local-community post/comment;
- one stable `ap_object_id` / `ap_activity_id` per activity;
- one source participant description for origin tracking;
- no assumption that one Discord thread/message is the canonical owner.

Suggested direction:

```python
class LocalCommunityThread(Base):
    """Persist one canonical local-community post independent of Discord surface."""

    __tablename__ = "local_community_threads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    local_community_id: Mapped[int] = mapped_column(Integer, nullable=False)
    ap_activity_id: Mapped[str] = mapped_column(String(512), nullable=False)
    ap_object_id: Mapped[str] = mapped_column(String(512), nullable=False)
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_remote_actor_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    source_discord_forum_channel_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_discord_thread_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_discord_starter_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
```

```python
class LocalCommunityMessage(Base):
    """Persist one canonical local-community comment independent of Discord surface."""

    __tablename__ = "local_community_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    local_community_thread_id: Mapped[int] = mapped_column(Integer, nullable=False)
    ap_activity_id: Mapped[str] = mapped_column(String(512), nullable=False)
    ap_object_id: Mapped[str] = mapped_column(String(512), nullable=False)
    parent_ap_object_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_remote_actor_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    source_discord_forum_channel_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_discord_thread_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_discord_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
```

`source_kind` should be an explicit invariant such as:

```text
host_forum
local_subscriber
remote_subscriber
```

#### Discord surface rows

Add separate tables for Discord surfaces created or recognized for each
canonical activity:

```text
local_community_thread_surfaces
local_community_message_surfaces
```

Suggested responsibilities:

- map one canonical post/comment to one Discord forum/thread/message surface;
- record whether the surface is the host forum or one specific local subscriber;
- support idempotent local sync and later update/delete targeting;
- let router/runtime resolve any Discord-originating event back to the canonical
  community activity.

Suggested shapes:

```python
class LocalCommunityThreadSurface(Base):
    """Map one canonical local-community post to one Discord thread surface."""

    __tablename__ = "local_community_thread_surfaces"
    __table_args__ = (
        UniqueConstraint("local_community_thread_id", "discord_forum_channel_id"),
        UniqueConstraint("discord_thread_id"),
        UniqueConstraint("discord_starter_message_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    local_community_thread_id: Mapped[int] = mapped_column(Integer, nullable=False)
    local_community_id: Mapped[int] = mapped_column(Integer, nullable=False)
    surface_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    local_subscriber_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    discord_forum_channel_id: Mapped[int] = mapped_column(Integer, nullable=False)
    discord_thread_id: Mapped[int] = mapped_column(Integer, nullable=False)
    discord_starter_message_id: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
```

```python
class LocalCommunityMessageSurface(Base):
    """Map one canonical local-community comment to one Discord message surface."""

    __tablename__ = "local_community_message_surfaces"
    __table_args__ = (
        UniqueConstraint("local_community_message_id", "discord_forum_channel_id"),
        UniqueConstraint("discord_message_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    local_community_message_id: Mapped[int] = mapped_column(Integer, nullable=False)
    local_community_thread_id: Mapped[int] = mapped_column(Integer, nullable=False)
    local_community_id: Mapped[int] = mapped_column(Integer, nullable=False)
    surface_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    local_subscriber_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    discord_forum_channel_id: Mapped[int] = mapped_column(Integer, nullable=False)
    discord_thread_id: Mapped[int] = mapped_column(Integer, nullable=False)
    discord_message_id: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_discord_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
```

This surface split is the cleanest way to support:

- one canonical community activity;
- many Discord participant surfaces;
- updates/deletes/retries without guessing.

### Keep remote federation fanout separate from local Discord fanout

Do not collapse local and remote fanout into one class.

Keep a split like:

- `LocalCommunityRemoteSubscriberFanout`
  - target selection for `RemoteSubscriber`
  - ActivityPub relay rendering and gateway delivery
- `LocalCommunityDiscordFanout`
  - target selection for host forum + local subscribers
  - Discord thread/message creation, edit, delete

Both fanout services should consume the same canonical activity rows and the
same participant-aware routing context.

### Router must resolve local community context from host or local subscriber forum

`DiscordEventRouter` currently only knows:

- host forum local-community source;
- remote-subscription forum source.

That is no longer enough. It needs a participant-aware lookup such as:

```python
ResolvedLocalCommunityDiscordSurface(
    local_community_id=...,
    source_kind="host_forum" | "local_subscriber",
    local_subscriber_id=... | None,
    discord_forum_channel_id=...,
)
```

The router should:

- resolve a community context for both host forum and local subscriber forums;
- send both through `LocalCommunityRuntime`;
- stop treating local subscriber forums as generic remote-subscription or
  default-community-sync forums.

### Migration strategy must match the current project

This repo does not use Alembic. Schema changes go through:

- `Base.metadata.create_all()`
- `Database.migrate()` additive/manual SQL

So the plan must use explicit migration code, not abstract “run a migration”.

Required migration work:

1. rename `local_community_followers` -> `remote_subscribers`;
2. create `local_subscribers`;
3. create `local_community_thread_surfaces`;
4. create `local_community_message_surfaces`;
5. backfill one host-forum surface row for every existing
   `LocalCommunityThread`;
6. backfill one host-forum surface row for every existing
   `LocalCommunityMessage`.

Because SQLite column-drop is awkward and the existing runtime is already wired
to the old embedded Discord ids, the first implementation may keep the legacy
Discord columns on `local_community_threads` and `local_community_messages` as
deprecated compatibility fields during the transition. New code should stop
treating those fields as the only local surface.

## Touched Files

```text
README.md
FEDERATION.md
src/db.py
src/models.py
src/dashboard.py
src/discord_event_router.py
src/commands/subscribe.py
src/commands/unsubscribe.py
src/commands/list_subs.py
src/operations/subscribe.py
src/operations/unsubscribe.py
src/operations/list_subscriptions.py
src/local_communities/runtime.py
src/local_communities/delivery_mapping.py
src/local_communities/reply_mapping.py
src/local_communities/federation_fanout.py
src/local_communities/README.md
fedify-gateway/src/db.ts
fedify-gateway/src/server.ts
fedify-gateway/src/types.ts
fedify-gateway/README.md
docs/architecture/bridge-modes.md
docs/architecture/database-map.md
docs/architecture/event-flows.md
docs/development/navigation.md
notes/known_issues.md
```

## New Files

```text
src/local_communities/discord_fanout.py
src/local_communities/participant_routing.py
src/operations/subscribe_local_community.py
src/operations/unsubscribe_local_community.py
tests/behavior/test_local_community_participant_sync_scenarios.py
```

## Implementation Steps

### 1. Add failing behavior tests first

Write runtime/behavior tests before implementation. Required scenarios:

1. Moderator subscribes a forum as a local subscriber through `/subscribe-channel`.
   - Given a registered moderator and an existing local community.
   - Given plan 52 resolves `source="local_bridge"`.
   - When they subscribe forum B.
   - Then a `LocalSubscriber` row exists.
   - Then no `BridgeActorFollow` row exists for that community.
   - Then no `RemoteSubscriber` row exists for the bridge actor.

2. Host forum thread synchronizes to local subscribers and remote subscribers.
   - Given community A has local subscribers B and C and remote subscribers R1 and R2.
   - When a thread is created in host forum A.
   - Then the post is published once.
   - Then B and C receive local Discord thread surfaces.
   - Then R1 and R2 receive federation relay according to current remote fanout rules.

3. Local subscriber thread synchronizes to host forum, other local subscribers, and remote subscribers.
   - Given B and C are local subscribers for community A.
   - When a thread is created in B.
   - Then host forum A receives a thread surface.
   - Then C receives a thread surface.
   - Then remote subscribers receive federation relay.
   - Then B does not receive a duplicate copy of its own source thread.

4. Host forum reply synchronizes everywhere except the source surface.
   - Given a canonical thread already exists with host + local subscriber surfaces.
   - When a reply is created in the host surface.
   - Then local subscribers receive matching replies.
   - Then remote subscribers receive one federated comment relay.

5. Local subscriber reply synchronizes everywhere except the source surface.
   - Given a canonical thread already exists with host + local subscriber surfaces.
   - When a reply is created in local subscriber B.
   - Then host forum and local subscriber C receive matching replies.
   - Then remote subscribers receive one federated comment relay.

6. Inbound remote subscriber post synchronizes to host forum, local subscribers, and other remote subscribers.
   - Given community A has host forum, local subscribers, and accepted remote subscribers.
   - When a remote subscriber posts into A.
   - Then host forum receives a thread surface.
   - Then every local subscriber receives a thread surface.
   - Then relay fanout excludes the origin remote actor and targets only other accepted remote subscribers.

7. Inbound remote subscriber comment synchronizes to every non-origin participant.
   - Given a canonical thread already exists.
   - When a remote comment arrives.
   - Then host forum and all local subscribers receive the mapped reply.
   - Then relay fanout excludes the origin remote actor.

8. Edit/delete from a local subscriber propagates across all other surfaces and remote subscribers.
   - Given one canonical activity with host and local subscriber surfaces.
   - When the source local subscriber edits or deletes it.
   - Then host forum and other local subscribers are updated/deleted.
   - Then remote relay update/delete targets only participants that previously received the create.

9. Router treats local subscriber forums as local-community sources.
   - Given forum B is a local subscriber for community A.
   - When a thread/message event arrives from B.
   - Then the router dispatches it into `LocalCommunityRuntime`, not `CommunityRuntime`.

10. Duplicate retries do not create extra Discord surfaces.
    - Given the same inbound or local event is processed twice.
    - Then canonical activity row count stays stable.
    - Then surface row count stays stable.
    - Then remote relay create fanout stays idempotent.

### 2. Rename remote follower persistence to `RemoteSubscriber`

Update `src/models.py`, `src/db.py`, gateway DB readers, and all local-community
docs/tests so the existing remote-follower entity becomes:

- model name: `RemoteSubscriber`
- table name: `remote_subscribers`

Add explicit migration SQL in `Database.migrate()` to rename the table for
existing SQLite databases.

### 3. Add `LocalSubscriber`

Add the new `LocalSubscriber` ORM model in `src/models.py` and repository
helpers in `src/db.py`.

Required helpers:

```python
create_local_subscriber(...)
get_local_subscriber(...)
get_local_subscriber_by_channel(discord_channel_id: int)
list_local_subscribers(local_community_id: int)
list_local_subscribers_by_guild(discord_guild_id: int)
delete_local_subscriber(discord_channel_id: int)
count_local_subscribers(local_community_id: int)
```

### 4. Add participant-aware surface tables

Add `LocalCommunityThreadSurface` and `LocalCommunityMessageSurface`.

Required helpers:

```python
create_local_community_thread_surface(...)
get_local_community_thread_surface_by_discord_thread(discord_thread_id: int)
get_local_community_thread_surface(local_community_thread_id: int, discord_forum_channel_id: int)
list_local_community_thread_surfaces(local_community_thread_id: int)

create_local_community_message_surface(...)
get_local_community_message_surface_by_discord_message(discord_message_id: int)
get_local_community_message_surface(local_community_message_id: int, discord_forum_channel_id: int)
list_local_community_message_surfaces(local_community_message_id: int)
```

These helpers must be idempotent because they sit on retry-sensitive runtime
paths.

### 5. Refactor canonical local-community activity rows

Refactor `LocalCommunityThread` and `LocalCommunityMessage` so they describe one
canonical community activity plus its source participant metadata, not one
canonical Discord surface.

Keep backwards-compatible migration notes explicit:

- backfill host-forum surface rows from existing legacy columns;
- stop adding new logic that assumes `discord_thread_id` or `discord_message_id`
  on the canonical row is the only local surface.

### 6. Add participant-aware routing helpers

Create `src/local_communities/participant_routing.py`.

Responsibility:

- resolve whether a forum channel is:
  - the host forum for a local community;
  - a local subscriber forum for a local community;
  - neither.
- resolve whether a Discord thread/message belongs to:
  - a host forum surface;
  - a local subscriber surface;
  - neither.

This helper should become the routing boundary used by
`DiscordEventRouter` and `LocalCommunityRuntime`.

### 7. Implement `LocalCommunityDiscordFanout`

Create `src/local_communities/discord_fanout.py`.

Responsibility:

- create host/local-subscriber thread surfaces for canonical posts;
- create host/local-subscriber message surfaces for canonical comments;
- exclude the source local surface from target selection;
- update/delete all non-source Discord surfaces for a canonical activity;
- persist per-surface rows for idempotency and later edit/delete targeting.

This service is local-Discord-only. It must not perform ActivityPub relay.

### 8. Refactor `LocalCommunityRuntime` around canonical activity + surfaces

`LocalCommunityRuntime` must become source-kind aware:

- `host_forum`
- `local_subscriber`
- `remote_subscriber`

Required behavior:

- resolve the local community from either host forum or local subscriber forum;
- create canonical thread/message rows for source activities from any participant type;
- create or reuse host/local subscriber Discord surfaces through
  `LocalCommunityDiscordFanout`;
- invoke remote relay through the renamed remote-subscriber fanout;
- route edit/delete using canonical activity plus surface mappings, not
  hard-coded host-forum-only assumptions.

### 9. Keep remote relay fanout semantics but retarget it to canonical activity

Refactor `src/local_communities/federation_fanout.py` so it reads the renamed
`RemoteSubscriber` model and uses canonical activity rows plus the existing
relay delivery tables.

Critical invariant:

- local subscriber-originated activity is federated exactly once;
- remote-originated relay excludes the origin remote actor;
- update/delete relay targets only remote subscribers that successfully
  received the original create.

### 10. Update commands and operations

Keep the existing command surface:

- `/subscribe-channel`
- `/unsubscribe-channel`
- `/list-subscriptions`

Required command behavior:

- `source="local_bridge"` -> local subscriber operations;
- `source="remote_lemmy"` / `source="remote_bridge"` -> unchanged remote paths;
- listing renders local subscribers separately from remote subscriptions.

### 11. Update dashboard and docs

Dashboard:

- expose `remote_subscriber_count`;
- expose `local_subscriber_count`;
- optionally expose local-subscriber rows for operator visibility;
- do not store one merged deep subscriber count.

Docs:

- bridge modes: local subscribers are full local-community participants, not mirror-only copies;
- database map: add `remote_subscribers`, `local_subscribers`, and surface tables;
- event flows: add host/local/remote participant flows in all directions;
- navigation docs: point to runtime, routing, surfaces, and fanout files;
- federation docs: explain that local subscribers do not use self-follow.

## Stage implementation boundary notes

Stage 3 implements host-originated and remote-originated create fanout into active local subscriber Discord surfaces. Stage 4 implements local-subscriber-originated post/comment creates as first-class canonical community activity. Participant-wide edit/delete propagation remains Stage 5.

## Tests

Follow TDD. Run at least:

```bash
pytest tests/behavior/test_local_community_participant_sync_scenarios.py
pytest tests/behavior/test_local_community_publish_scenarios.py
pytest tests/behavior/test_local_community_inbound_scenarios.py
pytest tests/behavior/test_local_community_edit_delete_scenarios.py
pytest tests/behavior/test_subscription_scenarios.py
pytest tests/behavior/test_dashboard_scenarios.py
pytest
```

The tests must assert:

- canonical activity rows;
- participant surface rows;
- router dispatch behavior;
- Discord fake observable effects;
- remote relay targeting and dedup behavior.

Do not rely mainly on “mock was called” tests.

## Compatibility Risks / Regression Analysis

### Canonical-mapping refactor risk

The current runtime and helpers assume one Discord surface per canonical
local-community activity. Refactoring to surfaces is the deepest risk in this
plan. The migration must preserve all existing host-forum behavior before
enabling local-subscriber-originated source events.

### Router regression risk

`DiscordEventRouter` currently only recognizes host forums as local-community
sources. If local subscriber forums are not resolved before fallback routing,
their events will leak into the wrong runtime and create duplicate or malformed
publishes.

### Echo/dedup risk

Once local subscriber forums become valid sources, the old “ignore copy forums”
assumption is gone. Dedup must be based on canonical activity ids and persisted
surface mappings, not on one-way mirror assumptions.

### Migration risk without Alembic

This project uses manual `Database.migrate()` logic. Table renames and backfill
must be explicit and idempotent. A partial migration could leave databases with
mixed old/new table names or missing surface rows.

### Dashboard semantics

Remote subscribers and local subscribers are different participant types. The
dashboard must not silently relabel one as the other or collapse them into one
stored count in the core runtime model.

### Existing remote subscription behavior

Do not change `channel_community_subscriptions` semantics or
`BridgeActorFollow` lifecycle for external communities. Remote subscribe and
unsubscribe behavior must continue to pass unchanged.

## Open Questions

1. Should local subscriber forums be allowed to host their own separate local community while also subscribing to another one?

   Recommendation: no. Keep one forum -> one bridge role until multi-role
   routing is explicitly designed.

2. Should unsubscribe ever delete existing surface rows for already synchronized history?

   Recommendation: no. Stop future fanout, keep existing historical mappings so
   edits/deletes and audit/debugging remain possible.

## Suggested Implementation Stages

### Stage 1 — Subscriber model cleanup

- rename `LocalCommunityFollower` / `local_community_followers` to
  `RemoteSubscriber` / `remote_subscribers`;
- add `LocalSubscriber` / `local_subscribers`;
- update DB helpers, command messages, list output, dashboard labels, and docs
  to use subscriber terminology;
- keep existing host-only local-community runtime behavior unchanged.

### Stage 2 — Surface model refactor

- add `local_community_thread_surfaces` and
  `local_community_message_surfaces`;
- refactor `LocalCommunityThread` and `LocalCommunityMessage` into canonical
  community activity rows instead of one-Discord-surface rows;
- backfill host-forum surfaces for existing rows;
- keep host-forum-only behavior working through the new surface model before
  adding new participant sync paths.

### Stage 3 — Host/remote to local-subscriber sync

- add local Discord fanout from host forum activity to all local subscribers;
- add local Discord fanout from inbound remote subscriber activity to all local
  subscribers;
- keep local subscriber forums as non-source surfaces in this stage;
- verify idempotent surface creation and no regression in existing remote relay.

### Stage 4 — Local-subscriber-originated activity

- make active local subscriber forums valid local-community create sources in
  `DiscordEventRouter` and `LocalCommunityRuntime`;
- publish local-subscriber-originated post/comment activity into canonical
  community rows through the existing local-community publish path;
- create source `role="local_subscriber"` surfaces and fan out missing host and
  sibling local-subscriber surfaces;
- exclude the origin local subscriber surface from local copy fanout;
- keep edit/delete propagation deferred to Stage 5.

### Stage 5 — Edit/delete and retry hardening

- extend participant-wide update/delete propagation for host, local-subscriber,
  and remote-subscriber origins;
- verify duplicate retry suppression across canonical rows and surface rows;
- verify partial-failure handling for missing Discord surfaces and failed remote
  relay targets;
- finish docs and known-issues updates for any intentionally deferred behavior.
