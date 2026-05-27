# 60 — Database repository split umbrella

## Problem / Goal

`src/db.py` has grown into a single persistence facade with more than two
thousand lines and roughly one hundred twenty `Database` methods. It currently
owns every persistence concern in the Python bridge:

- engine/session ownership;
- schema creation and migration checks;
- legacy Lemmy post/comment mappings;
- ActivityPub event receipts;
- remote community subscription state;
- registration and user state;
- bridge-owned local communities;
- remote and local local-community subscribers;
- local-community canonical post/comment rows;
- local-community Discord surface rows;
- local-community federation relay delivery tracking;
- published ActivityPub objects and source message mappings;
- remote actor cache;
- Discord fanout thread/message groups;
- bridge actor follows.

This makes navigation hard and makes every persistence change feel like an
edit to one global module. The goal of this umbrella plan is to split the
persistence layer into explicit domain repositories while preserving one
`Database` facade as the owner of the SQLAlchemy engine, session factory,
schema bootstrap, and migration entry point.

The refactor should improve structure without changing runtime behavior. It is
not a data-model redesign and must not introduce new bridge semantics.

## Expected Behavior

Runtime behavior should remain unchanged during the split:

```text
existing command/runtime/dashboard/gateway behavior
  -> same DB rows
  -> same dedup decisions
  -> same fanout target selection
  -> same public/internal API behavior
```

The desired final shape is:

```text
Database
  owns engine/session/create_all/migrate
  exposes domain repositories
  optionally keeps temporary forwarding wrappers during staged migration
```

Example final usage after call-site migration:

```python
runtime.database.local_community_surfaces.create_thread_surface(...)
runtime.database.remote_subscriptions.get_by_channel(...)
runtime.database.bridge_actor_follows.mark_accepted(...)
```

Temporary forwarding wrappers are allowed only when a stage explicitly needs a
compatibility bridge between old call sites and new repositories. They should
be tracked as short-lived cleanup work, not left as permanent duplicate APIs.

Every stage must keep the full Python test suite green. These wrappers are not
project-level backward compatibility features; they are refactor scaffolding so
repository extraction can happen in reviewable steps without breaking existing
runtime call sites. Stage 8 must remove the temporary dual API and leave one
supported persistence interface for domain operations: repository properties on
`Database`.

## Architecture

### Keep one session owner

Do not create independent engines or independent session factories per
repository. The `Database` object should continue to own:

```text
engine
SessionLocal
create_all()
migrate()
session()
```

Repository classes should receive the same session factory or a narrow session
provider from `Database`.

This preserves transaction/session behavior and avoids subtle bugs where two
repositories accidentally operate through unrelated sessions.

### Split by bounded persistence domain

The split should follow existing domain boundaries, not arbitrary line counts.
Recommended package shape:

```text
src/db/
  __init__.py
  database.py
  schema.py
  migrations.py
  repositories/
    legacy_lemmy_mappings.py
    event_receipts.py
    remote_subscriptions.py
    users.py
    local_communities.py
    remote_subscribers.py
    local_subscribers.py
    local_community_content.py
    local_community_surfaces.py
    local_community_relay.py
    activitypub_objects.py
    remote_actors.py
    discord_fanout_groups.py
    bridge_actor_follows.py
```

The file names may be adjusted during detailed stage plans, but each file must
have a clear purpose paragraph/docstring and must not become another dumping
ground.

### Preserve facade boundaries during transition

A stage may expose repositories while keeping old `Database` methods as
forwarding wrappers, for example:

```python
class Database:
    def create_local_subscriber(self, **kwargs):
        """Temporarily forward old facade calls to LocalSubscriberRepository."""

        return self.local_subscribers.create(**kwargs)
```

Such wrappers must be marked as temporary in the stage plan and removed by a
later cleanup stage after call sites move to repository APIs.

### Repository method naming

Repository methods should not blindly repeat the current verbose `Database`
method names. Names can be shorter because the repository already supplies the
context:

```text
Database.get_local_subscriber_by_channel(...)
  -> database.local_subscribers.get_by_channel(...)

Database.create_local_community_thread_surface(...)
  -> database.local_community_surfaces.create_thread_surface(...)

Database.get_bridge_actor_follow_by_follow_activity_id(...)
  -> database.bridge_actor_follows.get_by_follow_activity_id(...)
```

The detailed stage plans should define exact names for each moved domain.

## Stage Boundaries

### Stage 0 — Persistence inventory and repository map

Goal: document the current `Database` method inventory and decide the exact
repository grouping before moving code.

Owns:

- enumerate every public/non-private `Database` method;
- group methods by domain;
- identify call sites for each group;
- identify behavior tests that cover each group;
- decide which repositories will be created and in what order;
- update or add documentation explaining the new persistence direction.

Does not own:

- moving methods;
- changing imports;
- changing runtime call sites;
- changing schema behavior.

Acceptance criteria:

- there is a checked-in inventory document or plan section mapping current
  methods to target repositories;
- each repository group has known primary call sites and test coverage;
- risky groups are identified before code movement begins.

### Stage 1 — Add navigation banners and freeze baseline behavior

Goal: make the existing `src/db.py` easier to review and ensure the current
behavior is protected before extraction.

Owns:

- add section banners to `src/db.py` for every persistence domain;
- add or strengthen module/class/method docstrings where needed;
- add high-level tests only if an uncovered persistence group is about to be
  moved in later stages;
- update development/navigation docs to point to the current persistence map.

Does not own:

- physical file split;
- repository class creation;
- call-site migration.

Example banner style:

```python
# ---------------------------------------------------------------------------
# Local community Discord surface helpers
#
# These helpers map canonical local-community posts/comments to concrete
# Discord thread/message surfaces. Canonical ActivityPub ids stay on
# LocalCommunityThread / LocalCommunityMessage; Discord ids live here.
# ---------------------------------------------------------------------------
```

Acceptance criteria:

- `src/db.py` is sectioned by domain;
- no behavior changes;
- full Python test suite remains green or unrelated failures are analyzed.

### Stage 2 — Extract schema and migration helpers

Goal: remove schema bootstrap/migration logic from the runtime repository
method file first.

Owns:

- create `src/db/` package or equivalent package layout;
- move `Database.create_all()`, `Database.migrate()` implementation details,
  `_table_columns()`, and current invariant checks into schema/migration
  modules;
- keep `Database.create_all()` and `Database.migrate()` as public facade
  methods delegating to the extracted implementation;
- preserve the current schema behavior exactly.

Does not own:

- moving domain repository methods;
- changing application call sites;
- changing models or migrations except import/module relocation.

Expected shape:

```python
class Database:
    def create_all(self) -> None:
        """Create the current schema using the shared SQLAlchemy metadata."""

        schema.create_all(self.engine)

    def migrate(self) -> None:
        """Run current-schema maintenance and invariant checks."""

        migrations.migrate(self.engine)
```

Acceptance criteria:

- schema/migration code is no longer embedded in the large repository method
  file;
- `Database.create_all()` and `Database.migrate()` public calls still work;
- migration/schema tests remain green;
- full Python test suite remains green or unrelated failures are analyzed.

### Stage 3 — Extract local-community repositories

Goal: move the largest and most active persistence domain out of the facade.

Owns extraction for:

```text
local communities
remote subscribers
local subscribers
local-community canonical thread/message rows
local-community Discord surface rows
local-community relay source/delivery rows
```

Suggested repositories:

```text
LocalCommunityRepository
RemoteSubscriberRepository
LocalSubscriberRepository
LocalCommunityContentRepository
LocalCommunitySurfaceRepository
LocalCommunityRelayRepository
```

Does not own:

- changing local-community runtime semantics;
- adding new subscriber behavior;
- changing schema;
- changing gateway contracts.

Transition options:

1. extract repository classes and keep forwarding methods on `Database`; then
   move call sites in the same or following stage;
2. extract repositories and migrate local-community call sites directly if the
   diff remains reviewable.

Acceptance criteria:

- local-community runtime, dashboard, operations, and tests read/write through
  the extracted repositories or explicit temporary forwarding wrappers;
- repository names reflect current concepts (`RemoteSubscriber`,
  `LocalSubscriber`, `Surface`), not removed legacy names;
- Stage 1–5 local-subscriber behavior tests remain green;
- full Python test suite remains green or unrelated failures are analyzed.

### Stage 4 — Extract remote subscription and bridge-follow repositories

Goal: isolate remote community subscription lifecycle from local-community
persistence.

Owns extraction for:

```text
ChannelCommunitySubscription
BridgeActorFollow
```

Suggested repositories:

```text
RemoteSubscriptionRepository
BridgeActorFollowRepository
```

Does not own:

- changing remote subscribe/unsubscribe behavior;
- changing allowlist semantics;
- changing ActivityPub Follow/Accept behavior;
- changing local subscriber behavior.

Acceptance criteria:

- `/subscribe-channel`, `/unsubscribe-channel`, inbound `Accept(Follow)`, and
  stale inbound filtering still use the same semantics;
- relevant subscription tests remain green;
- no old direct-follow lifecycle compatibility is reintroduced;
- full Python test suite remains green or unrelated failures are analyzed.

### Stage 5 — Extract registration, user, and event receipt repositories

Goal: move relatively self-contained service support persistence out of the
facade.

Owns extraction for:

```text
User
RegistrationSession
ActivityPubEventReceipt
```

Suggested repositories:

```text
UserRepository
RegistrationSessionRepository
EventReceiptRepository
```

Does not own:

- changing OAuth/registration behavior;
- changing idempotency behavior for inbound ActivityPub events;
- changing public FastAPI routes.

Acceptance criteria:

- registration flow tests remain green;
- inbound event idempotency tests remain green;
- `src/http_api.py`, `src/registration_service.py`, and
  `src/activitypub_handlers.py` use the new repositories or explicit temporary
  facade wrappers;
- full Python test suite remains green or unrelated failures are analyzed.

### Stage 6 — Extract ActivityPub object, remote actor, and mapping repositories

Goal: isolate generic ActivityPub object/mapping persistence from Discord and
community-specific repositories.

Owns extraction for:

```text
MessageMapping
PublishedActivityObject
RemoteActor
```

Suggested repositories:

```text
ActivityPubObjectRepository
MessageMappingRepository
RemoteActorRepository
```

Does not own:

- changing ActivityPub JSON rendering;
- changing gateway contracts;
- changing federation compatibility fallbacks;
- changing object IDs or actor IDs.

Acceptance criteria:

- object serving and update/delete tests remain green;
- gateway contract assumptions remain unchanged;
- no object/activity URL migration is introduced;
- full Python test suite remains green or unrelated failures are analyzed.

### Stage 7 — Extract legacy Lemmy mapping and Discord fanout group repositories

Goal: move remaining older remote-community mapping/fanout state out of the
facade.

Owns extraction for:

```text
PostLink
CommentLink
CommunityThreadGroup
CommunityThreadGroupDelivery
CommunityMessageGroup
CommunityMessageGroupDelivery
```

Suggested repositories:

```text
LegacyLemmyMappingRepository
DiscordFanoutGroupRepository
```

Does not own:

- changing remote Lemmy publish/fanout behavior;
- changing cross-channel fanout semantics;
- changing dedup keys.

Acceptance criteria:

- remote publish, inbound backfill, dedup, edit/delete, and fanout tests remain
  green;
- old remote-community behavior is unchanged;
- full Python test suite remains green or unrelated failures are analyzed.

### Stage 8 — Remove temporary facade wrappers and finalize repository API

Goal: make `Database` a small engine/session/repository container instead of a
permanent duplicate API surface.

Owns:

- remove temporary forwarding methods from `Database`;
- update all call sites to use repository properties directly;
- update docs and navigation to describe the final persistence layout;
- ensure no repository group depends on another repository through global
  imports that create cycles.

Does not own:

- changing schema;
- changing runtime behavior;
- changing gateway contracts.

Acceptance criteria:

- `src/db.py` has either been replaced by the `src/db/` package or reduced to
  a minimal import shim; the active `Database` implementation lives in a small,
  scan-friendly module such as `src/db/database.py`;
- no old forwarding wrappers remain except intentionally documented stable
  infrastructure facade methods such as `session()`, `create_all()`, and
  `migrate()`;
- no domain operation has two supported call paths through both a `Database`
  method and a repository method;
- all runtime tests remain green;
- docs point maintainers to the right repository modules.

## Touched Files

```text
AGENTS.md
README.md
src/db.py
src/db/__init__.py
src/db/database.py
src/db/schema.py
src/db/migrations.py
src/db/repositories/legacy_lemmy_mappings.py
src/db/repositories/event_receipts.py
src/db/repositories/remote_subscriptions.py
src/db/repositories/users.py
src/db/repositories/local_communities.py
src/db/repositories/remote_subscribers.py
src/db/repositories/local_subscribers.py
src/db/repositories/local_community_content.py
src/db/repositories/local_community_surfaces.py
src/db/repositories/local_community_relay.py
src/db/repositories/activitypub_objects.py
src/db/repositories/remote_actors.py
src/db/repositories/discord_fanout_groups.py
src/db/repositories/bridge_actor_follows.py
src/activitypub_handlers.py
src/dashboard.py
src/discord_event_router.py
src/http_api.py
src/registration_service.py
src/operations/subscribe.py
src/operations/unsubscribe.py
src/operations/list_subscriptions.py
src/local_communities/runtime.py
src/local_communities/discord_fanout.py
src/local_communities/federation_fanout.py
src/local_communities/participant_routing.py
tests/support/runtime.py
tests/support/database.py
docs/architecture/database-map.md
docs/development/navigation.md
notes/known_issues.md
```

The exact touched file list should be narrowed in each detailed stage plan.
Do not touch every file listed here in every stage.

## New Files

```text
plans/60_database_repository_split_umbrella.md
```

Later stage plans may add the `src/db/` package files listed above.

## Implementation Steps

1. Write detailed stage plans before each physical extraction stage.
2. Start with inventory and sectioning, not code movement.
3. Extract schema/migration code first because it has minimal runtime call-site
   surface.
4. Extract local-community persistence next because it is the largest and most
   active domain.
5. Move call sites in bounded groups and keep tests green after each group.
6. Use temporary facade wrappers only when needed to keep a stage reviewable.
7. Remove those wrappers in the final cleanup stage.
8. Update documentation whenever repository boundaries move.

## Tests

Each detailed stage must follow the project TDD rules when behavior or routing
is affected. For mostly mechanical extraction stages, tests should protect the
existing observable behavior rather than assert internal implementation details.

Minimum repeated checks after each stage:

```bash
./.venv/bin/pytest -q
```

This full-suite check is required even for mechanical extraction stages. A
stage is not complete until either the full suite is green or every non-green
test has been analyzed and classified. Stage-owned regressions must be fixed in
that stage; unrelated or environment-only failures must be reported without
silently expanding the stage scope.

When a stage touches local-community repositories, also run the local-subscriber
stage tests explicitly before the full suite:

```bash
./.venv/bin/pytest -q \
  tests/behavior/test_local_subscriber_stage1_scenarios.py \
  tests/behavior/test_local_community_surface_stage2_scenarios.py \
  tests/behavior/test_local_community_stage3_local_subscriber_sync_scenarios.py \
  tests/behavior/test_local_community_stage4_local_subscriber_origin_scenarios.py \
  tests/behavior/test_local_community_stage5_participant_edit_delete_scenarios.py
```

When a stage touches remote subscription repositories, run:

```bash
./.venv/bin/pytest -q \
  tests/behavior/test_subscription_scenarios.py \
  tests/behavior/test_unsubscribe_retry_scenarios.py \
  tests/test_follow_subscription_flow.py \
  tests/test_federation_allowlist_handlers.py
```

If full `pytest` reports failures outside the stage boundary, analyze them and
report:

- command;
- test name;
- error summary;
- whether the failure is stage-owned, indirectly related, unrelated, or
  environment-only.

Do not silently fix unrelated behavior in a repository-split stage.

## Regression / Blind-Spot Analysis

### Session ownership regressions

The most important risk is accidentally creating repositories that open
independent sessions or engines. That can make one runtime action read stale
state from one session while another repository writes through a different
session. All repositories must share `Database` session ownership.

### Transaction boundary drift

Current methods usually open one short session per call. Splitting methods into
repositories must not accidentally combine unrelated operations into one long
session or split an operation that currently depends on read-after-write
behavior.

### Hidden call-site compatibility wrappers

Temporary facade wrappers can make migration easier, but they can also recreate
the original problem by leaving two APIs forever. Each wrapper stage needs a
cleanup target. Wrappers are allowed only as repository-split scaffolding before
Stage 8; they must not be described or treated as permanent backward
compatibility.

### Circular imports

Repository files must import models and session types, not application runtimes.
Runtime/service modules can depend on repositories through `Database`, but
repositories must not import runtime modules.

### Behavior changes disguised as extraction

A repository split should not change dedup rules, fanout target selection,
ActivityPub ids, Discord surface ownership, or subscription lifecycle. Any
behavior change discovered during extraction must stop the stage and get its
own plan.

### Documentation drift

Because the purpose of the work is navigability, documentation must move with
the code. `docs/architecture/database-map.md` and `docs/development/navigation.md`
should remain accurate after every extraction stage.

## Open Questions

1. Should final call sites use repositories directly, or should `Database` keep
   a small stable domain facade forever?

   Recommendation: call sites should use repository properties directly for
   domain-specific work, while `Database` keeps only infrastructure methods and
   repository construction.

2. Should repository methods accept a session object for multi-step operations?

   Recommendation: not in the first split. Preserve current method-level session
   behavior first. Add explicit transaction support later only if a concrete
   multi-repository consistency issue appears.

3. Should schema/migration files live under `src/db/` even though there is
   currently a `src/db.py` module?

   Recommendation: yes, but do it as an explicit package migration stage. Do
   not leave both `src/db.py` and `src/db/` importable with conflicting names.
