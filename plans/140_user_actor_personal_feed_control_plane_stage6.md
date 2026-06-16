# 140 — User Actor Personal-Feed Control Plane (Stage 6)

## Problem / Goal

Stage 5 makes existing bridge-owned user content reach accepted User Actor followers, but the repository has no persisted ownership contract for a Discord text channel that will later serve as one user's personal federated feed.

Stage 6 adds only that control plane. A registered user who has Discord `manage_guild` permission in an allowed guild can bind one eligible guild text channel to their own existing `User` actor, inspect the binding, move it, or remove it. The binding is globally unique per User Actor, per guild, and per channel. It is resolved before existing Discord community routing, but Stage 6 does not publish, map, edit, delete, thread, or federate any Discord content.

The current `HEAD` has three persisted Discord channel-owner domains: remote-community subscriptions (`channel_community_subscriptions`), local-community host forums (`local_communities`), and local-community subscriber forums (`local_subscribers`). No Mastodon-linked-channel model or runtime exists in the current repository. Stage 6 therefore checks every ownership domain that actually exists and introduces one shared ownership-query boundary that a future Mastodon-linked mode must join before it can claim channels. It does not invent a placeholder Mastodon token/channel table.

## Expected Behavior

### Account-settings surface

The existing `/account-settings` command group gains a coherent personal-feed surface:

- `/account-settings feed-view` shows whether the invoking registered User Actor has a binding and, if so, its guild/channel identity.
- `/account-settings feed-bind channel:<Discord text channel>` creates the first binding or atomically moves the caller's existing binding to the selected channel.
- `/account-settings feed-unbind` removes the caller's current binding without deleting or rewriting any publication, thread, or message rows.

The exact subcommand names may be adjusted during implementation only to satisfy Discord command-name constraints, while retaining one `/account-settings` family and the three behaviors above.

### Authorization and validation

A bind or move succeeds only when all of the following are true at mutation time:

1. the caller maps to an existing registered `User` row;
2. the interaction occurs in a guild;
3. the caller has Discord `manage_guild` permission in that guild; bridge super-admin status is not a substitute;
4. the guild passes the existing `BridgePolicyService.is_discord_guild_allowed` policy;
5. the selected channel belongs to the interaction guild;
6. the selected object satisfies the single authoritative normal-text predicate `isinstance(channel, discord.TextChannel) and channel.type is discord.ChannelType.text`; announcement/news (`GUILD_ANNOUNCEMENT`), forum, media, thread, category, voice/stage, DM, and partial unresolved channels are rejected;
7. the target channel is not owned by an existing remote subscription, local-community host, local subscriber, or personal-feed binding;
8. the target guild does not already have another User Actor's personal-feed binding.

The operation may select the caller's already-bound channel and return an idempotent success without rewriting timestamps. Moving to another channel in the same or another eligible guild updates the single row atomically. A target guild occupied by another user's feed fails without removing or changing the caller's old binding.

`feed-view` and `feed-unbind` require registration and the existing allowed-guild command policy. `feed-unbind` removes only the invoking user's row; it does not require the binding to belong to the guild where the command is invoked because the one global binding may point to another guild. Binding and movement nevertheless require `manage_guild` in the target/current interaction guild and can only target a channel from that guild, so Discord supplies an authoritative current permission context.

### Routing recognition without publication

`DiscordEventRouter` receives a personal-feed binding lookup dependency and exposes explicit classification methods for:

- a guild text-channel root message;
- a publication mapping/channel pair used by Stages 7–8 to determine whether Discord synchronization is still active.

For Stage 6, `BridgeBot.on_message` performs personal-feed classification before its existing thread/forum guards. When a root message is in a bound personal-feed channel:

- bot-authored messages remain ignored;
- a message by the bound Discord user is recognized and then intentionally produces no publication or persistence effect;
- a message by any other user is ignored completely, with no response, mapping, database mutation, or fallback into community routing.

Unbound text-channel messages retain current behavior and are ignored by the existing non-thread guard. Thread messages and all existing forum/community flows retain their current routing precedence and behavior.

### Removal and movement

Unbinding or moving changes only the current binding row. It does not delete old Discord messages, threads, ActivityPub objects, message mappings, or future publication-context rows. The active-binding contract is exact equality between:

```text
publication.discord_channel_id == current_binding.discord_channel_id
and publication.user_id == current_binding.user_id
```

Stages 7–8 may rely on that lookup. Historical mappings in an old channel remain archival and inactive after a move/unbind; Stage 6 does not create such mappings itself.

## Risk Dimensions

### Cross-domain channel ownership

Channel exclusivity currently spans independent tables and cannot be guaranteed by one ordinary foreign-key or unique constraint. Checking each repository before a later write without one write transaction creates a time-of-check/time-of-use race. Stage 6 therefore centralizes ownership checks and binding mutation inside a writer-reserved transaction.

### Concurrent bind/move races

The database must reject two concurrent attempts to bind different users in one guild, one user in two guilds, or two rows for one channel. Three database unique constraints are authoritative. For SQLite, the service uses `Database.immediate_session()` so cross-table conflict checks and the insert/update occur under one reserved writer transaction. Integrity errors are translated into stable domain conflicts after re-reading state rather than exposed as generic command failures.

### Authorization staleness

Discord permission and guild membership are evaluated from the interaction's current `Member` and selected guild channel. They are not persisted as authorization. Losing administrator permission later does not delete a binding automatically; a later mutation must pass authorization again. This avoids treating stale database metadata as a permission grant.

### Routing leakage

Personal-feed recognition must happen before the bot rejects non-thread messages, but it must not capture announcement/news channels, threads, forums, or other surfaces. Both bind admission and runtime event admission use the same authoritative predicate: `isinstance(channel, discord.TextChannel) and channel.type is discord.ChannelType.text`. The subsequent lookup is based on an exact bound channel ID plus guild ID. Runtime type admission remains mandatory even when a binding row exists, because the database stores only snowflake IDs and cannot prove the current Discord protocol channel type; this protects against manually inserted, legacy, corrupted, or externally converted channel state.

### Stage-boundary leakage

A binding must not create `PublishedActivityObject`, follower delivery, Discord mapping, thread, retry, or ActivityPub state. Tests assert absence of these effects. Stage 7 owns root publication; Stage 8 owns conversations and mutations.

### Information exposure

Bindings contain only Discord snowflake IDs and a local `user_id`; no Discord OAuth token, bot token, ActivityPub private key, or other secret is added. Logs and command responses identify guild/channel IDs or Discord mentions only. The public dashboard is not extended in this stage.

## Architecture

### 1. Persisted binding model

Add `PersonalFeedBinding` in `src/models.py`:

```python
class PersonalFeedBinding(Base):
    __tablename__ = "personal_feed_bindings"
    __table_args__ = (
        UniqueConstraint("user_id"),
        UniqueConstraint("discord_guild_id"),
        UniqueConstraint("discord_channel_id"),
        CheckConstraint("discord_guild_id > 0"),
        CheckConstraint("discord_channel_id > 0"),
    )

    id: int
    user_id: ForeignKey("users.id", ondelete="CASCADE")
    discord_guild_id: int
    discord_channel_id: int
    created_at: datetime
    updated_at: datetime
```

`user_id` is the ownership root rather than a duplicated Discord user ID. The repository joins/loads the `User` only when actor or Discord identity is required. A single mutable row is preferred over append-only active/inactive rows because Stage 6 needs exactly one current routing assignment, while the durable historical publications that must survive movement belong to Stages 7–8 and carry their own channel context.

The clean schema obtains the table through SQLAlchemy metadata. `src/db/migrations.py` explicitly creates the same table and indexes for existing SQLite deployments. Migration is additive, empty by default, and requires no backfill because no prior personal-feed binding exists.

### 2. Repository and transaction boundary

Add `PersonalFeedBindingRepository` with read methods using normal repository sessions:

```python
get_by_user_id(user_id)
get_by_discord_user_id(discord_user_id)
get_by_channel_id(channel_id)
get_by_guild_id(guild_id)
is_current_channel_for_user(user_id, channel_id) -> bool
```

Mutation is owned by one domain service, not by command callbacks. `PersonalFeedService.bind_or_move(...)` opens `Database.immediate_session()` and performs, in this order:

1. reload the User by Discord ID inside the transaction;
2. re-check current personal-feed rows by user, guild, and channel;
3. query all existing channel-owner tables for the target channel;
4. return an idempotent result if the row already matches;
5. reject any conflict without touching the caller's old row;
6. update the caller's row in place or insert one row;
7. flush so uniqueness violations occur before returning;
8. commit as one operation.

`unbind(user_id)` deletes only that user's current row in one transaction and returns `not_bound` idempotently if absent.

The service returns typed outcomes such as `bound`, `moved`, `already_bound`, `unbound`, `not_bound`, `channel_conflict`, and `guild_conflict`. Command code translates those outcomes into ephemeral user messages. Exceptions are reserved for infrastructure faults.

### 3. Shared channel-ownership resolver

Add `src/channel_ownership.py` as the single cross-domain query boundary. It accepts an existing SQLAlchemy `Session` so the bind operation can inspect all owners in the same transaction. Its current owner kinds are:

```text
remote_subscription
local_community_host
local_subscriber
personal_feed
```

It returns structured non-secret metadata (`kind`, row ID, owning channel/guild IDs) rather than model instances from unrelated domains. This module does not decide authorization and does not mutate rows.

This foundation is required now because Stage 6 must atomically reject conflicts across multiple existing tables. Centralizing the query also prevents Stage 7 routing and any later Mastodon-linked-channel implementation from independently redefining exclusivity. It does not add future workflow state or a generic lease/registry table.

Important alternative rejected: a new universal `channel_claims` table with one row for every bridge mode. Migrating all existing modes into that table would create a broad, high-risk ownership migration outside Stage 6 and temporary dual sources of truth. The resolver preserves current tables while giving one consistent conflict contract.

### 4. Application service and Discord adapter

Add `src/personal_feeds.py` containing:

- immutable binding/result data transfer types;
- `PersonalFeedService` authorization-independent persistence operations;
- lookup methods used by commands and routing.

Discord-specific checks remain in `src/commands/account_settings.py`:

- `_resolve_authorized_user` continues to enforce registration, global/user bans, guild context, and allowed-guild policy;
- bind additionally checks `interaction.user.guild_permissions.manage_guild` directly and rejects bridge super-admin-only callers;
- the command option is restricted at registration to Discord `GUILD_TEXT` (`ChannelType.text`) only when the resolved `discord.py` application-command transformer supports an explicit one-type `channel_types` declaration; a plain `discord.TextChannel` annotation is not treated as sufficient because discord.py also represents announcement/news channels with that concrete class;
- regardless of UI filtering, one shared adapter predicate verifies `isinstance(channel, discord.TextChannel) and channel.type is discord.ChannelType.text`, guild identity, and positive IDs before any mutation;
- the interaction is deferred before the bounded synchronous write, and `feed-bind`/move plus `feed-unbind` invoke the service exclusively through the existing cancellation-safe `_run_owned_sync_mutation(lambda: ...)`; command code never performs the SQLite transaction directly on the event loop;
- after deferral, all outcomes are returned with `edit_original_response`;
- the selected channel is passed to the service only as primitive IDs.

No Discord object is stored by the service. No new secret reaches the command or persistence layer.

Important alternative rejected: use only a “bind current channel” command. A channel selector is explicit and permits administrators to configure an eligible channel without changing conversational context. UI filtering is defense in depth only: Discord distinguishes `GUILD_TEXT` from `GUILD_ANNOUNCEMENT`, while discord.py uses `TextChannel` for both, so the explicit `ChannelType.text` runtime predicate remains authoritative for interaction payloads and test doubles.

### 5. Router and bot wiring

`DiscordEventRouter` gains `personal_feed_service` and methods with narrow semantics:

```python
resolve_personal_feed_root(guild_id, channel_id) -> PersonalFeedBindingView | None
is_active_personal_feed_channel(user_id, channel_id) -> bool
```

The first method requires exact positive guild/channel IDs. The second supports the Stage 8 stale-mapping rule without knowledge of publication models.

`BridgeBot.on_message` changes order:

1. ignore the bridge bot and other bots as today;
2. for a channel satisfying the shared normal-text predicate (`isinstance(message.channel, discord.TextChannel)` and `message.channel.type is discord.ChannelType.text`), ask the router for a binding before returning; announcement/news channels fail admission and continue through the unchanged non-personal-feed path even if a matching binding row exists;
3. if bound, compare `message.author.id` with the owning `User.discord_user_id`, record no state, and return in both owner/non-owner cases;
4. otherwise continue the unchanged thread/forum path.

No personal-feed runtime handler is introduced because Stage 6 has no content behavior to own. A no-op “publisher” abstraction would be unused future coordination logic and is deliberately excluded.

`src/app.py` constructs one `PersonalFeedService`, registers its repository on `Database`, injects the service into `DiscordEventRouter`, and passes it to account-settings command registration through `BridgeBot`.

### 6. Discord platform contracts

The implementation relies on current Discord/discord.py contracts that must be verified during implementation against official documentation:

- Discord protocol channel types distinguish `GUILD_TEXT` (`0`) from `GUILD_ANNOUNCEMENT` (`5`);
- discord.py represents both ordinary text and announcement/news channels as `discord.TextChannel`, so concrete Python type checks alone are insufficient;
- application-command channel options can be restricted to specified protocol channel types where the resolved transformer API supports it; runtime validation remains authoritative;
- forum channels and threads are separate concrete types;
- `Member.guild_permissions.manage_guild` exposes the effective guild-management permission used by existing project policy input;
- receiving root message contents requires the privileged Message Content intent for verified applications at scale, and the repository already enables `intents.message_content = True`.

The deployment docs must state that personal-feed publication in Stage 7 depends on the same enabled Message Content intent; Stage 6 itself only recognizes events and does not consume content.

The Discord channel-type correction is mandatory rather than optional: Discord documents ordinary guild text as protocol type `0` and announcement as type `5`, and discord.py supports changing a `TextChannel` to/from news/announcement mode. Therefore `isinstance(..., discord.TextChannel)` cannot enforce the Stage 6 normal-text invariant by itself.

## Boundaries and Ownership

- `PersonalFeedBindingRepository` owns CRUD for current bindings.
- `PersonalFeedService` owns bind/move/unbind transaction semantics and uniqueness/conflict outcomes.
- `channel_ownership.py` owns read-only cross-domain channel-claim classification.
- `account_settings.py` owns Discord interaction presentation and current Discord permission/type validation.
- `DiscordEventRouter` owns runtime classification order.
- `BridgeBot` owns Discord event admission and remains intentionally side-effect-free for personal-feed roots in Stage 6.
- Existing community runtimes retain all publication behavior.

There is no distributed transaction with Discord. Binding records an existing Discord channel; it does not create, rename, modify, or delete the channel. Discord API/cache failure before mutation causes no DB change. A committed binding remains valid even if the Discord channel is later deleted; future command view may show IDs when resolution is unavailable, and a later bind/unbind can replace/remove it. Automatic orphan cleanup is deferred because deleting a binding based on transient cache absence could disable a valid feed.

## Compatibility and Deployment Consequences

- The migration is additive and starts with zero rows; all existing deployments behave exactly as before until a user binds a feed.
- Existing remote subscriptions, local-community hosts, and local subscribers are not migrated or rewritten.
- Existing command registration remains one `/account-settings` group.
- Existing forum/thread event routing is preserved after the new exact text-channel classification branch.
- No gateway, ActivityPub API, signing-key, follower-delivery, media, or HTTP public contract changes.
- SQLite backups automatically include the new table. No filesystem or secret backup scope changes.
- Rollback to pre-Stage-6 code leaves an unknown additive table that older code ignores; no existing table shape is changed.

## Touched Files

- src/models.py
- src/db/database.py
- src/db/migrations.py
- src/db/repositories/__init__.py
- src/commands/account_settings.py
- src/discord_event_router.py
- src/discord_bot.py
- src/app.py
- tests/conftest.py
- tests/commands/test_account_settings_command.py
- docs/architecture/database-map.md
- docs/architecture/bridge-modes.md
- docs/architecture/event-flows.md
- docs/DEPLOY.md

## New Files

- src/db/repositories/personal_feed_bindings.py
- src/channel_ownership.py
- src/personal_feeds.py
- tests/behavior/test_personal_feed_control_plane_scenarios.py
- plans/140_user_actor_personal_feed_control_plane_stage6.md

## Implementation Steps

1. Add a failing migration/schema behavior test that opens both a clean and a migrated Stage-5 database and verifies the `personal_feed_bindings` columns, foreign key, and three uniqueness constraints.
2. Add `PersonalFeedBinding`, the additive migration, repository export, and `Database.personal_feed_bindings` wiring. Re-run the schema test.
3. Add failing service behavior scenarios for first bind, idempotent rebind, move, unbind, one-user uniqueness, one-guild uniqueness, one-channel uniqueness, and preservation of the old binding on target conflict.
4. Implement `channel_ownership.py` against the current three owner tables plus personal feeds. Implement `PersonalFeedService` with one `BEGIN IMMEDIATE` transaction for check-and-write and typed results. Re-run service scenarios.
5. Add concurrent bind scenarios using separate database/service instances against one SQLite file:
   - two users race for one guild/different channels;
   - one user races to bind two guilds;
   - two users race for one channel.
   Exactly one operation succeeds; the durable state satisfies all three invariants; the loser receives a domain conflict rather than a raw integrity error.
6. Add account-settings command tests for registered-user resolution, `manage_guild` enforcement independent of bridge super-admin status, allowed-guild policy, `GUILD_TEXT` command-option restriction where supported, authoritative runtime channel-type validation, cross-guild rejection, conflict messages, move confirmation, view, and idempotent unbind. Include an announcement/news `discord.TextChannel` fixture and prove rejection occurs with no DB mutation.
7. Extend `account_settings.register` with the feed subcommands and inject `PersonalFeedService` through `BridgeBot`/`app.py`. Defer before every bind/move/unbind write, execute each synchronous mutation through `_run_owned_sync_mutation`, finish through `edit_original_response`, keep all responses ephemeral, and pass only primitive IDs into the service.
8. Add routing behavior tests through `BridgeBot.on_message` and the real `DiscordEventRouter`:
   - bound owner root is recognized but causes no publication/mapping/fanout;
   - bound non-owner root is completely ignored;
   - unbound root remains ignored;
   - thread messages in remote/local community modes still reach the same runtime;
   - forum and announcement/news channels cannot be treated as personal feeds;
   - a deliberately seeded binding row for an announcement/news channel still does not activate classification or any side effect;
   - disallowed-guild messages cause no effect.
9. Implement the router lookup and bot ordering change without introducing a personal-feed content runtime.
10. Add move/unbind tests proving that unrelated pre-existing mapping/object fixture rows are unchanged and `is_active_personal_feed_channel` becomes false for the old channel and true only for the current channel.
11. Update only architecture/deployment documents whose stated purpose covers the new model, bridge mode, event classification, or Message Content deployment requirement. Do not document Stage 7 publication as available.
12. Run targeted tests, then the complete Python suite, migration assurance, formatting/static checks configured by the repository, and gateway tests to prove the Python-only additive change did not alter gateway contracts.

## Tests

### Persistence and migration

- clean schema contains the new table and constraints;
- Stage-5 schema migrates with no binding rows and no changes to existing data;
- deleting a User cascades its current binding;
- invalid/non-positive guild or channel IDs are rejected;
- duplicate `user_id`, `discord_guild_id`, or `discord_channel_id` is rejected by the database.

### Authorization and command behavior

- unregistered caller cannot view, bind, move, or unbind;
- registered caller without `manage_guild` cannot bind even if configured as bridge super-admin;
- registered guild administrator can bind only in an allowed guild;
- selected channel must satisfy `isinstance(channel, discord.TextChannel) and channel.type is discord.ChannelType.text` in the interaction guild;
- announcement/news, forum/media, thread, DM, voice/stage, category, partial unresolved, and cross-guild inputs are rejected before mutation;
- command registration narrows the selector to `GUILD_TEXT` only when supported by the resolved transformer, but a forged/test payload for `GUILD_ANNOUNCEMENT` is still rejected by the authoritative runtime predicate;
- announcement/news rejection leaves both a prior binding and an initially empty binding state unchanged;
- view shows no binding or the current binding without exposing secrets;
- unbind is idempotent and affects only the caller's row.

### Conflict isolation

For each existing owner type, seed a target channel and verify bind fails with no change to the user's previous binding:

- accepted or pending remote-community subscription row (channel ownership exists regardless of follow status until explicitly removed);
- local-community host forum;
- active or inactive local subscriber row (historical ownership remains reserved while the row exists);
- another user's personal feed;
- another personal feed in the target guild on a different channel.

The test explicitly records that no Mastodon-linked owner type exists at current `HEAD`; no fake table or test double is added for it.

### Concurrency and restart

- race tests produce one winner and one stable conflict for each uniqueness dimension;
- reopening the database restores the same active binding;
- router lookup after restart resolves by durable IDs, not in-memory registration;
- command callbacks defer before writes and call bind/move/unbind only through `_run_owned_sync_mutation`;
- cancellation before commit rolls back, while cancellation after the worker starts waits for and retrieves the worker result/exception before propagating, so a committed row remains authoritative and no write is abandoned.

### Routing and no-side-effect boundary

- personal-feed classification occurs before the existing non-thread return;
- owner and non-owner root messages both create zero `PublishedActivityObject`, message mapping, content mutation, follower source/delivery, thread, or ActivityPub receipt rows;
- non-owner receives no Discord response;
- existing local-community and remote-subscription thread/message tests remain green;
- exact guild/channel matching prevents a channel ID or guild mismatch from being classified;
- the shared `ChannelType.text` admission predicate prevents an announcement/news event from being classified even when a matching binding row is seeded directly in the database;
- move/unbind immediately changes active lookup while preserving all unrelated historical rows.

## Validation

- inspect the final diff and confirm no gateway, ActivityPub handler, content publisher, follower delivery, mapping schema, or outbox behavior changed;
- run the new scenario file and account-settings tests;
- run all Python tests and repository migration assurance;
- run `python -m compileall src tests`;
- run the repository's configured format/lint/type checks, if present;
- run `npm test`/the documented gateway verification suite because composition contracts must remain unchanged;
- create a fresh database and migrate a copy of the previous-stage schema, then compare all pre-existing table definitions/data;
- verify the working tree contains only the plan during planning review; implementation is not started in this stage-plan task.

## Regression and Blind-Spot Analysis

- **Community route capture:** a broad “channel has binding” branch placed after thread detection would never recognize roots; placed with only `isinstance(TextChannel)` could capture announcement/news channels. Tests enforce the shared `TextChannel + ChannelType.text` predicate before lookup and the existing non-thread return.
- **Status-sensitive ownership:** pending/failed subscriptions and inactive local subscribers still own their channel until their domain removes the row. Treating only active rows as conflicts could allow two modes to mutate one Discord surface.
- **Move data loss:** delete-then-insert would lose the old binding if target validation or insertion fails. The selected update-in-place transaction preserves the old row on every conflict/error.
- **Cross-table race:** per-repository sessions cannot protect a multi-table check. The ownership resolver must use the mutation's existing immediate session.
- **Super-admin confusion:** existing management policies sometimes permit owner/super-admin alternatives. Feed binding deliberately checks Discord `manage_guild`; tests include a super-admin lacking that permission.
- **Stale Discord cache:** absence from `bot.get_channel` is not proof of deletion. Bind uses the interaction-resolved channel, while view degrades to IDs instead of auto-deleting state.
- **Future Mastodon mode:** because no such domain exists now, Stage 6 cannot verify a nonexistent table. The shared resolver is the required extension point; any future channel-owning mode must add itself before activation.
- **Premature publication:** no-op recognition can be accidentally wired to existing `CommunityRuntime`. Tests assert zero durable and outbound effects for bound roots.

## Stage Handoff

### Contracts changed

- one durable current personal-feed binding may exist per User Actor, guild, and channel;
- account settings can view/bind/move/unbind that binding under explicit Discord guild-administrator authorization;
- Discord root text-channel events can be classified as personal-feed events before existing routing;
- later stages can test whether a publication's channel is still the actor's current active feed channel.

### Contracts intentionally preserved

- actor identity, profile, followers, follower delivery, existing Create/Update/Delete semantics, community routing, mappings, and gateway contracts are unchanged;
- root messages produce no federation or persistence in Stage 6;
- historical rows are never deleted by binding movement/removal.

### Later-stage work left untouched

- Stage 7 owns root Note construction, typed publication context, mappings, idempotency, and follower fanout;
- Stage 8 owns threads, replies, inbound rendering/storage, edits, deletes, stale-mapping synchronization suppression, and Discord delivery retry;
- Stage 9 owns dashboard/public documentation completion, diagnostics, and outbox behavior.

### Assumptions handed to Stage 7

- `PersonalFeedService.resolve_root_binding(guild_id, channel_id)` returns at most one durable owner, but callers must first admit the live Discord surface with the authoritative `TextChannel + ChannelType.text` predicate; a binding row alone never proves channel eligibility;
- each user, guild, and channel has at most one current binding by database constraint;
- `is_active_personal_feed_channel(user_id, channel_id)` is the authoritative current-channel comparison;
- target channel conflict checks cover every channel-owning domain present through Stage 6;
- moving/unbinding never removes historical publication/mapping state.

No temporary adapter, duplicate binding API, placeholder publisher, or broken caller remains.

## Open Questions

None. The current code, umbrella plan, and Discord contracts determine the Stage 6 design without a product decision from the coordinator.
