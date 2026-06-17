# 142 — User Actor Outbox, Operational Hardening, and Release Validation (Stage 9)

## Problem / Goal

Stages 1–8 leave registered User Actors with stable profiles, followers, follower fanout, personal-feed publication, durable conversations, and mutation handling, but three release contracts remain incomplete:

1. `GET /actors/:identifier/outbox` and its compatibility alias `GET /users/:username/outbox` still return an empty `OrderedCollection`, even though `published_activity_objects` is now the durable source of truth for supported public posts and comments.
2. Existing follower-delivery and profile-media maintenance state is durable but lacks a narrow operator-facing diagnostic/retry surface and a complete, tested backup/orphan-cleanup runbook.
3. Documentation and full-system release validation do not yet describe the final User Actor account contract, inactive personal-feed behavior, data retention, or all supported deployment paths.

Stage 9 will add a read-only, bounded, stable User Actor outbox over existing authored-object persistence; expose non-secret operator tooling for follower-delivery inspection and retry wake-up; operationalize existing media orphan cleanup and complete-archive backup/restore; update responsible documentation; and run the complete compatibility and release validation matrix.

This stage does not redesign publication, follower ownership, mutation ordering, personal-feed routing, or durable comment storage established in Stages 1–8.

## Expected Behavior

### Public User Actor outbox

For a registered user such as `alice`:

- `GET /users/alice/outbox` returns an ActivityStreams `OrderedCollection` for the resolved registered user, but every collection identifier is canonicalized from that user’s persisted `User.outbox_url`:
  - `id` equals the resolved User’s persisted canonical `outbox_url`, regardless of which HTTP alias was requested;
  - `type: "OrderedCollection"`;
  - `totalItems` equal to the count of eligible authored objects;
  - `first` pointing to `?page=1` when `totalItems > 0`;
  - no embedded unbounded history.
- `GET /users/alice/outbox?page=1` returns an `OrderedCollectionPage` with at most 40 `Create` activities in `orderedItems`, newest first.
- `GET /actors/alice/outbox` is the canonical route for users registered with the current `/actors/{username}` identity contract. `GET /users/alice/outbox` is an HTTP compatibility alias only. Both root and page responses use the same persisted `User.outbox_url` for `id`, `first`, `next`, and `prev`; request-path spelling never creates a second logical collection or changes item IDs.
- A page number must be a positive integer. The page size is a server-owned constant of 40 and is not caller-selectable.
- Pagination uses stable ordering by `published_at DESC, id DESC`. Count and bounded page rows are loaded by one repository operation in one SQLAlchemy session and one SQLite read transaction, using one shared eligibility predicate, so each HTTP response is internally snapshot-consistent under concurrent publication or committed deletion.
- `next` is present only when another non-empty page exists; `prev` is present only for pages after page 1. An out-of-range positive page returns an empty page with `prev` where applicable and no `next`, rather than a 404.
- Every item is a complete public `Create` activity built from the existing durable `PublishedActivityObject` row and existing gateway renderers. The outbox does not mint new IDs or serialize a second object shape.

Eligible rows are exactly those where:

- `actor_username` belongs to the resolved registered `User` and `actor_url` resolves to that same User Actor identity;
- `kind` is one of the currently supported authored public kinds, `post` or `comment`;
- the row is bridge-owned durable publication state in `published_activity_objects`;
- `deleted_at IS NULL`;
- the row is not merely a pending/failed publication intent. Current persistence inserts `PublishedActivityObject` only after gateway rendering succeeds and commits it atomically with its source/mapping, so no new publication-state column is introduced in Stage 9.

The collection includes authored objects across both existing publication contexts:

- community context, including local-community and remote-community interactions represented by the durable row;
- personal-feed context, including roots and replies authored by the user.

The collection excludes:

- remote-authored `InboundConversationObject` rows;
- Discord-only messages and historical Discord content;
- rejected or failed publication attempts that never produced a `PublishedActivityObject`;
- deleted objects (`deleted_at IS NOT NULL`);
- profile `Update(Person)`, content `Update`, and `Delete` activities, because the account outbox in this stage is the discoverable authored timeline, represented by one current Create/object entry per durable public post/comment rather than an append-only mutation log;
- any future private or followers-only object type.

Deletion policy is therefore “remove deleted objects from collection membership while preserving existing dereference/mutation state.” Stage 9 does not change the current object-route behavior or convert deleted rows to a new Tombstone representation. This avoids a cross-cutting mutation contract change after Stage 8.

### Operator delivery diagnostics and retry tooling

Add a local CLI entry point, not a public/dashboard route, because delivery rows expose remote endpoints, actor IDs, errors, and timing data that are operationally sensitive.

The tool will support:

```text
bridge-user-deliveries list [--username alice] [--status failed] [--limit 100]
bridge-user-deliveries retry --delivery-id 456 [--username alice]
bridge-user-deliveries retry --source-id 123 [--username alice]
bridge-user-deliveries retry --username alice --all-failed --limit 100
```

Contracts:

- `list` is read-only and emits one bounded row per endpoint delivery with source kind, logical version, activity ID, status, attempt count, last/next attempt timestamps, and a bounded sanitized error. Private keys, shared secrets, authorization headers, full activity JSON, profile field contents, post bodies, and Discord message content are never printed.
- Remote endpoint URLs and represented actor IDs are shown only in the local operator CLI, not in the public dashboard or HTTP API.
- `retry` does not directly send network traffic and does not fabricate a new source or activity ID. Normal mode requires exactly one precise selector: `--delivery-id`, or `--source-id`; optional `--username` is an additional ownership check, never a substitute for the precise selector. Invocation without one of those selectors fails validation before opening a write transaction.
- A username-wide operation is available only through the deliberately explicit bulk form `--username USER --all-failed --limit N`. `--all-failed` and a positive bounded `--limit` are both mandatory; the implementation caps `N` at 100. Selection is deterministic by delivery ID, one command processes one batch, and the operator must invoke another command for the next batch.
- Both exact and bulk modes transactionally change only selected retryable `failed` rows back to `pending`, clear claim ownership, and make them immediately due; then the normal `UserActorDeliveryWorker` processes them when the bridge is running or after restart. Delivered, superseded, ineligible, or actively `sending` rows are never reset.
- The bulk operation performs deterministic selection and conditional updates in one `BEGIN IMMEDIATE` transaction and reports `matched`, `reset`, `skipped`, and `remaining`. Exact mode exits non-zero when the requested delivery/source does not exist, fails the optional username ownership check, or has no retryable failed delivery.
- The retry mutation remains inside `UserActorDeliveryRepository`, which already owns delivery state transitions. The CLI is only an adapter. Concurrent worker/CLI access is protected by the same immediate-transaction and compare-and-set status predicate, so retry cannot steal an active claim.

Important alternative rejected: an unauthenticated dashboard or new public HTTP diagnostic endpoint. That would broaden access to remote relationship and failure metadata and create a new authorization surface unrelated to the public dashboard’s current safe-data contract.

### Profile media orphan cleanup

`UserProfileService.cleanup_orphans()` and the profile-media FK/source-retention model already own cleanup semantics. Stage 9 will add a narrow CLI adapter and deployment instructions rather than another cleanup implementation.

```text
bridge-profile-media cleanup \
  --database sqlite:////data/bridge.db \
  --media-root /data/profile-media \
  --deployment-lock /data/deployment-state.lock \
  --media-lock /data/profile-media.lock \
  --grace-seconds 86400
```

Contracts:

- default operation requires the deployment shared/exclusive locking contract already used by runtime and backup paths;
- files younger than the grace period are retained;
- current avatar/header references and media retained by executable outbound profile sources are never deleted;
- terminal source-media ownership is released and orphan row/file deletion remains coordinated in the existing reserved transaction plus media lock;
- missing files are handled idempotently while stale DB metadata is removed only when unreferenced;
- invalid paths, symlinked storage, ownership violations, or lock failure stop cleanup without deleting candidates;
- output contains counts only, not storage keys or user profile data unless a verbose local operator flag is explicitly introduced and tested.

No periodic scheduler is added. Operators may invoke the command from cron/systemd or a one-shot Compose service. A built-in scheduler would add lifecycle and leader-election concerns without a Stage 9 requirement.

### Backup and restore

The existing complete archive mechanism in `src/db/backup.py` remains the single owner of database-plus-profile-media backup and restore. Stage 9 will verify and document:

- online backup obtains the existing deployment/media locks and captures a consistent SQLite snapshot plus all referenced profile media;
- restore validates the archive before replacement, uses the exclusive deployment lock, and leaves recoverable markers for interrupted replacement;
- DB-only copying is explicitly documented as incomplete after profile media exists;
- both Compose image deployment and source-build deployment mount the same durable `/data` state and a separate backup output directory;
- restoration into a clean temporary data directory reproduces user/profile/media/outbox state and allows the gateway to dereference restored actor media and outbox items;
- backup service/container receives filesystem paths and database configuration only, never Discord token, OAuth secrets, Fedify shared secret, or actor private-key material as environment variables beyond the key material already contained in the database archive itself.

### Documentation and final release behavior

Documentation will state:

- User Actor profile, followers, content fanout, personal-feed, and outbox behavior;
- public outbox inclusion/exclusion and deletion policy;
- actor/profile cache behavior: profile updates are actively delivered, but remote software controls cache refresh and presentation;
- follower delivery diagnosis/retry commands and their privacy boundary;
- database and profile-media retention/backup ownership;
- inactive/removed personal-feed mappings continue storing inbound comments but do not render them in Discord;
- restoration of retained personal-feed publication/comment history into Discord after channel loss/rebinding remains future work;
- abuse/privacy implications of public posts, public follower identities, retained inbound comments, operator-visible remote endpoints, and remote-server retention.

## Scope Boundaries

### Contracts changed by Stage 9

- User Actor outbox routes become real paginated ActivityStreams collections.
- Python internal gateway read API gains a bounded outbox page DTO.
- `PublishedActivityObject` persistence gains query indexes only if query-plan tests show the current schema cannot efficiently satisfy user/order/filter access.
- Local operator CLIs gain delivery inspection/retry and profile-media cleanup adapters.
- Backup/restore and final account behavior documentation becomes complete and verified.

### Contracts intentionally preserved

- Actor URLs, usernames, inbox/outbox/followers URLs, keys, and object/activity IDs.
- Publication transaction boundaries and follower audience snapshots.
- Stage 8 content revision, delete ordering, durable inbound-comment storage, stale-binding suppression, and Discord reconciliation semantics.
- Existing object and activity dereference routes.
- Community outboxes remain outside this stage and continue their current behavior.
- Public dashboard remains free of delivery endpoint/error data.
- Existing backup archive format unless a failing restore test proves a compatible manifest extension is required.

### Later or out-of-scope work deliberately untouched

- historical Discord backfill;
- replay/restoration of retained personal-feed history into a replacement Discord channel;
- append-only mutation history in the outbox;
- private/followers-only posts;
- media attachments in posts;
- account migration, favourites, boosts, bookmarks, polls, quotes, pinned posts, or featured tags;
- multiple personal-feed channels per user or guild;
- community outbox implementation;
- remote context reconstruction.

## Risk Dimensions

### Authorization and public-data boundary

The outbox is intentionally public and must expose only content that was already published publicly. Internal delivery diagnostics contain remote endpoints, actor IDs, and failure strings, so they remain local CLI output. No private key or shared secret may cross into outbox DTOs, diagnostics DTOs, logs, or tests.

### Cross-user leakage

`actor_username` is stored denormalized. The repository must first resolve the `User`, then filter by the canonical user ownership fields and test adversarial rows with another user’s username/actor URL. A caller-supplied actor URL is never used as the query predicate. The internal endpoint accepts only a resolved username path and server-owned page size.

### Pagination snapshots, races, and duplicates

Each root/page response must derive `totalItems`, membership, and navigation from one database transaction view. `ActivityPubObjectRepository.load_user_outbox_slice()` therefore performs count and list statements through one SQLAlchemy session inside one explicit read transaction; the count is the first read that establishes the SQLite read view, and the transaction remains open through the bounded list query. The predicate is constructed once and reused by both statements. Under the current production/default rollback-journal configuration, a conflicting writer cannot complete its commit while that reader transaction is open, so the response remains internally consistent and the writer completes after the reader releases the transaction. A separate file-backed WAL fixture may verify SQLite snapshot semantics where a writer can commit between the two reads, but WAL is not introduced or required as production configuration by Stage 9.

Offset pagination can still shift across separate HTTP requests when new posts arrive between page fetches. Stage 9 accepts that cross-request trade-off because the existing followers collection and public URL style use numbered pages, the requirement is discoverability rather than immutable export, and `(published_at, id)` gives deterministic ordering within each response. Documentation must not promise immutable traversal. A cursor contract would better resist insertion shifts but would introduce opaque public tokens and a new compatibility contract without a current requirement. Tests must prove snapshot consistency within one response, no duplicates inside one page, and deterministic tie-breaking.

### Query cost

Outbox requests perform one count and one bounded page query. The implementation must inspect `EXPLAIN QUERY PLAN` on representative data. If SQLite cannot use an existing index, add a composite index beginning with actor ownership and deleted state, followed by ordering columns, for example:

```text
(actor_username, deleted_at, published_at DESC, id DESC)
```

Do not add speculative indexes for inbound or delivery tables. The selected index must be migration-safe and covered by clean-schema plus upgrade tests.

### Rendering consistency

Outbox items must call the same `buildPublishedCreateActivityJson()` and `buildPublishedActivityObjectJson()` used by direct dereference routes. Duplicating JSON assembly in the server route would risk addressing, actor canonicalization, HTML escaping, or title differences.

### Deleted and pending mutations

A row with `desired_kind = 'delete'` but `deleted_at IS NULL` still represents a currently public object until the Delete reaches the stage’s durable terminal transition. It remains in the outbox. Once `deleted_at` is set, it disappears. This aligns collection membership with committed current state and avoids hiding content merely because a retryable Delete is pending.

### Retry races

Operator retry must not reset `sending` rows or overwrite a worker claim. Exact retry requires a delivery or source ID; username-wide replay requires the explicit `--all-failed` mode and a maximum batch of 100. Selection and compare-and-set updates occur in one immediate transaction ordered by delivery ID. This bounds lock duration and the number of rows made immediately due. Resetting a failed older update cannot bypass a newer Delete because the normal claim logic retains its ordering/supersession checks.

### Filesystem cleanup and restore

Media deletion is irreversible. Cleanup must reuse current locks/reference checks and remain idempotent. Backup restore must be tested from a clean directory and under interruption markers. Docker volumes and bind mounts persist outside container images and require explicit backup; deployment documentation must not imply image rollback restores data.

### Retention and privacy

Outbox publication makes durable authored content discoverable by actor history. Inbound comments remain stored even when Discord delivery is suppressed or terminally fails. Documentation must distinguish bridge-local deletion/retention from copies already fetched by remote servers.

## Architecture

### 1. Outbox read model in Python

Extend `ActivityPubObjectRepository`, which already owns `PublishedActivityObject` resolution, with one snapshot-bound operation rather than independently callable count/list methods:

```python
@dataclass(frozen=True)
class UserOutboxSlice:
    total_items: int
    items: tuple[PublishedActivityObject, ...]

def load_user_outbox_slice(
    *,
    user: User,
    offset: int,
    limit: int,
) -> UserOutboxSlice:
    ...
```

The operation owns one SQLAlchemy session and one explicit SQLite read transaction from the first count read through the bounded list read. It builds the eligibility expression once and applies that same expression to both statements. This repository boundary prevents callers from accidentally combining totals and rows from different snapshots; no public `count_user_outbox_items()` or `list_user_outbox_items()` pair is added.

Repository predicate:

```python
PublishedActivityObject.actor_username == user.activitypub_username
PublishedActivityObject.actor_url.in_((user.actor_url, canonical compatible stored URL if required by existing migration history))
PublishedActivityObject.kind.in_(("post", "comment"))
PublishedActivityObject.deleted_at.is_(None)
```

Before implementation, inspect actual historical actor URL variants produced by previous stages. Prefer a one-time compatibility migration to canonicalize rows only if it is proven safe and required; otherwise use the existing gateway canonicalization while requiring username ownership. Do not accept arbitrary same-origin actor URLs.

Return full ORM rows because the internal DTO and gateway renderer need the existing fields. No body/content transformation occurs in Python.

### 2. Internal Fedify API DTO

Add:

```python
class UserOutboxPageResponse(BaseModel):
    outbox_url: str  # persisted canonical User.outbox_url
    total_items: int
    page: int
    page_size: int
    items: list[PublishedObjectResponse]
```

and:

```text
GET /internal/fedify/actors/users/{username}/outbox?page=N&page_size=40
```

The route:

- applies the existing internal bearer authorization and `Cache-Control: no-store`;
- rejects `page < 1` and any page size other than 40;
- resolves the user or returns 404;
- calls `load_user_outbox_slice()` once, so count and page rows come from one repository-owned read transaction and one SQLite snapshot;
- maps rows through one shared `published_object_payload()` helper also used by `/published-objects/resolve`, preventing DTO drift;
- returns the resolved user’s persisted canonical `outbox_url`; it never reconstructs collection identity from the request path;
- includes no private key, delivery state, profile data, Discord content beyond already-public authored body fields, or inbound conversation rows.

### 3. Gateway collection rendering

Extend `PythonBridgeClient` with `loadUserOutbox(username, page, pageSize)` and a typed `UserOutboxPageRow`.

Add a focused renderer module such as `fedify-gateway/src/user-outbox.ts` responsible only for:

- canonical root/page URL construction exclusively from the DTO’s persisted `outbox_url`, never from the incoming route URL;
- `OrderedCollection` and `OrderedCollectionPage` JSON-LD shape;
- mapping each returned row through `buildPublishedCreateActivityJson()`;
- `first`, `next`, and `prev` links.

Route flow:

```text
/users/:username/outbox[?page=N] or /actors/:identifier/outbox[?page=N]
  -> resolve the same registered User / internal 404 boundary
  -> load one bounded page DTO containing persisted canonical outbox_url
  -> render collection or page with id/first/next/prev rooted at that outbox_url
```

To avoid two internal calls on the root, the root may load page 1 and use its `totalItems` while omitting `orderedItems`. This keeps one bounded query and one count. The `/users/:username/outbox` compatibility alias delegates to the same function as canonical `/actors/:identifier/outbox` after both resolve the same User Actor. Non-user local actors preserve their existing behavior. The renderer must ignore the alias request path when emitting collection and navigation identifiers.

Use plain JSON-LD objects or Fedify vocabulary objects consistently with the existing followers renderer. Do not expose a `page_size` query parameter publicly.

ActivityStreams contract: actor outboxes are `OrderedCollection`s of activities, and collection pages use ordered membership and navigation links. Mastodon interoperability must be checked against its current ActivityPub documentation, but no Mastodon-only extension is required.

### 4. Delivery diagnostics repository and CLI

Add repository methods:

```python
list_diagnostics(username: str | None, status: str | None, limit: int) -> list[DeliveryDiagnostic]
retry_failed_by_delivery_id(delivery_id: int, username: str | None) -> DeliveryRetryResult
retry_failed_by_source_id(source_id: int, username: str | None) -> DeliveryRetryResult
retry_failed_batch(username: str, limit: int) -> DeliveryRetryResult
```

`DeliveryDiagnostic` is a non-ORM immutable DTO containing only approved fields. Error strings pass through the existing bounded-error policy and receive an additional control-character/newline normalization before CLI output.

Add `src/user_actor_delivery_admin.py` with `argparse`, a `main()` entry point, explicit database URL input/default consistent with existing CLI patterns, table or JSON output, and deterministic exit codes. Register `bridge-user-deliveries` in `pyproject.toml`.

The command opens only `Database`; it does not construct Discord, gateway, HTTP clients, policy services, or load application secrets. This is the process-level secret-minimization boundary.

### 5. Media cleanup CLI

Add `src/profile_media_admin.py` as an adapter that constructs only:

- `Database`;
- `LocalProfileMediaStore` using explicit media root;
- `UserProfileService` dependencies required by `cleanup_orphans()`;
- deployment/media lock paths.

If `UserProfileService` construction currently requires delivery/gateway dependencies irrelevant to cleanup, extract a narrow `ProfileMediaCleanupService` around the existing cleanup method rather than passing dummy network services. Ownership of reference release and deletion remains in the same domain module/repositories.

Register `bridge-profile-media` in `pyproject.toml`. Do not pass Discord/Fedify secrets to this process or Compose service.

### 6. Migration/index strategy

No data-model column is expected. Add only the measured composite outbox index if required. Implement it in both:

- SQLAlchemy model `Index` metadata for clean databases;
- `src/db/migrations.py` with `CREATE INDEX IF NOT EXISTS` for upgrades.

Migration tests must start from the exact Stage 8 schema/bundle state, preserve all rows, and show the new query/index contract. No table rebuild is justified for an index-only change.

### 7. Documentation ownership

Update only documents whose purpose covers the contract:

- `README.md`: user-visible final feature summary, data/privacy overview, and pointers to detailed docs.
- `docs/FEDERATION.md`: actor outbox shape, inclusion/deletion policy, profile-cache and delivery behavior.
- `docs/DEPLOY.md`: cleanup/retry commands, complete backup/restore, both Compose paths, secret-minimized one-shot execution.
- `docs/architecture/overview.md`: final durable authored timeline/read flow and operational boundaries.
- `docs/architecture/http-routes.md`: User Actor outbox now paginated; internal outbox endpoint.
- dashboard documentation only if it currently claims operational delivery visibility; do not add sensitive diagnostics to the dashboard.
- `notes/known_issues.md`: add the inactive/removed binding retained-comment behavior as a current limitation/verified behavior.
- `dev/future_tasks.md`: add restoration of durable personal-feed publication/comment history into Discord after channel loss or rebinding, unless an equivalent item already exists; avoid duplicates.
- relevant test coverage/navigation documents when their stated purpose requires new paths.

## Reasonable Alternatives Considered

### Outbox objects versus Create activities

Chosen: `OrderedCollection` of complete `Create` activities containing current objects. ActivityPub actor outboxes are activity streams, existing canonical Create routes already exist, and reuse prevents identity drift.

Rejected: bare Page/Note objects. Some consumers tolerate object collections, but it weakens alignment with the actor outbox contract and bypasses existing canonical Create activity rendering.

### Append-only mutation history

Chosen: one current Create/object entry per undeleted durable authored object.

Rejected: include Update/Delete activities. Current persistence is optimized for delivery state and object current state, not a complete immutable activity log; synthesizing history would be incomplete and potentially misleading. Adding an append-only log now is a new persistence foundation without a defined next-stage requirement.

### Cursor versus numbered pagination

Chosen: fixed-size numbered pages with deterministic tie-breaking and a snapshot-consistent count/page load for each response, matching current followers collection style. Separate page requests are not promised to form an immutable traversal.

Rejected: opaque cursor pagination. It better resists insertion shifts across requests but introduces a new public token contract and more migration/query complexity than required for this bounded public timeline. This recommendation is not applied because no current or defined later stage requires immutable export semantics, while canonical numbered links are already part of the project’s collection conventions.

### SQLite rollback journal versus mandatory WAL

Chosen: preserve the current runtime/default SQLite journal mode. The single repository-owned read transaction gives an internally consistent response because, in rollback-journal mode, conflicting writer commit is serialized until the reader releases its transaction. Tests cover this production behavior directly with a file-backed database.

Rejected: making WAL mandatory solely to permit a writer commit between the two outbox reads. Stage 8 runtime initializes `foreign_keys` but does not establish WAL as a deployment invariant, and Stage 9 has no requirement to change database concurrency, upgrade, backup, or Compose semantics. A dedicated WAL fixture is retained only to verify the stronger SQLite snapshot behavior without changing supported production configuration.

### Public/dashboard diagnostics versus local CLI

Chosen: local CLI with database-only access.

Rejected: dashboard or HTTP endpoint, because authorization, privacy, CSRF/authentication, and secret exposure would expand substantially.

### New cleanup daemon versus one-shot CLI

Chosen: one-shot CLI reusing existing locking and cleanup service.

Rejected: embedded scheduler/daemon, because multi-process scheduling and shutdown ownership are unnecessary for Stage 9.

## Touched Files

- src/models.py
- src/db/migrations.py
- src/db/database.py
- src/db/repositories/activitypub_objects.py
- src/db/repositories/user_actor_deliveries.py
- src/db/repositories/__init__.py
- src/internal_fedify_api.py
- src/user_profiles.py (only if cleanup responsibility must be extracted without semantic change)
- pyproject.toml
- fedify-gateway/src/python-bridge-client.ts
- fedify-gateway/src/server.ts
- fedify-gateway/src/published-objects.ts (only for exported/shared renderer typing; no duplicate renderer)
- fedify-gateway/tests/verify-user-outbox.ts or the repository’s corresponding gateway test manifest
- tests/test_internal_fedify_api.py
- tests/test_migration_completeness_report.py
- tests/behavior/test_profile_media_backup_scenarios.py
- tests/behavior/test_profile_media_scenarios.py
- tests/behavior/test_user_actor_delivery_worker_scenarios.py
- tests/test_docker_deployment.py
- README.md
- docs/FEDERATION.md
- docs/DEPLOY.md
- docs/architecture/overview.md
- docs/architecture/http-routes.md
- docs/development/navigation.md
- docs/development/test-coverage-map.md
- notes/known_issues.md
- dev/future_tasks.md

The exact test manifest/file names must follow the current gateway test convention found during implementation. Do not touch a listed documentation file if its purpose paragraph proves the contract belongs elsewhere.

## New Files

- plans/142_user_actor_outbox_operational_hardening_stage9.md
- fedify-gateway/src/user-outbox.ts
- tests/behavior/test_user_actor_outbox_scenarios.py
- tests/behavior/test_user_actor_delivery_admin_scenarios.py
- tests/behavior/test_profile_media_admin_scenarios.py
- src/user_actor_delivery_admin.py
- src/profile_media_admin.py

A separate Python repository file is not planned; outbox queries stay with `ActivityPubObjectRepository`.

## Implementation Steps

1. **Pin Stage 8 baseline behavior with failing outbox scenarios.**
   - Build registered users and durable community/personal-feed post/comment rows through existing publication handlers where practical.
   - Assert current empty route fails expected Stage 9 behavior before production changes.
   - Add adversarial rows: another user, inbound remote comment, deleted row, pending Delete (`desired_kind='delete'`, `deleted_at=None`), equal timestamps, and Discord-only message.

2. **Implement shared outbox repository predicate and measured index.**
   - Add `load_user_outbox_slice()` to `ActivityPubObjectRepository`; do not expose independent count/list operations.
   - Build the eligibility predicate once, execute count and ordered bounded list inside one explicit repository-owned read transaction, and apply offset/limit only after all eligibility filters.
   - Run representative `EXPLAIN QUERY PLAN`; add the smallest composite index only when it changes the plan from scan/temp sort to indexed bounded access.
   - Add clean-schema and exact Stage 8 upgrade coverage for any index.

3. **Add internal outbox DTO and endpoint.**
   - Refactor published-object DTO mapping into one helper.
   - Resolve the User once and return its persisted canonical `outbox_url` in every DTO, including requests reached through `/users/...`.
   - Invoke `load_user_outbox_slice()` exactly once per response so totals, membership, and navigation derive from one read snapshot.
   - Enforce page and fixed-size validation.
   - Confirm internal responses use `no-store` and require the existing shared-secret bearer.
   - Confirm no keys or delivery fields appear in serialized output.

4. **Add typed gateway client support.**
   - Define `UserOutboxPageRow` and parse every field strictly.
   - Preserve internal 404 behavior for unknown users and fail closed on malformed Python payloads.

5. **Implement one shared User Actor outbox renderer.**
   - Render root `OrderedCollection` and numbered `OrderedCollectionPage`.
   - Reuse `buildPublishedCreateActivityJson()` for every item.
   - Delegate `/users/:username/outbox` and User Actor `/actors/:identifier/outbox` to the same function.
   - Use the DTO’s persisted canonical `outbox_url` as the sole base for collection `id`, `first`, `next`, and `prev`; do not rebuild those links from `req.url`, route parameters, or alias spelling. Validate that the persisted URL belongs to the configured Fedify origin before rendering and fail closed on inconsistent internal data.
   - Preserve non-user `/actors/:identifier/outbox` behavior.

6. **Add delivery diagnostic/retry repository operations using TDD.**
   - List bounded sanitized diagnostics with filters.
   - Provide exact retry by delivery ID or source ID, with optional username ownership verification.
   - Provide username-wide retry only as a deterministic delivery-ID-ordered batch capped at 100 rows.
   - Perform selection and conditional reset of failed rows in one immediate transaction; return matched/reset/skipped/remaining counts.
   - Verify normal claim ordering still handles superseded updates, pending Deletes, removed followers, policy rejection, and worker restart.

7. **Add `bridge-user-deliveries` CLI.**
   - Reject retry without `--delivery-id`, `--source-id`, or the complete `--username --all-failed --limit` bulk form before opening a write transaction.
   - Reject incompatible selector combinations and limits outside `1..100`.
   - Use deterministic output, including matched/reset/skipped/remaining for bulk batches.
   - Open database-only dependencies.
   - Return clear exit status for success, partial skips, invalid selector, ownership mismatch, and unavailable DB.
   - Do not log activity body/snapshot/private material.

8. **Add profile-media cleanup CLI around the existing cleanup owner.**
   - Extract a narrow cleanup service only if necessary to avoid constructing unrelated delivery dependencies.
   - Validate path/lock/grace arguments before deletion.
   - Verify dry failure paths cannot partially remove referenced media.

9. **Validate complete backup/restore with outbox and media.**
   - Create profile media, authored objects in both contexts, follower and delivery rows.
   - Produce complete archive, restore into a clean data root, initialize runtime/gateway reads, and assert profile bytes plus outbox content/count/order survive.
   - Exercise interrupted-restore recovery and reject tampered/incomplete archives.

10. **Update responsible documentation and journals.**
    - Add exact operator commands and data boundaries.
    - Add inactive-binding comment behavior to `notes/known_issues.md`.
    - Add Discord-history restore task to `dev/future_tasks.md` if absent.
    - Remove no completed future-task items unless Stage 9 actually implements them.

11. **Run regression/performance/security validation.**
    - Execute focused Stage 9 tests first, then all existing Python and gateway tests.
    - Inspect query plans and benchmark representative 10k/100k authored-row fixtures without setting an unrealistic absolute CI time threshold; assert bounded result size and indexed plan instead.
    - Search generated docs/log snapshots for private key material, shared secret, Discord token, OAuth secret, raw activity bodies in diagnostics, and control-character log injection.

12. **Perform stage completion verification without implementing Stage 10 work.**
    - Compare implementation point-by-point with this plan, umbrella Stage 9, AGENTS.md, and orchestrator.
    - Confirm only Stage 9 contracts changed and no temporary adapter/duplicate outbox renderer remains.
    - Verify clean working tree, commit authorship, final commit, bundle, clean restore, parent/tree/history as required by orchestrator during implementation completion. Plan-only delivery stops before these implementation actions.

## Tests

### Outbox behavior scenarios

1. Registered user with no eligible authored rows gets `totalItems: 0`, no `first`, and page 1 is empty.
2. Mixed community and personal-feed roots/replies authored by one user all appear newest-first.
3. Another registered user’s rows do not leak even with manipulated same timestamp/context.
4. Remote-authored inbound comments and Discord-only messages never appear.
5. Deleted rows are excluded; a retryable pending Delete remains until `deleted_at` commits.
6. Equal `published_at` values are ordered by descending primary key with no duplicate in one page.
7. 41 rows produce 40 items on page 1, one on page 2, correct `next`/`prev`, and exact total.
8. Unknown user returns 404; page 0/non-integer returns 400; large positive page returns an empty page.
9. `/users/alice/outbox` and `/actors/alice/outbox` expose the persisted `/actors/alice/outbox` as the identical root/page `id` base and produce identical `first`/`next`/`prev` links and item IDs.
10. In a file-backed fixture using the current production/default rollback-journal mode, the reader performs count and holds the transaction while a second connection attempts to insert. A deterministic barrier proves the writer cannot complete commit before the reader finishes list; the response is internally consistent, and the writer commits after the reader transaction ends.
11. Repeat the rollback-journal scenario for a committed deletion attempt (`deleted_at` update): the writer cannot complete commit while the reader transaction is open, the response remains internally consistent, and the deletion commits after reader completion.
12. In a separate file-backed fixture that explicitly enables `PRAGMA journal_mode=WAL`, allow an insert and a deletion writer to commit between count and list and verify that the reader stays on the snapshot established by count. This is a SQLite snapshot-semantics test only, not evidence that WAL is the current production mode and not authorization to change runtime configuration. Use SQLAlchemy events/barriers or equivalent deterministic hooks, never timing sleeps.
13. Outbox item JSON equals the corresponding canonical direct Create route JSON for community post, community comment, personal-feed root, and personal-feed reply.
14. Existing object escaping/addressing is preserved for hostile Discord markdown/HTML input.
15. Actor/profile/followers and community outbox routes remain unchanged.

### Internal API and trust boundary

1. Missing/wrong internal bearer is rejected.
2. Response has `Cache-Control: no-store`.
3. Fixed page size enforcement works.
4. DTO does not contain private keys, delivery endpoints, snapshots, Discord tokens, or inbound content.
5. Malformed internal payload causes gateway failure rather than partial/unsafe rendering.

### Delivery diagnostics/retry

1. `list` filters by username/status and enforces limit.
2. Output contains approved metadata and sanitized bounded error only.
3. Selector-less retry and username-only retry fail parser validation before any write transaction.
4. Exact retry accepts delivery ID or source ID, optionally verifies username ownership, and resets only matching failed rows without changing source/activity/logical version/audience snapshot.
5. Bulk retry requires `--username`, `--all-failed`, and `--limit`; limits outside `1..100` fail validation.
6. Bulk selection is deterministic by delivery ID, resets at most the requested bounded batch in one immediate transaction, and reports matched/reset/skipped/remaining accurately.
7. Delivered, ineligible, superseded, and sending rows are skipped; concurrent worker claim versus retry yields one owner and no stolen claim.
8. Repeated bulk invocations advance through separate batches without resetting the same row twice unless it fails again through the normal worker state machine.
9. Retried older update remains blocked/superseded correctly by newer mutation rules.
10. Removed follower/policy-blocked endpoint remains ineligible through normal worker revalidation.
11. Restart processes reset rows through the normal worker, not a separate send path.
12. CLI process can run with database configuration only and does not require Discord/Fedify secrets.

### Media cleanup and backup/restore

1. Current avatar/header and executable source-retained files survive cleanup.
2. Old unreferenced file+row is removed after grace period.
3. Young orphan survives.
4. Missing orphan file cleanup is idempotent.
5. Lock contention, invalid ownership, symlinked path, and unwritable media root fail before deletion.
6. Complete backup restores DB, profile media, outbox rows/order/deletion state, followers, and delivery state.
7. DB-only/incomplete archive is rejected or documented test fixture proves why it is incomplete.
8. Interrupted restore recovery returns to one valid complete state.
9. Compose backup/cleanup one-shot service/environment does not receive application secrets beyond required paths/config.

### Migration and performance

1. Clean schema contains the selected outbox index, if added.
2. Exact Stage 8 schema upgrades without data loss.
3. Migration is idempotent.
4. Representative query plan uses the intended index and avoids full-table sort/scan for one user’s bounded page.
5. Count and list statements reuse one predicate and one transaction. In the actual file-backed production/default rollback-journal mode, deterministic barriers prove a conflicting insert or deletion writer cannot complete commit until the reader transaction has finished both statements; the response’s `totalItems`, items, and `next` remain consistent, and the writer succeeds afterward.
6. A separate file-backed WAL fixture explicitly sets `PRAGMA journal_mode=WAL` and proves that, when a writer can commit between count and list, the reader remains on the snapshot established by count. This test is labeled as SQLite snapshot-semantics coverage and must not alter or imply a Stage 9 production journal-mode requirement.

### Full regression validation

Run, from the repository’s supported environment:

```text
python -m compileall src tests tools
ruff check src tests tools
ruff format --check src tests tools
pytest
npm --prefix fedify-gateway run check
npm --prefix fedify-gateway test
```

Also run repository-specific assurance, migration, Docker/deployment, gateway contract, and bundle checks discovered from current CI/project scripts. Do not substitute focused Stage 9 tests for the complete suite.

Manual protocol checks against a restored clean instance:

```text
GET /actors/alice
GET /actors/alice/followers
GET /actors/alice/outbox
GET /actors/alice/outbox?page=1
GET each returned Create id
GET each embedded object id
GET restored profile media URL
```

Validate ActivityStreams content type, canonical IDs, page links, count, ordering, and no secret-bearing fields.

## Regression and Blind-Spot Analysis

- **Canonical identity drift:** seed/resolve the persisted `User.outbox_url`, request both aliases, and assert all collection/navigation identifiers use that persisted URL rather than either request path; compare direct Create/object routes byte-for-structure after JSON normalization.
- **Old actor URL rows:** test real prior-stage rows before choosing compatibility predicate or migration.
- **Pending publication leakage:** prove no current failure path inserts an externally undiscoverable `PublishedActivityObject`; if found, add an explicit eligibility state foundation in this stage because outbox correctness depends on it and it cannot be inferred safely.
- **Mutation race:** read during pending/committed Delete must follow `deleted_at`, not delivery attempt status.
- **Cross-user denormalization:** adversarial rows must not leak through username-only matching if actor ownership is inconsistent.
- **Outbox amplification:** root request does not embed all items; pages are fixed-size; no caller-controlled page size.
- **Internal endpoint abuse:** authorization and bounded pagination prevent untrusted public callers from using Python API directly.
- **Diagnostic data exposure:** no dashboard/public API integration; errors sanitized; no payload bodies/keys.
- **Retry corruption:** conditional status updates and existing worker ordering prevent claim theft or ID regeneration.
- **Cleanup race:** media lock plus immediate DB transaction preserves currently referenced/source-retained files.
- **Backup mismatch:** archive restore is validated with both DB and media and clean runtime dereference.
- **Deployment divergence:** test image-based and build-based Compose overlays, paths, user IDs, volumes, and one-shot commands.
- **Inactive feed misunderstanding:** documentation and known issue explicitly state stored-but-not-rendered comments and no automatic replay after rebinding.
- **Remote retention:** docs state that local deletion/outbox removal cannot recall remote copies.

## External Contracts Rechecked

Implementation must re-open and cite current primary documentation in code-review evidence:

- W3C ActivityPub Recommendation: actor `outbox` is an `OrderedCollection`; collection paging and ordered items follow ActivityStreams collection semantics.
- Mastodon ActivityPub documentation: interoperability uses standard actor/outbox/activity/object structures; no Mastodon-specific outbox extension is required for this stage.
- Fedify 2.2 documentation/source matching the locked package version: confirm vocabulary serialization behavior if Fedify objects are used; otherwise use explicit JSON-LD consistent with existing gateway collection renderers.
- Docker storage/volume documentation: container images and `docker export` do not include mounted volume data; DB/media volumes require explicit backup and restore.

Primary references:

- https://www.w3.org/TR/activitypub/
- https://www.w3.org/TR/activitystreams-core/
- https://docs.joinmastodon.org/spec/activitypub/
- https://fedify.dev/
- https://docs.docker.com/engine/storage/volumes/
- https://docs.docker.com/reference/cli/docker/container/export/

## Handoff Contract

After Stage 9 implementation and independent verification, the repository guarantees:

- a public, bounded, paginated User Actor outbox sourced only from durable public bridge-owned authored objects;
- stable canonical actor/activity/object identity and existing mutation ownership;
- deleted-object exclusion with no fabricated mutation history;
- local non-secret delivery diagnostics and safe retry reset through the existing worker;
- operational media orphan cleanup using existing ownership/locking rules;
- verified complete DB-plus-profile-media backup/restore;
- documented privacy, abuse, inactive-binding, deployment, and cache behavior;
- no temporary adapter, duplicate renderer, broken caller, or dependency on a future stage to make Stage 9 releasable.

No assumptions are handed to a Stage 10; this is the final configured umbrella stage.

## Open Questions

None. The umbrella plan and current Stage 8 persistence establish enough information to select the outbox membership, deletion policy, pagination style, diagnostics boundary, and cleanup ownership without a coordinator product decision.
