# 53 — Stage 1 local subscriber model cleanup

## Problem / Goal

The current codebase already exposes same-instance bridge communities through
the unified discovery layer from `52_unified_community_discovery.md`, but the
`source="local_bridge"` branch still stops at a placeholder message in
`src/commands/subscribe.py`:

```text
This local community can be resolved, but local channel subscriptions are not implemented yet.
```

At the same time, the local-community persistence model only has one
subscriber-like concept:

- `LocalCommunityFollower` / `local_community_followers`

That entity really means "remote ActivityPub actor following a bridge-owned
local community", but its name becomes ambiguous as soon as local Discord
subscriber forums are introduced.

Before any bidirectional local-community sync is implemented, the project needs
a clean participant vocabulary and control-plane persistence:

- `RemoteSubscriber` for remote ActivityPub participants;
- `LocalSubscriber` for same-instance Discord forum participants.

Stage 1 should only establish that model cleanup and the moderator control
surface. It must not yet change `LocalCommunityRuntime` routing, canonical
thread/message modeling, or bidirectional Discord fanout.

## Stage Boundary

### Stage 1 owns

Stage 1 owns only control-plane and naming cleanup:

- rename remote local-community participant naming:
  - `LocalCommunityFollower` -> `RemoteSubscriber`
  - `local_community_followers` -> `remote_subscribers`
- add `LocalSubscriber` / `local_subscribers`;
- let `/subscribe-channel` and `/unsubscribe-channel` create/remove local
  subscriber state for `source="local_bridge"`;
- let `/list-subscriptions` and dashboard terminology expose the split between
  remote and local participants;
- apply the required SQLite migration and compatibility work.

### Stage 1 does not own

Stage 1 must not implement or partially implement participant sync behavior:

- no `DiscordEventRouter` dispatch changes for local subscriber forums;
- no `LocalCommunityRuntime` changes that make local subscriber forums valid
  content sources;
- no local Discord fanout into subscriber forums;
- no canonical activity/surface refactor;
- no new edit/delete propagation behavior;
- no dedup redesign for multi-surface local-community activity.

### Stage 1 handoff contract

Stage 1 is complete only when these are all true:

- participant names are explicit and consistent;
- local subscriber rows can be created, listed, and removed safely;
- remote subscription/runtime behavior is unchanged;
- local-community runtime behavior is unchanged.

If implementation work requires router/runtime fanout changes to make Stage 1
pass, the boundary is wrong and the plan should be corrected before coding.

## Expected Behavior

### `/subscribe-channel` persists a local subscriber

When `/subscribe-channel` resolves `ResolvedCommunity(source="local_bridge")`:

- the command should create one `LocalSubscriber` row;
- it must not call `FedifyGatewayClient.follow_community()`;
- it must not create `BridgeActorFollow`;
- it must not create `RemoteSubscriber` for the bridge actor.

Example persisted row:

```text
local_subscribers
  local_community_id = 7
  discord_guild_id = 42
  discord_channel_id = 123456789012345678
  initiated_by_discord_user_id = "999999999999999999"
  status = active
```

Moderator-facing success message can stay simple:

```text
Subscribed #target-forum to local community **Great Community**.
```

### `/unsubscribe-channel` removes only local subscriber state

When a forum channel has a local subscriber row and no remote subscription row:

- `/unsubscribe-channel` should delete or deactivate the `LocalSubscriber` row;
- it must not call `FedifyGatewayClient.unfollow_community()`;
- it must not delete or mutate remote follow state.

### `/list-subscriptions` distinguishes remote and local entries

The listing command should no longer return one undifferentiated
`subscriptions` list. It should return separate sections:

```text
Remote community subscriptions
• #worldnews -> !worldnews@lemmy.ml

Local community subscribers
• #great-community-copy -> !great_community@bot.example.com
```

If only local subscribers exist, the command should still succeed.

### Dashboard terminology becomes participant-aware

The dashboard should stop using "followers" as the only public participant
label for local communities. Stage 1 does not need a deep unified count, but it
should expose separate read-side counts:

```json
{
  "remoteSubscriberCount": 2,
  "localSubscriberCount": 0
}
```

The dashboard can continue showing remote subscriber details only in this stage.
It does not need to render local subscriber forum details yet if that would
surface Discord-internal ids publicly.

### Runtime behavior stays unchanged in Stage 1

Creating or deleting `LocalSubscriber` rows in Stage 1 does **not** mean that
the forum immediately becomes a live community participant.

In this stage:

- `LocalCommunityRuntime` still only treats `LocalCommunity.discord_forum_channel_id`
  as a valid local Discord source;
- local subscriber forums do not yet receive synchronized posts/comments;
- router behavior for Discord events remains host-forum-only.

This boundary is intentional. Stage 1 is a control-plane cleanup, not the
bidirectional sync rollout.

## Architecture

### Keep Stage 1 narrow: model cleanup only

Do not mix these concerns into Stage 1:

- canonical local-community thread/message refactor;
- Discord surface tables;
- local subscriber fanout;
- router changes for local subscriber forums;
- local-subscriber-originated publish paths.

Those are later stages from `51_local_community_channel_subscriptions.md`.

Stage 1 should only cover:

1. participant naming cleanup;
2. local subscriber persistence;
3. command/operation branching;
4. dashboard/list output terminology;
5. schema rename and additive migration work.

The separation is strict: Stage 1 may prepare data and naming for later sync
work, but it must not smuggle in partial runtime behavior from Stage 2+.

### Rename `LocalCommunityFollower` to `RemoteSubscriber`

The current ORM/storage naming is misleading:

- model: `LocalCommunityFollower`
- table: `local_community_followers`

Stage 1 should rename them to:

- model: `RemoteSubscriber`
- table: `remote_subscribers`

This rename should flow through:

- `src/models.py`
- `src/db.py`
- `src/dashboard.py`
- `src/local_communities/federation_fanout.py`
- `fedify-gateway/src/db.ts`
- any test helpers or docs that read the table directly

The rename is not just cosmetic. It prevents Stage 2+ from carrying two
parallel naming systems for the same participant family.

### Add `LocalSubscriber`

Add a new ORM model and table:

```python
class LocalSubscriber(Base):
    """Persist one same-instance Discord forum subscribed to a local community."""

    __tablename__ = "local_subscribers"
    __table_args__ = (
        UniqueConstraint("local_community_id", "discord_channel_id"),
        UniqueConstraint("discord_channel_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    local_community_id: Mapped[int] = mapped_column(Integer, nullable=False)
    discord_guild_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    discord_channel_id: Mapped[int] = mapped_column(Integer, nullable=False)
    initiated_by_discord_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
```

Keep `discord_channel_id` globally unique in Stage 1. That matches the current
project invariant that one forum should not silently participate in multiple
bridge roles.

### Repository helpers should follow participant terminology

`src/db.py` should stop introducing new helpers with legacy follower wording.

Remote participant helpers should become:

```python
create_remote_subscriber(...)
get_remote_subscriber(...)
list_remote_subscribers(...)
list_remote_subscribers_for_all(...)
delete_remote_subscriber(...)
```

Local participant helpers should be added as:

```python
create_local_subscriber(...)
get_local_subscriber(...)
get_local_subscriber_by_channel(...)
list_local_subscribers(...)
list_local_subscribers_by_guild(...)
delete_local_subscriber(...)
count_local_subscribers(...)
```

The old helper names can remain as temporary wrappers only if necessary to keep
Stage 1 reviewable, but the plan target should be the renamed API.

### Stage 1 command branching should live at the operation layer

The command adapter in `src/commands/subscribe.py` already resolves a typed
`ResolvedCommunity`. Stage 1 should stop returning a placeholder for
`source="local_bridge"` and delegate to a dedicated local operation.

Recommended split:

```text
src/operations/subscribe.py
  existing remote lifecycle

src/operations/subscribe_local_community.py
  local subscriber persistence only

src/operations/unsubscribe.py
  existing remote lifecycle

src/operations/unsubscribe_local_community.py
  local subscriber delete/deactivate only
```

The slash-command surface stays unchanged:

- `/subscribe-channel`
- `/unsubscribe-channel`
- `/list-subscriptions`

### Database migration must use the current project pattern

This repo does not use Alembic. Schema evolution is done in `Database.migrate()`
with manual idempotent SQL.

Stage 1 migration work should include:

1. rename table `local_community_followers` -> `remote_subscribers`;
2. create `local_subscribers` if missing;
3. keep runtime-safe compatibility for existing deployed databases.

Because SQLite is in use, the rename should be explicit and idempotent. The
plan should not assume fresh databases only.

### Gateway readers must follow the rename

The gateway currently reads accepted local-community follower rows through
`fedify-gateway/src/db.ts` using SQL against `local_community_followers`.

Stage 1 should rename that reader to match the new concept, for example:

```ts
loadAcceptedRemoteSubscribersByActorUrl(...)
```

and change the SQL to read from `remote_subscribers`.

The surrounding federation behavior stays the same; only the participant naming
and DB contract change in this stage.

## Touched Files

```text
README.md
FEDERATION.md
src/models.py
src/db.py
src/dashboard.py
src/commands/subscribe.py
src/commands/unsubscribe.py
src/commands/list_subs.py
src/operations/subscribe.py
src/operations/unsubscribe.py
src/operations/list_subscriptions.py
src/local_communities/federation_fanout.py
src/local_communities/README.md
fedify-gateway/src/db.ts
fedify-gateway/src/federation-outbound.ts
fedify-gateway/README.md
docs/architecture/database-map.md
docs/architecture/bridge-modes.md
docs/development/navigation.md
tests/behavior/test_dashboard_scenarios.py
tests/commands/test_subscribe_command.py
tests/behavior/test_subscription_scenarios.py
tests/behavior/test_unsubscribe_retry_scenarios.py
```

## New Files

```text
src/operations/subscribe_local_community.py
src/operations/unsubscribe_local_community.py
tests/behavior/test_local_subscriber_stage1_scenarios.py
```

## Implementation Steps

### 1. Add failing behavior tests first

Write Stage 1 behavior tests before code changes.

Required scenarios:

1. `/subscribe-channel` persists a local subscriber for `source="local_bridge"`.
   - Given a registered moderator, a bridge-owned local community, and a target forum.
   - Given `ResolvedCommunity(source="local_bridge")`.
   - When `/subscribe-channel` runs.
   - Then one `LocalSubscriber` row exists.
   - Then no `BridgeActorFollow` row exists for that community.
   - Then no `RemoteSubscriber` row exists for the bridge actor.

2. `/subscribe-channel` rejects the host forum as a local subscriber target.
   - Given the target forum equals `LocalCommunity.discord_forum_channel_id`.
   - When the moderator subscribes it.
   - Then the operation rejects with a clear moderator-facing reason.

3. `/unsubscribe-channel` removes only local subscriber state.
   - Given a channel has a `LocalSubscriber` row and no remote subscription row.
   - When `/unsubscribe-channel` runs.
   - Then the `LocalSubscriber` row is gone or inactive.
   - Then no gateway `unfollow_community()` call happens.

4. `/list-subscriptions` renders separate remote and local sections.
   - Given one remote subscription and one local subscriber.
   - When the command runs.
   - Then both are visible in distinct sections.

5. Dashboard payload exposes separate remote/local subscriber counts.
   - Given one local community with accepted `RemoteSubscriber` rows and zero `LocalSubscriber` rows.
   - Then `remoteSubscriberCount` is non-zero and `localSubscriberCount` is zero.

### 2. Rename `LocalCommunityFollower` to `RemoteSubscriber`

Update `src/models.py` and `src/db.py` first.

Concrete changes:

- rename ORM class `LocalCommunityFollower` -> `RemoteSubscriber`;
- rename table `local_community_followers` -> `remote_subscribers`;
- rename repository helpers and call sites;
- keep comments/docstrings explicit that this is remote ActivityPub participant
  state owned by Python moderation/runtime logic.

### 3. Add `LocalSubscriber`

Add the ORM model in `src/models.py` and repository helpers in `src/db.py`.

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

These helpers must be idempotent where retry semantics matter, especially for
subscribe command retries.

### 4. Implement Stage 1 local subscribe/unsubscribe operations

Add:

- `src/operations/subscribe_local_community.py`
- `src/operations/unsubscribe_local_community.py`

`subscribe_local_community` should:

- require moderator registration if subscribe actions still require it;
- verify the target channel is not the host forum;
- verify the target channel has no remote subscription row;
- verify the target channel is not already a local subscriber;
- create `LocalSubscriber(status="active")`.

`unsubscribe_local_community` should:

- resolve local subscriber state by channel id;
- delete or deactivate the row;
- never dispatch remote Undo(Follow).

### 5. Replace the `local_bridge` placeholder in `/subscribe-channel`

Update `src/commands/subscribe.py` so:

- `source="remote_lemmy"` and `source="remote_bridge"` keep the existing
  remote subscribe path;
- `source="local_bridge"` delegates to `subscribe_local_community`.

Do the analogous branching for `/unsubscribe-channel`.

### 6. Split `list-subscriptions` into remote and local sections

Update `ListSubscriptionsInput`, `list_subscriptions_operation`, and
`src/commands/list_subs.py`.

Target result shape:

```python
OperationResult(
    applied=True,
    message="Loaded active subscriptions.",
    extra_kwargs={
        "remote_subscriptions": remote_rows,
        "local_subscribers": local_rows,
    },
)
```

The command adapter should render separate sections and preserve the existing
empty-state behavior when neither list has rows.

### 7. Update dashboard terminology and payload

Update `src/dashboard.py` to:

- rename follower-facing payload fields to subscriber terminology where safe;
- add `remoteSubscriberCount`;
- add `localSubscriberCount`;
- keep the existing remote-subscriber detail disclosure working.

Do not add local forum ids to the public payload unless the design is clearly
safe and intended. Stage 1 only needs aggregate local-subscriber count.

### 8. Update gateway readers and docs

Update `fedify-gateway/src/db.ts` and `fedify-gateway/src/federation-outbound.ts`
to use the renamed `remote_subscribers` table and reader naming.

Update only relevant docs by purpose:

- database map for new/renamed tables;
- bridge modes for participant naming;
- navigation docs for the new operation files;
- local-community README/federation docs where they currently say "followers"
  but mean "remote subscribers".

## Tests

Follow TDD. Run at least:

```bash
pytest tests/behavior/test_local_subscriber_stage1_scenarios.py
pytest tests/commands/test_subscribe_command.py
pytest tests/behavior/test_subscription_scenarios.py
pytest tests/behavior/test_unsubscribe_retry_scenarios.py
pytest tests/behavior/test_dashboard_scenarios.py
cd fedify-gateway && npm test
```

The tests must verify:

- real DB rows for `RemoteSubscriber` and `LocalSubscriber`;
- no accidental remote follow/unfollow dispatch on local subscribe/unsubscribe;
- command-visible sectioning in `/list-subscriptions`;
- dashboard payload field changes.

## Compatibility Risks / Regression Analysis

### SQLite rename risk

Renaming `local_community_followers` to `remote_subscribers` affects both
Python and gateway readers. A partial migration would break remote relay reads
or dashboard counts. `Database.migrate()` must be explicit and idempotent.

### Remote follow lifecycle regression

Stage 1 must not change the behavior of:

- `BridgeActorFollow`
- `ChannelCommunitySubscription`
- remote subscribe/unsubscribe dispatch

Remote community flows must continue to pass unchanged.

### Dashboard contract churn

`src/dashboard.py` and `tests/behavior/test_dashboard_scenarios.py` currently
use `subscriberCount` as a remote-follower count. Stage 1 must update labels
carefully without implying that local subscribers already participate in live
sync.

### Premature runtime coupling

Stage 1 must not partially wire `LocalSubscriber` rows into
`DiscordEventRouter` or `LocalCommunityRuntime`. Doing so without the later
surface-model refactor would create ambiguous host-vs-subscriber source
behavior and brittle dedup.

## Open Questions

1. Should Stage 1 expose local subscriber forum mentions in `/list-subscriptions` only, or also on the public dashboard?

   Recommendation: only in `/list-subscriptions`. Keep the public dashboard on
   aggregate count only for now.

2. Should the old `LocalCommunityFollower` helper names remain temporarily as wrappers during the rename?

   Recommendation: yes, only if needed to keep Stage 1 reviewable and avoid a
   giant all-at-once diff. But the target API and model names should still be
   `RemoteSubscriber`.
