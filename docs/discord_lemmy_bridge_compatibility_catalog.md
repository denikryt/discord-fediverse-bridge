# Discord Lemmy Bridge compatibility catalog

This document lists the compatibility logic currently present in the project: what exists, where it lives, why it exists, what it does, and whether it is long-term compatibility or temporary cleanup debt.

The categories are intentionally separated because not every compatibility path has the same meaning. Some paths protect real deployed SQLite databases or already-federated ActivityPub URLs. Some paths are temporary aliases left behind during staged refactors. Some paths are interoperability fallbacks for Lemmy, Mastodon, Fedify, or Discord payload variants.

## Summary

| Area | Compatibility type | Keep? |
|---|---|---|
| SQLite schema migrations | Current additive maintenance only | Keep current-schema guards; old schema translators removed |
| ActivityPub URL aliases and payload fallbacks | Federation compatibility | Keep long term unless migration is designed |
| Old Python/TypeScript names after refactors | Temporary technical debt | Remove after call sites are renamed |
| Discord formatting fallbacks | Historical message compatibility | Keep while old mirrored messages may be edited |
| Old nginx split-host settings | Not compatibility; intentional rejection | Keep as guardrail or remove only if split-host is restored |

## 1. SQLite schema compatibility

### 1.1 Legacy remote-participant table to `remote_subscribers`

**Where**

- `src/db/migrations.py`, `Database.migrate()`
- `src/models.py`, `RemoteSubscriber`
- `fedify-gateway/src/db.ts`, remote-subscriber readers

**What changed**

Older databases used a legacy remote-participant table for bridge-owned local communities.

The staged local-subscriber work renamed the concept to:

```text
remote_subscribers
```

The old name became misleading because the project now has two participant families:

```text
RemoteSubscriber = remote ActivityPub actor following a bridge-owned local community
LocalSubscriber  = same-instance Discord forum subscribed to a bridge-owned local community
```

**What the compatibility code does**

No active runtime migration path remains for the legacy table. Stage 0 verified
the deployment database against the current schema baseline, and Stage 5 removed
this pre-Stage-1 SQLite upgrade branch from `Database.migrate()`.

**Why it existed**

Deployments that already had accepted local-community followers needed a
one-time migration path so remote subscriber state survived the rename.

**Can it be removed?**

Already removed by Stage 5. Current deployments are expected to start from the
verified schema baseline where `remote_subscribers` already exists.

---

### 1.2 Canonical local-community rows to surface rows

**Where**

- `src/db/migrations.py`, `Database.migrate()`
- `src/models.py`, `LocalCommunityThread`, `LocalCommunityMessage`, `LocalCommunityThreadSurface`, `LocalCommunityMessageSurface`
- `src/local_communities/delivery_mapping.py`
- `src/local_communities/reply_mapping.py`
- `src/local_communities/runtime.py`
- `src/discord_event_router.py`

**Old shape**

Canonical rows directly stored one Discord surface:

```text
LocalCommunityThread.discord_thread_id
LocalCommunityThread.discord_starter_message_id
LocalCommunityMessage.discord_message_id
LocalCommunityMessage.parent_discord_message_id
```

That meant one ActivityPub post/comment could map to exactly one Discord thread/message surface.

**New shape**

Canonical rows hold ActivityPub identity and community-level metadata. Discord surfaces are stored separately:

```text
local_community_thread_surfaces
local_community_message_surfaces
```

This allows one canonical community activity to exist on multiple Discord participant surfaces:

```text
host forum surface
local subscriber forum surface A
local subscriber forum surface B
```

**What the compatibility code does**

`Database.migrate()` no longer upgrades pre-surface canonical rows. Stage 5
removed the old backfill/rebuild helpers after Stage 0 selected the current
migrated schema as the supported baseline.

The current migration path still creates current surface tables for fresh or
migrate-only callers and verifies that every canonical local-community thread
and message has exactly one host surface.

**Why it existed**

Without the old migration, pre-surface local-community posts/comments would
have lost their Discord mapping during the Stage 2 refactor. That upgrade path
was needed while old databases were still in scope.

**Can it be removed?**

Already removed by Stage 5. Current deployments are expected to have canonical
rows plus explicit host surface rows before running this code.

---

### 1.3 `discord_guild_id` additive migration

**Where**

- `src/db/migrations.py`, `Database.migrate()`
- `src/models.py`, `ChannelCommunitySubscription`

**What it does**

Adds `discord_guild_id` to `channel_community_subscriptions` if the column is missing.

**Why it exists**

Older databases may have remote community subscription rows without guild context. Newer command/listing behavior can use guild-scoped subscription state, but the app still needs to start against older databases.

**Can it be removed?**

Only if support for old databases without `discord_guild_id` is dropped.

## 2. Temporary naming compatibility after refactors

These paths are not protecting external federation identity or old database shape by themselves. They mainly keep the codebase working while call sites migrate from old names to new names.

### 2.1 Legacy Python remote-subscriber naming

**Where**

- `src/models.py`
- `src/db/database.py`
- `src/local_communities/runtime.py`
- `src/local_communities/federation_fanout.py`
- several behavior tests
- `notes/known_issues.md`

**What it does**

Stage 1 removed the old Python alias and repository wrappers that used the
legacy follower-oriented name for remote ActivityPub subscribers. Runtime,
fanout, and tests now call the explicit `RemoteSubscriber` / remote-subscriber
helpers directly.

**Why it exists**

The staged refactor renamed the concept because the bridge now has distinct
remote and local participant families. Stage 1 completes that naming cleanup in
Python so new code does not drift back to the ambiguous term.

**Can it be removed?**

Already removed by Stage 1.

---

### 2.2 Gateway old reader alias

**Where**

- `fedify-gateway/src/db.ts`

**What it does**

The gateway now reads accepted remote subscribers only from
`remote_subscribers`.

**Why it exists**

Before Stage 1, the gateway carried both a TypeScript export alias and an
old-table fallback so deployment ordering and call-site renames could be staged
independently.

**Can it be removed?**

The alias and fallback are removed in Stage 1. Database upgrade support remains
owned by Python-side migration code, not by gateway reader aliases.

---

### 2.3 Content publish service naming cleanup

**Where**

- `src/content_publish_service.py`
- `src/community_sync/runtime.py`
- tests/support and behavior tests

**What changed**

The temporary `DiscordPublishService` compatibility layer has been removed. The
canonical service name and constructor wiring are now:

```python
ContentPublishService
content_publish_service=...
```

`CommunityRuntime` no longer accepts `discord_publish_service=...`, no longer
keeps `self.discord_publish_service`, and tests no longer import the old class
name. The module path was renamed from `src/discord_publish_service.py` to
`src/content_publish_service.py` so the import path matches the canonical
service responsibility.

**Why the cleanup was safe**

This compatibility layer only protected in-repository call sites and tests. It
did not protect deployed database state, remote federation URLs, or external
ActivityPub payloads. After all call sites moved to the canonical name, keeping
the old alias would only preserve ambiguous terminology.

**Can it be removed?**

It has been removed by Stage 2 of the compatibility cleanup. Future code should
use only `ContentPublishService` and `content_publish_service`.

---

### 2.4 Optional `settings` argument in unsubscribe command registration

**Where**

- `src/commands/unsubscribe.py`

**What it does**

The command registration keeps a backward-compatible optional `settings` argument even if the implementation no longer needs it.

**Why it exists**

Older registration call sites/tests may still pass settings using the old signature.

**Can it be removed?**

Yes, after all call sites use the new signature.

### 2.5 Policy snapshot helper compatibility

**Where**

- `src/lemmyverse_communities.py`
- `src/local_community_permissions.py`

**What changed**

The bootstrap-only `allowlist=` and `settings=` helper inputs were removed.
Lemmyverse autocomplete and local-community permission helpers now accept only
an explicit effective `BridgePolicySnapshot`.

**Why the cleanup was safe**

All production callers already supplied snapshots. The removed forms existed
only for in-repository tests and could not represent dynamic blocklist or
super-admin entries. Tests now construct the same snapshot contract used by
production.

**Can it be removed?**

Already removed by policy cleanup Stage 1.

### 2.6 Discord mutation-tracking capability probe

**Where**

- `src/community_sync/discord_fanout.py`
- `src/app.py`

**What changed**

Mirror edit and delete fanout no longer probes the bot with `getattr()` before
recording bridge-originated mutations. `DiscordFanout` now requires an explicit
`DiscordMutationTracker`, and production composition supplies `BridgeBot`.

**Why the cleanup was safe**

The tracking calls protect raw Discord edit/delete handlers from echoing the
bridge's own mutations. They are required behavior rather than optional SDK
compatibility, so tests and production now implement the same direct contract.

**Can it be removed?**

Already removed by policy cleanup Stage 2.

---

## 3. Remote subscription lifecycle compatibility

No remote-subscription lifecycle compatibility path remains for `Accept(Follow)`.
The current handler accepts remote follow confirmations only through the
`BridgeActorFollow` lifecycle row. An `Accept(Follow)` whose `follow_activity_id`
does not map to a bridge follow is treated as stale or unknown and is skipped
without mutating channel subscription rows directly.

The older direct subscription-acceptance path was intentionally removed during
Stage 4 cleanup because it preserved a pre-`BridgeActorFollow` ownership model:

```text
Accept(Follow)
  -> no matching BridgeActorFollow
  -> look up ChannelCommunitySubscription by follow_activity_id
  -> mark that subscription accepted directly
```

That path is no longer part of the compatibility catalog. Remote subscription
acceptance now has one invariant: federation follow lifecycle state belongs to
`BridgeActorFollow`, while channel subscription rows depend on that bridge-level
state.

## 4. Command and discovery compatibility

### 4.1 Old autocomplete payload format

**Where**

- `src/community_discovery.py`, encoded community value parsing
- `src/commands/subscribe.py`

**Old format**

Older autocomplete values used a source-less payload like:

```text
actor_id|name|id
```

**New format**

Unified discovery introduced source-aware values such as:

```text
lemmy:...
bridge-local:...
bridge-remote:...
```

**What the compatibility code does**

If a source-less autocomplete value is received, the parser treats it as the old remote Lemmy path.

**Why it exists**

Discord autocomplete selections can be stale, and older tests or clients may still provide old payloads. The command should not fail simply because the selected value predates unified discovery.

**Can it be removed?**

Eventually, after the command cache and tests have fully moved to source-aware payloads. It is low-risk to keep.

---

### 4.2 Lazy Lemmy numeric id resolution

**Where**

- `src/commands/subscribe.py`
- `src/lemmy_client.py`
- `src/community_discovery.py`

**What it does**

If a community resolves as `remote_lemmy` but lacks the numeric Lemmy community id expected by the older remote subscribe operation, the command lazily asks Lemmy for the numeric id before calling the operation.

**Why it exists**

Direct actor URLs, handles, and legacy autocomplete values may identify the community without carrying Lemmy's numeric id. The old operation contract still needs it.

**Can it be removed?**

Only after the remote subscribe operation no longer requires a Lemmy numeric id, or after all resolve paths are guaranteed to provide it.

## 5. ActivityPub and federation compatibility

### 5.1 `/users/{username}` compatibility route for user actors

**Where**

- `fedify-gateway/src/server.ts`
- `fedify-gateway/src/federation.ts`
- `fedify-gateway/tests/verify-user-canonical-actor.ts`

**What it does**

Registered user actors are canonicalized under `/actors/{identifier}`, but `/users/{username}` remains as a compatibility entry point.

**Why it exists**

ActivityPub actor IDs and object URLs are federated identifiers. Remote servers may cache or reference old `/users/{username}` URLs. Removing the route would break dereferencing for old objects and actors.

**Can it be removed?**

Not safely without an explicit federation migration plan. This should be treated as long-term compatibility.

---

### 5.2 Published object canonical actor URL normalization

**Where**

- `fedify-gateway/src/published-objects.ts`
- gateway published-object tests

**What it does**

Published object rows may contain older actor URLs using `/users/{username}`. The gateway can still serve those objects and normalize actor references toward the canonical actor route where appropriate.

**Why it exists**

Old `PublishedActivityObject` rows should remain dereferenceable after actor route canonicalization.

**Can it be removed?**

Not while old rows with `/users/` actor URLs may exist.

---

### 5.3 Timestamp normalization for stored published objects

**Where**

- `fedify-gateway/src/published-objects.ts`

**What it does**

Stored rows may contain timestamps in slightly different forms, including Python/SQLite datetime strings. The gateway normalizes them before returning ActivityStreams JSON.

**Why it exists**

Fediverse consumers expect ActivityStreams-compatible timestamps. Older persisted rows may not already be in the exact JSON timestamp form needed by remote software.

**Can it be removed?**

Only if all stored timestamps are guaranteed to be written in the normalized form and old rows are migrated.

---

### 5.4 Raw ActivityPub JSON fallback around Fedify abstractions

**Where**

- `fedify-gateway/src/federation.ts`
- `fedify-gateway/src/normalize.ts`
- `src/activitypub_models.py`

**What it does**

The gateway sometimes keeps or uses raw incoming ActivityPub JSON instead of relying only on Fedify object abstractions.

**Why it exists**

Certain wrapped activities need exact nested payload details, especially:

```text
Announce(Create(...))
Announce(Update(...))
Announce(Delete(...))
Accept(Follow)
Undo(Follow)
```

Fedify's higher-level representation may not preserve every detail needed by the Python bridge's normalized event contract.

**Can it be removed?**

Probably not without replacing it with an equivalent normalized-event extraction layer that has the same coverage.

---

### 5.5 Lemmy and ActivityStreams payload-shape fallbacks

**Where**

- `fedify-gateway/src/normalize.ts`
- `fedify-gateway/src/federation-outbound.ts`
- `src/local_communities/activitypub_renderers.py`

**What it does**

The project accepts and renders several payload variants used by Lemmy/threadiverse peers, including cases where:

```text
Delete.object is a plain string
Delete.object is an object with id
object type is Page, Article, or Note depending on post/comment role
ActivityStreams Public appears as compact as:Public or expanded IRI
remote object URLs contain /post/{id} or /comment/{id}
```

**Why it exists**

Fediverse implementations are not uniform. The bridge is intentionally Lemmy/threadiverse-oriented and must tolerate real ActivityPub shapes emitted by those peers.

**Can it be removed?**

No, not without narrowing interoperability.

---

### 5.6 Mastodon-compatible local-community rendering

**Where**

- `src/local_communities/activitypub_renderers.py`
- `fedify-gateway/src/federation-outbound.ts`
- `FEDERATION.md`

**What it does**

The project has selected compatibility profiles for rendering local-community relay payloads, including Mastodon-oriented and threadiverse-oriented shapes.

**Why it exists**

Mastodon and Lemmy/threadiverse software have different expectations around `Note`, `Page`, `Announce`, reply targeting, and actor/audience fields. The bridge is not a full Mastodon-compatible server, but it needs selected Mastodon compatibility for local-community replies.

**Can it be removed?**

Only if the project intentionally drops Mastodon-compatible local-community behavior.

## 6. Discord message formatting compatibility

### 6.1 Old mirrored-message header fallback

**Where**

- `src/formatting.py`
- `src/community_sync/discord_fanout.py`
- related content-sync paths

**What it does**

When updating mirrored Discord messages, the formatter can preserve or reconstruct author/header text even if the existing message was created before the current header format.

**Why it exists**

Old mirrored messages may not contain the newest recognizable formatting. Edits should not erase attribution or produce malformed output.

**Can it be removed?**

Only if old mirrored messages are considered disposable or no longer edited.

## 7. Route and deployment compatibility

### 7.1 `/dashboard` redirect to `/`

**Where**

- `src/http_api.py`
- `README.md`

**What it does**

The canonical public dashboard page is `/`, but `/dashboard` redirects to `/`.

**Why it exists**

Earlier plans and links may refer to `/dashboard`. The redirect prevents those links from breaking.

**Can it be removed?**

It can be removed only if old `/dashboard` links are no longer expected to work. Keeping it is cheap and user-friendly.

---

### 7.2 Legacy split-host nginx settings are rejected, not supported

**Where**

- `fedify-gateway/nginx-setup.sh`
- `fedify-gateway/tests/verify-nginx-setup.sh`

**What it does**

The setup script detects old split-host variables such as separate gateway and bridge domains and fails with a clear message.

**Why it exists**

The project now expects single-domain path routing. A silent attempt to support the old shape could produce invalid ActivityPub/OAuth routing.

**Can it be removed?**

This is not backward compatibility. It is an intentional guardrail. Remove it only if the project deliberately reintroduces split-host deployment support.

## 8. Recommended cleanup follow-up

The most obvious cleanup target is the temporary Stage 1 naming compatibility.

Suggested cleanup plan:

1. Python call sites use `remote_subscriber` wording directly.
2. Tests and fixtures use `RemoteSubscriber` terminology explicitly.
3. The old Python alias and repository wrappers are removed.
4. The old TypeScript reader alias is removed.
5. The database migration from the legacy remote-participant table to `remote_subscribers` was removed in Stage 5 after Stage 0 selected the current schema baseline.

Do not remove schema migrations just because runtime call sites no longer use old names. Runtime naming cleanup and deployed-database upgrade compatibility are separate concerns.

## 9. Removal safety matrix

| Compatibility path | Safe to remove now? | Reason |
|---|---:|---|
| DB table rename/backfill migrations | Removed | Stage 5 dropped pre-Stage-1 SQLite upgrade support after Stage 0 verified the DB baseline |
| Surface backfill migration | Removed | Stage 5 dropped pre-Stage-2 canonical-column upgrade support after Stage 0 verified the DB baseline |
| Legacy Python remote-subscriber alias | Yes | Stage 1 removed it after runtime/tests moved to explicit terminology |
| `src/db/database.py` remote-subscriber wrappers | Yes | Stage 1 removed the old follower-named wrappers |
| Gateway old reader alias | Yes | Stage 1 removed the alias after TS call sites switched |
| `DiscordPublishService` alias | Removed | Stage 2 moved call sites and runtime wiring to `ContentPublishService` |
| Old autocomplete payload parser | Later | Useful for stale Discord choices |
| Legacy `Accept(Follow)` path | Removed | Stage 4 now skips unknown accepts unless a `BridgeActorFollow` exists |
| `/users/{username}` route | No | Federated URL compatibility |
| Published object actor/timestamp normalization | No | Old persisted object compatibility |
| ActivityPub payload fallbacks | No | Real fediverse interop |
| Discord header fallback | No | Old mirrored-message edit compatibility |
| `/dashboard` redirect | Optional, but cheap | Protects old links |
| nginx split-host rejection | Not compatibility | Guardrail against unsupported config |
