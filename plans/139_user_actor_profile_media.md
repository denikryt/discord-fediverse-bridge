# 139 — profile media storage: User Profile Schema and Durable Media Foundation

## Problem / Goal

The current `users` row owns the stable Discord-to-ActivityPub identity, actor URLs, and signing keys, but it has no editable profile source of truth. The deployment persists only `/data/bridge.db`, while `src.db.backup` snapshots only SQLite. Profile images therefore cannot safely be implemented by retaining Discord CDN URLs or writing files outside the existing durable-volume and backup contract.

profile media storage adds only the durable profile and media foundation required by physical surface mapping. It must not change actor JSON, Discord commands, Follow handling, content delivery, personal-feed routing, or user outbox behavior.

The completed implementation must provide:

- nullable stored display name with an effective fallback to immutable `activitypub_username`;
- nullable summary;
- ordered storage for zero to four profile metadata rows;
- nullable avatar and header references to immutable bridge-owned media objects;
- secure image ingestion, validation, replacement, clearing, public retrieval, and conservative orphan cleanup;
- migrations for existing databases and clean-schema creation;
- a complete DB-plus-media backup and restore contract;
- unchanged external federation behavior.

## Current-State Findings and Risk Dimensions

### Current ownership and runtime

- `src.models.User` is the canonical registered-user identity record.
- `src.registration_service.RegistrationService` creates users; `src.db.repositories.users.UserRepository` owns identity lookups.
- `src.app.build_runtime()` runs `Database.create_all()` and `Database.migrate()` before constructing long-lived services.
- `src.http_api.create_http_app()` already owns public Python HTTP routes. It is the correct boundary for media reads; the Fedify gateway should receive only stable public URLs in physical surface mapping, not filesystem access.
- `compose.yaml` mounts one named volume at `/data` into the Python bridge and backup containers. The gateway has no volume access.
- `src.db.backup` creates standalone SQLite snapshots, which becomes incomplete once database rows reference files.
- The project currently has no image-decoder dependency or media-storage abstraction.

### Function-specific risks

1. **Untrusted bytes and decompression attacks.** Request MIME and filenames are attacker-controlled. Validation must fully decode content, impose compressed-byte and decoded-pixel limits, reject malformed/truncated/multi-frame input, and treat decompression-bomb warnings as errors.
2. **Path traversal and accidental file exposure.** Original filenames, actor names, URLs, and request path segments must never become storage paths. Public retrieval must resolve an opaque persisted ID through the database.
3. **Database/filesystem split-brain.** SQLite transactions cannot atomically commit file writes. Bytes must be fully written, fsynced, and atomically renamed before a DB reference can commit. A failed DB commit may leave only an unreferenced immutable file, never a committed reference to missing bytes.
4. **Replacement and cleanup races.** Immediate unlink on replacement can race backups, HTTP readers, or a concurrent mutation. A read-only liveness check is insufficient under SQLite because it does not reserve the following write. Media objects must be immutable; replacement changes a reference, while physical deletion uses an explicit lock/transaction protocol shared by cleanup and backup.
5. **Backup/restore generation races.** Backup must copy media referenced by the completed SQLite snapshot, not by the live database or directory enumeration, and it must never run while restore or startup recovery switches the live DB/media generation. The separate Compose processes therefore need one deployment-state exclusion domain in addition to the media-lifecycle lock. A missing or hash-mismatched referenced file must fail backup publication.
6. **Concurrency.** Concurrent profile writes must not lose unrelated fields, violate the four-field invariant, or delete the winning reference.
7. **Deployment and permissions.** The non-root container user (`10001`) must own the media directories under `/data`. Startup must fail clearly for unsafe or unwritable paths.
8. **Public caching and privacy.** Profile media is public once referenced. URLs and logs must not reveal original filenames, Discord URLs, local paths, profile text, secrets, or signing keys. Replacement must produce a new immutable URL.
9. **Scope leakage.** Persisted fields must not be added to gateway actor serialization or trigger federation activity in this work.

## Expected Behavior

### Profile defaults

For a user with `activitypub_username = "alice"` and no stored profile customization:

```text
stored display_name = NULL
effective display_name = "alice"
summary = NULL
fields = []
avatar = NULL
header = NULL
```

Empty display-name or summary input is normalized to `NULL` at the profile-service boundary. The username, actor URL, inbox, outbox, followers URL, and keypair remain unchanged.

### Ordered metadata rows

Use a dedicated table rather than unconstrained JSON:

```text
user_profile_fields
- id
- user_id
- position        # 0..3
- name
- value
- created_at
- updated_at
UNIQUE(user_id, position)
CHECK(position >= 0 AND position <= 3)
```

The profile service replaces all rows for one user inside the same transaction as scalar profile changes. This establishes ordering and the four-row cap before physical surface mapping renders Mastodon `PropertyValue` entries.

### Immutable media records

Add:

```text
profile_media_assets
- id
- public_id             # random URL-safe opaque identifier, unique
- storage_key           # generated relative path, unique, never user-controlled
- media_kind            # avatar | header
- content_type          # decoder-derived canonical MIME
- byte_size
- width
- height
- sha256_hex
- created_at
```

Add nullable `users.avatar_media_id` and `users.header_media_id`. The selected asset kind must match the slot. The public URL is derived from `PUBLIC_BASE_URL` and `public_id`, for example:

```text
https://bridge.example.com/media/profile/<opaque-public-id>
```

Neither original filename nor `storage_key` appears in the URL.

### Media ingestion and mutation protocol

The filesystem and relational responsibilities are deliberately separate:

```text
ProfileMediaStore.stage_and_publish(kind, source_stream)
    -> PublishedMediaObject

UserProfileService.replace_media(user_id, slot, source_stream)
    -> store.stage_and_publish(...)
    -> one caller-owned database transaction:
         ProfileMediaRepository.insert(session, published_object)
         UserProfileRepository.switch_media_slot(session, user_id, slot, asset_id)
```

`ProfileMediaStore.stage_and_publish()` must:

1. stream into `<media_root>/.staging/` while enforcing the configured compressed-byte limit;
2. decode with Pillow, convert `DecompressionBombWarning` to a validation failure, allow only still JPEG/PNG/WebP, and reject animation/multiple frames;
3. enforce non-zero dimensions and the project-owned decoded-pixel limit before full pixel materialization;
4. normalize orientation and re-encode to canonical JPEG for opaque images or PNG when alpha is retained, stripping source metadata;
5. hash canonical bytes, fsync the file and containing directory, and atomically rename to a generated immutable key under `<media_root>/objects/`;
6. return decoder-derived metadata and the immutable storage key without opening or committing a database session.

`UserProfileService.replace_media()` then inserts the asset row and switches exactly one selected user slot in one service-owned transaction. Repository transaction-scoped methods accept the caller-owned SQLAlchemy `Session`; they must not open or commit independent sessions. The existing convenience repository methods may retain their current one-operation session ownership only for unrelated callers.

The protocol must preserve the previous profile/reference on every validation, storage, or database failure. A failure after durable rename but before database commit may leave only an unreferenced immutable object for cleanup. Replacement changes only the selected slot. Clearing sets only that slot to `NULL` in one transaction. No profile media storage public method may attach an arbitrary existing orphan asset to a user.

### Public media route

Add `GET /media/profile/{public_id}` to the existing FastAPI app. The handler must:

- validate public-ID syntax;
- load a persisted asset by `public_id`;
- resolve `storage_key` beneath the configured root and reject escaped paths or symlinks;
- return 404 for unknown rows or missing files;
- return canonical `Content-Type`, exact `Content-Length`, `X-Content-Type-Options: nosniff`, and immutable cache headers;
- never list files or expose staging, orphan, manifest, DB, or arbitrary filesystem content.

Do not mount the media directory with `StaticFiles`; mounted static paths handle every subpath and would bypass database ownership checks.

### Cleanup, retention, and deletion exclusion

- A media object is live when referenced by either user media slot.
- Runtime replacement/clear does not synchronously unlink superseded files.
- Candidate discovery may occur without locks, but it is advisory only.
- Cleanup running inside the main bridge process executes under the runtime's already-held lifetime shared deployment-state lease; it must not reacquire or close a separate descriptor for the same `flock`, because process-level advisory-lock semantics could accidentally alter the runtime lease. A standalone cleanup CLI first acquires a shared deployment-state lock on `/data/deployment-state.lock`. Both forms then acquire an exclusive POSIX advisory media-lifecycle lock on `/data/profile-media.lock`, open a service-owned SQLAlchemy session, and explicitly start `BEGIN IMMEDIATE`.
- While the lifetime/shared deployment-state lease, exclusive media-lifecycle lock, and SQLite write transaction remain held, cleanup rechecks both user media slots. If the asset is live, cleanup performs no filesystem or metadata change. If it is still unreferenced and older than the configured grace period, cleanup resolves the contained immutable path, unlinks it, deletes the metadata row, and commits.
- `BEGIN IMMEDIATE` is mandatory for this path: a deferred transaction or a SELECT followed later by a write does not close the race. All slot switches and clears write the same SQLite database, so they cannot commit while cleanup holds the write transaction.
- An unlink failure rolls back and retains the row for retry. A missing orphan file counts as already unlinked and permits row deletion. If database commit fails after a successful unlink, the remaining unreferenced row is retryable and must never be attachable through a public service method.
- Global lock acquisition order is always `deployment-state lock -> profile-media lifecycle lock -> SQLite transaction`. Profile mutations take only the SQLite write transaction because they neither switch deployment generations nor delete immutable files. No code path may acquire these locks in reverse order.
- Cleanup must not follow symlinks or remove recent staging files.
- profile media storage provides and tests the operation but does not schedule it automatically; outbox maintenance owns final operational scheduling/diagnostics.

This exclusion protocol is selected instead of a durable `deleting` column. The current deployment has one SQLite database and local immutable files, so `BEGIN IMMEDIATE` provides the necessary writer exclusion without adding a future coordination state. A durable claim would be justified only if a later storage backend could not share the same database lock.

### Complete backup and restore

Replace the assumption that one `.sqlite3` file is the complete backup with a versioned archive while retaining explicit legacy SQLite restore support.

Backup creation:

1. open `/data/deployment-state.lock` and acquire a shared POSIX advisory lock, then open `/data/profile-media.lock` and acquire its shared lock before creating the SQLite snapshot; backup either blocks until both are available or, for an explicitly configured finite timeout, exits with a controlled non-published-backup error;
2. while retaining that shared lock, create and integrity-check a temporary SQLite snapshot through the existing online backup API;
3. query that snapshot for all referenced media assets;
4. copy exactly those immutable files, verifying size and SHA-256 against snapshot metadata;
5. write a manifest with format version, DB filename, and media entries;
6. fsync and atomically publish one mode-`0600` archive;
7. release the media-lifecycle lock and then the deployment-state lock only after archive publication or complete failure cleanup.

Cleanup requires the runtime-held or CLI-acquired shared deployment-state lease and the exclusive media-lifecycle lock before unlinking, so an asset referenced by the completed snapshot cannot disappear during the media-copy window. The main bridge retains a shared deployment-state lease for its entire live-state lifetime, while restore and startup recovery require the exclusive lock; therefore backup publication, bridge operation, and generation switching cannot overlap incorrectly. Profile replacement and clearing do not take this lock because media objects are immutable and published before their references commit; they may proceed while backup copies the snapshot's older object set.

The lock files are inside the shared `/data` volume and therefore coordinate the separate `bridge`, `backup`, and one-shot restore/recovery processes. POSIX advisory locks are released automatically when the process exits or closes the descriptor, so a crashed backup or cleanup process leaves no durable stale lock. Startup must verify that the configured deployment supports this Unix lock contract; unsupported platforms fail the backup/cleanup command rather than silently running unlocked.

Restore uses a durable, restart-recoverable transaction protocol because `/data/bridge.db` and `/data/profile-media` cannot be switched atomically by one filesystem rename:

1. acquire the exclusive deployment-state lock under `/data`; the running bridge holds a lifetime shared lease from before opening the database/media store until after all workers, HTTP/media readers, database sessions, and database resources are closed, so restore blocks or fails with a controlled timeout before any live-path rename while any bridge process is using the current generation. Stopping the bridge remains the operational way to make restore proceed, but correctness does not depend on operator compliance or container restart timing; the separate restore-command mutex may remain only to reject duplicate operator invocations, not as the generation-exclusion mechanism;
2. before beginning a new restore, recover any existing `/data/restore-state.json`; reject concurrent or ambiguous operations;
3. reject absolute, `..`, duplicate, symlink, device, hard-link, or unexpected archive entries, then validate the manifest, media hashes, and SQLite `integrity_check` without changing live state;
4. extract and fsync the candidate database and media tree into `/data/.restore-generations/<operation-id>/new`; create `/data/.restore-generations/<operation-id>/old`;
5. publish `/data/restore-state.json` **before the first live-path rename**, using write-to-temp, file fsync, atomic rename, and `/data` directory fsync. The marker records the operation ID, validated archive identity, old/new generation paths, expected live paths, expected hashes, and phase `prepared`;
6. move the current database, WAL/SHM sidecars, and media directory into the `old` generation one component at a time. After each rename, fsync both source and destination parent directories and durably advance the marker phase. An absent pre-restore component is recorded explicitly in the marker rather than inferred later;
7. install the restored database at `/data/bridge.db`, fsync the file and `/data`, advance and fsync the phase, then install the restored media directory at `/data/profile-media`, fsync its tree and `/data`, and advance and fsync the phase;
8. run post-install SQLite integrity and referenced-media verification against the live paths. Only after both succeed, set phase `committed` and fsync the marker and `/data`;
9. after durable `committed`, garbage-collect the retained old generation. Removal is restartable and does not change the committed live generation. Remove the marker only after cleanup completes and fsync `/data`;
10. at the start of every restore command and before normal application startup opens the database or media store, run recovery while holding the exclusive deployment-state lock (and the restore-command mutex if retained). For any non-committed phase, recovery deterministically rolls back to the recorded old generation unless the marker proves that both new components were installed and their recorded integrity/hash verification completed, in which case it rolls forward to `committed`. It never accepts a mixed pair;
11. every rename, marker transition, rollback, roll-forward, and generation cleanup is idempotent. Recovery decisions use the durable phase, explicit pre-restore presence flags, and recorded hashes; they never guess from timestamps or whichever live path happens to exist. If neither complete old nor complete verified new state can be established, startup fails closed and retains all evidence for operator recovery.

The marker is durable before any destructive rename, closing the crash window that would otherwise leave moved live state with no recovery record. The restore marker and generation directories are private deployment state, mode `0700`/`0600`, never exposed by HTTP or included as profile media. Catchable exceptions invoke the same recovery state machine rather than a separate best-effort rollback path.

This protocol is selected instead of changing the public `/data/bridge.db` layout to a symlink or generation pointer. The existing deployment, backup scripts, health checks, and operator expectations address that fixed database path; introducing an indirection would be a broader deployment-contract migration. The durable marker provides crash safety while preserving those paths.

Legacy `.sqlite3` restore remains database-only and is valid for pre-media backups. It must warn or reject use as a complete restore when referenced profile media exists.

## Architecture

### `UserProfileRepository`

Owns relational profile state only:

- load the effective profile view;
- update scalar fields and replace ordered metadata rows atomically;
- switch or clear avatar/header references with kind validation;
- expose referenced media IDs for backup and cleanup.

It must not decode images, write files, build URLs, or render ActivityPub.

### `ProfileMediaRepository`

Owns media metadata rows and liveness queries:

- insert immutable asset metadata;
- lookup by `public_id` or ID;
- list referenced assets;
- select and delete safely rechecked orphan rows.

### `ProfileMediaStore`

Owns the local filesystem boundary:

- `stage_and_publish()` for staging, byte/pixel limits, decoding, canonical re-encoding, hashing, fsync, and atomic rename;
- containment-safe path resolution;
- opening a persisted object for HTTP delivery;
- unlinking an orphan only after the cleanup coordinator has established the required locks.

It never inserts rows, switches user references, opens SQLAlchemy sessions, or commits transactions.

It receives only non-secret configuration and no Discord token, OAuth secret, Fedify secret, gateway client, or actor private key.

### `UserProfileService`

Coordinates profile mutations across repositories and storage. Scope-local methods should be reusable by physical surface mapping:

```python
update_profile(user_id, display_name, summary, fields) -> UserProfile
replace_media(user_id, slot="avatar", upload=stream) -> UserProfile
clear_media(user_id, slot="header") -> UserProfile
```

profile media storage does not expose these methods through Discord commands.

### HTTP route

Owns public read delivery only. It receives read-only media lookup/open capabilities through `Runtime`, not mutation methods.

### Backup module

Owns complete deployment-state snapshot and restore. It reads media metadata from the SQLite snapshot and immutable files from the data root; it does not depend on secret-bearing runtime clients. It acquires the shared deployment-state lock first and the shared media-lifecycle lock second before snapshot creation, holding both through archive publication.

### `DeploymentStateLock` and `ProfileMediaLifecycleLock`

Two narrow Unix-only adapters own `fcntl.flock()` acquisition on configured lock files:

- `DeploymentStateLock`: the main bridge holds `LOCK_SH` for the full lifetime of all open live-state users; backup and standalone cleanup use `LOCK_SH`; restore, startup recovery, rollback/roll-forward, and live-generation cleanup use `LOCK_EX`;
- `ProfileMediaLifecycleLock`: backup uses `LOCK_SH`; cleanup uses `LOCK_EX`;
- the runtime descriptor remains open from immediately after startup recovery and before `Database.create_all()` through final shutdown cleanup; operation-scoped descriptors remain open for the full protected backup/restore/CLI cleanup operation;
- context-manager exit and process termination release advisory locks;
- fixed acquisition order is deployment-state first, media-lifecycle second, SQLite transaction third;
- backup/standalone cleanup/restore may block or use one bounded configured timeout; timeout must fail before publishing backup, deleting media, or performing the first live-path rename;
- mutation and HTTP-read paths do not receive these capabilities.

These adapters contain no database, media, profile, Discord, gateway, or secret logic.

### `RestoreCoordinator`

A narrow backup-module component owns the durable restore state machine, exclusive deployment-state locking, optional duplicate-command mutex, marker serialization, generation-path validation, fsync/rename ordering, and startup recovery. `build_runtime()` first acquires the exclusive deployment-state lock and invokes recovery before opening any database or media path; it then releases EX and acquires a lifetime shared runtime lease before `Database.create_all()`. No live-state path is opened between those operations; if a restore wins the intervening lock race, startup waits or fails by policy and still does not open live state until SH is acquired. The runtime retains that shared descriptor through shutdown. It receives filesystem paths and integrity-verification helpers only; it does not receive Discord, gateway, HTTP-client, or signing-secret dependencies.

### Transaction and lock boundaries

- `UserProfileService` owns database transaction scope for profile mutations.
- Scalar fields plus complete metadata replacement: one caller-owned SQLAlchemy session and one DB transaction.
- Asset-row insertion plus switching one slot: one caller-owned session and one DB transaction after durable file publication.
- Clear one slot: one caller-owned session and one DB transaction.
- Transaction-scoped repository methods accept that session and perform no independent commit or rollback.
- Physical orphan cleanup: inside the bridge, use the already-held lifetime shared deployment-state lease; in a standalone CLI, acquire shared `/data/deployment-state.lock`; then acquire exclusive `/data/profile-media.lock`, start `BEGIN IMMEDIATE`, perform the final liveness check, unlink, row delete, and commit while all exclusions remain held.
- Backup: shared deployment-state lock, then shared media-lifecycle lock, from before snapshot creation through media copy and atomic archive publication.
- Backup publication: one atomic filesystem rename after all validation.
- Runtime startup/shutdown: acquire deployment-state EX and recover before opening live state; obtain and retain deployment-state SH before `Database.create_all()`/migration/media-store initialization; start workers only afterward. Shutdown stops Discord/HTTP/background workers and closes media readers, sessions, engine/connections, and other live-state owners before releasing SH. Kernel process exit releases the lease after crashes.
- Restore/recovery: exclusive deployment-state lock plus the durable phase marker and retained old/new generations; the lifetime runtime SH lease mechanically prevents restore from renaming live paths under an active bridge. Application startup performs recovery before opening live state. A separate restore-command mutex, if retained, prevents duplicate CLI invocation only and is never the backup/restore exclusion domain.

### Foundations required now

1. **Dedicated metadata rows.** physical surface mapping requires ordered, capped `PropertyValue` data; constraints now avoid a later incompatible JSON migration.
2. **Immutable media URLs.** physical surface mapping and 4 will expose and federate URLs cached by remote servers. Mutable bytes under one URL would make replacement/retry behavior ambiguous.
3. **DB-backed public route.** Changing from public filesystem paths later would break cached actor documents and expose non-live files.
4. **Versioned DB+media backup.** Once physical surface mapping accepts uploads, DB-only backup would lose user data.
5. **Nullable display name plus username fallback.** This preserves existing rows without changing immutable identity.
6. **Lifetime deployment-generation lease.** Restore safety depends on excluding every process with open DB or media handles, not only backup and cleanup. Establishing the runtime SH lease now avoids a later incompatible restore contract and prevents old/new SQLite split brain.

No follower row, delivery state, profile Update activity, publication context, personal-feed binding, outbox state, lease, retry queue, or future coordination state is added.

### Alternatives considered

- **Image BLOBs in SQLite:** this would materially simplify transaction, cleanup, backup, and restore behavior for the current two-slot profile-media scope. It is not adopted in this profile media storage plan because the approved umbrella plan explicitly makes selection of a bridge-owned media directory or object-storage boundary an entry condition for profile media storage and assigns a local media-storage implementation to this work. Replacing that approved boundary with BLOB persistence would therefore be an umbrella-level architecture change, not an implementation detail. The current repository also contains no approved capacity target or coordinator decision authorizing that change. The filesystem model remains selected under the existing authority; the BLOB alternative should be reconsidered only if the coordinator first revises the umbrella contract. This is not a rejection based merely on preference or file count.
- **Discord CDN URLs:** external, temporary, and outside bridge backup/retention control. Rejected.
- **Direct `StaticFiles`/nginx mount:** bypasses DB liveness and could expose staging/orphans. Rejected.
- **One JSON profile blob:** weakens count/order invariants and mixes unrelated ownership. Rejected.
- **Immediate unlink on replacement:** races readers/backups and cannot be atomic with SQLite. Rejected.
- **Object storage now:** unsupported by the current deployment. Keep a store interface, but implement only local durable storage in profile media storage.
- **Filesystem store owning database mutation:** rejected because it conflates byte persistence with relational ownership and cannot join the existing repository transactions safely. The accepted design has `UserProfileService` own one session and pass it to transaction-scoped repositories.
- **Durable `deleting` state for cleanup:** not selected for the current SQLite/local-filesystem deployment. Exclusive file locking plus `BEGIN IMMEDIATE` closes the concrete race without adding future workflow state; no recommendation is being rejected on preference alone.

## Persistence and Migration

1. Extend `users` with nullable `display_name`, `summary`, `avatar_media_id`, and `header_media_id`.
2. Create `profile_media_assets` and `user_profile_fields` for clean databases.
3. Add an idempotent SQLite migration that creates the new tables and rebuilds `users` when required so the two new foreign keys are real schema constraints rather than comments or repository conventions. The rebuild must preserve all existing columns, indexes, uniqueness constraints, data, and clean-schema parity; it then creates the slot-kind triggers and verifies no invalid positions, duplicate `(user_id, position)` rows, missing references, or slot/kind mismatches.
4. Enforce SQLite referential integrity on every connection through a SQLAlchemy connection hook executing `PRAGMA foreign_keys=ON`; startup and migration verification must fail if the pragma is not active. `users.avatar_media_id` and `users.header_media_id` are foreign keys to `profile_media_assets.id` with `ON DELETE RESTRICT`. Add narrow SQLite triggers for INSERT/UPDATE of each slot that reject a referenced asset whose `media_kind` does not match the slot. Repository validation remains for typed domain errors, but it is not a substitute for database enforcement. Migration and clean-schema tests must prove FK/trigger parity and convert expected `IntegrityError` cases into stable domain errors at service boundaries.
5. Preserve registration compatibility: `create_user()` callers do not need to supply profile values.
6. Do not alter identity export with ad hoc media paths; complete backup owns DB-plus-media portability.

## Configuration, Security, and Deployment

Add validated non-secret settings with these profile media storage defaults and accepted ranges:

```text
PROFILE_MEDIA_ROOT=/data/profile-media
DEPLOYMENT_STATE_LOCK_PATH=/data/deployment-state.lock
PROFILE_MEDIA_LOCK_PATH=/data/profile-media.lock
PROFILE_MEDIA_MAX_UPLOAD_BYTES=10485760       # 10 MiB; allowed 1048576..26214400
PROFILE_MEDIA_MAX_PIXELS=20000000             # 20 MP; allowed 1000000..40000000
PROFILE_MEDIA_ORPHAN_GRACE_SECONDS=86400      # 24 h; allowed 300..604800
```

Startup rejects values outside those closed ranges, a media root outside the durable `/data` tree, either lock path outside `/data`, identical deployment/media lock paths, or a lock path located inside the media object/staging directories. The 10 MiB/20 MP defaults are deliberately independent of physical surface mapping Discord acquisition limits: profile media storage protects storage and decoder resources for every future source stream. The maximum configurable 40 MP remains below Pillow's current default decompression-warning threshold, so the bridge-owned pixel check is always the primary product boundary.

Profile text contracts are also fixed here:

```text
display_name:       0..30 Unicode code points; empty -> NULL
summary:            0..500 Unicode code points; empty -> NULL
metadata rows:      0..4
metadata name:      1..255 Unicode code points
metadata value:     1..255 Unicode code points
```

Length is measured after Unicode NFC normalization and trimming leading/trailing Unicode whitespace. The stored value is that normalized value. An all-whitespace display name or summary becomes `NULL`; an all-whitespace metadata name or value rejects the entire mutation. The limits align with Mastodon's default bio and profile-field interoperability limits while preserving the immutable ActivityPub username.

Add Pillow with an explicit compatible lower bound. Decoder-derived format and successful full decode are authoritative; request MIME and extension are hints only. Keep Pillow's global `MAX_IMAGE_PIXELS` safety mechanism enabled and convert `DecompressionBombWarning` to a typed validation error within the decode boundary. Independently reject `width * height > PROFILE_MEDIA_MAX_PIXELS`; do not rely on Pillow's library default as the bridge limit.

Only bridge and backup containers retain `/data`; the gateway receives no volume access. Create `.staging` and `objects` with bridge-user ownership and strict permissions. Startup must fail before long-lived tasks if the path is unsafe or unwritable. Runtime construction must also fail before opening the database if it cannot acquire the recovery EX lock or the subsequent lifetime SH lease.

The long-lived `backup` Compose service must not inherit the bridge application `env_file`. It uses the same image but receives an explicit allowlist of only the non-secret values required by `src.db.backup`: database path, media root, deployment-state/media-lifecycle/restore-command lock paths, backup output path, interval, retention, and archive-format settings. Discord tokens, gateway credentials, OAuth secrets, actor signing configuration, webhook secrets, and unrelated application settings must be absent from its environment. A future archive-encryption credential, if separately approved, must be backup-specific and supplied only to backup/restore commands.

Backup, restore, and startup-recovery CLI entry points must parse their own narrow configuration and must not instantiate the full application `Settings`, `Runtime`, Discord client, gateway client, OAuth client, or signing-key owners. One-shot operator restore/recovery commands follow the same least-privilege contract. Compose and process-level tests inspect the effective backup-container environment and fail if any known application-secret variable is present.

Verified external contracts:

- FastAPI mounted static applications own all subpaths under the mount: https://fastapi.tiangolo.com/tutorial/static-files/
- SQLite documents that plain `BEGIN` acquires no lock and that `BEGIN IMMEDIATE` starts the write transaction immediately: https://sqlite.org/lockingv3.html and https://sqlite.org/lang_transaction.html
- Python documents Unix `fcntl.flock()` shared/exclusive advisory locks: https://docs.python.org/3/library/fcntl.html
- Pillow documents decompression-bomb warnings/errors based on decoded pixel count: https://pillow.readthedocs.io/en/stable/reference/Image.html
- Mastodon documents a default 500-character bio and default 255-character profile-field names/values: https://docs.joinmastodon.org/user/profile/ and https://docs.joinmastodon.org/methods/profile/
- OWASP recommends server-side authorization, allowlisted types, generated filenames, content validation, size limits, and storage outside directly public/executable paths: https://cheatsheetseries.owasp.org/cheatsheets/File_Upload_Cheat_Sheet.html
- Docker volumes persist outside container lifecycle and require explicit backup/restore; container export excludes mounted volume contents: https://docs.docker.com/engine/storage/volumes/ and https://docs.docker.com/reference/cli/docker/container/export/

## Touched Files

- `pyproject.toml`
- `src/models.py`
- `src/config.py`
- `src/db/database.py`
- `src/db/migrations.py`
- `src/db/repositories/__init__.py`
- `src/db/repositories/users.py`
- `src/db/backup.py`
- `src/runtime.py`
- `src/app.py`
- `src/http_api.py`
- `Dockerfile`
- `compose.yaml`
- `.env.example`
- `.env.dev.example`
- `docs/DEPLOY.md`
- `docs/architecture/database-map.md`
- `docs/architecture/database-method-inventory.md`
- `docs/architecture/http-routes.md`
- `docs/architecture/overview.md`
- `docs/development/test-coverage-map.md`
- `notes/known_issues.md` only for a verified limitation or finding
- affected registration, migration, HTTP, backup, config, and runtime test-support files

## New Files

- `src/db/repositories/user_profiles.py`
- `src/profile_media.py`
- `src/user_profiles.py`
- `tests/behavior/test_user_profile_persistence_scenarios.py`
- `tests/behavior/test_profile_media_scenarios.py`
- `tests/behavior/test_profile_media_backup_scenarios.py`

Names may change only to match current repository conventions; the responsibilities above must remain separate.

## Implementation Steps

1. Add failing behavior tests for migration defaults, effective display-name fallback, atomic scalar/field updates, four-row enforcement, rollback, and unchanged identity/key columns.
2. Add schema, migrations, repositories, transaction-scoped repository methods, and `Database` wiring. Verify clean and legacy databases and idempotent reruns. Add an explicitly supported `BEGIN IMMEDIATE` helper for service-owned cleanup sessions rather than emitting ad hoc transaction SQL from repositories.
3. Add failing ingestion tests for valid JPEG/PNG/WebP and invalid MIME spoofing, malformed/truncated input, unsupported format, animation, byte overflow, pixel overflow, decompression bombs, and storage failures.
4. Implement local media storage and profile service with staging, canonical re-encoding, fsync/rename, transactional reference switching, clearing, and orphan cleanup.
5. Add concurrent replacement/clear and cleanup scenarios against real SQLite. Pause cleanup after its final liveness query while it holds `BEGIN IMMEDIATE`, attempt a reference switch from a second session, and prove that only one coherent outcome commits: either cleanup observes the reference and preserves bytes, or cleanup deletes first and the forbidden orphan attachment cannot commit. Assert no cross-slot loss and that every committed reference has bytes.
6. Add failing HTTP-route tests for known assets, malformed/unknown IDs, missing files, traversal-like IDs, response headers, and inability to fetch staging/orphan/unpersisted objects.
7. Wire read-only media delivery into `Runtime`, `build_runtime()`, and FastAPI without changing internal Fedify actor responses or gateway serialization.
8. Add failing complete-backup scenarios: exact snapshot-derived inclusion; a snapshot referencing asset A followed by replacement with B and attempted cleanup before A is copied; deployment-state shared/exclusive exclusion between backup and restore/recovery; media-lifecycle shared/exclusive exclusion between backup and cleanup; fixed lock ordering and timeout behavior; crash-release behavior; missing file/hash mismatch failure; orphan exclusion; malicious archive paths; retention; and legacy SQLite behavior. Add restore crash-injection scenarios at every durable boundary: after prepared-marker publication, each old-component move, each marker phase update, new DB install, new media install, post-install verification, commit marking, and old-generation cleanup. Each scenario must restart recovery and prove convergence to one verified complete old or new generation with no mixed pair.
9. Implement the versioned archive and safe restore while preserving the existing SQLite online-backup primitive internally.
10. Update Compose, Docker image/runtime directory ownership, env templates, and deploy instructions for the single `/data` volume and complete backups. Remove the shared application `env_file` from the `backup` service, pass only the explicit backup allowlist, and keep restore/recovery CLIs independent of full application settings and secret-bearing runtime construction.
11. Update only documentation responsible for schema, repository methods, HTTP routes, runtime ownership, deployment, and test coverage. Do not document physical surface mapping commands or actor fields as available.
12. Compare the implementation point-by-point with this plan and verify that no follow-on behavior entered the diff.

## Tests

### Behavior and integration

- Legacy users migrate with effective display name equal to username, no explicit media, unchanged actor URLs/key material, and idempotent migration.
- New registration still succeeds without explicit profile rows.
- Updating display name, summary, and zero to four fields is atomic; a fifth field, duplicate position, blank normalized row, or oversized value rejects the whole mutation.
- Valid images are decoded and canonically re-encoded; EXIF/text metadata and original filenames do not survive.
- Invalid, truncated, spoofed, animated, oversized, or decompression-bomb input produces a typed validation error, no changed reference, and no public asset.
- Simulated write/fsync/rename/DB failures preserve the prior profile; post-rename DB failure leaves only an unreferenced object.
- Replacing avatar does not change header/text; clearing header does not change avatar; concurrent writes end in one coherent state.
- Cleanup preserves live assets and young orphans. A deterministic two-session race pauses cleanup after the final liveness query and proves `BEGIN IMMEDIATE` prevents a concurrent slot switch from committing until cleanup resolves; a lock-order test proves cleanup takes deployment-state shared, media-lifecycle exclusive, then SQLite, with timeout leaving state untouched; no committed reference can point to deleted bytes. Unlink failure rolls back and remains retryable.
- The public route serves only persisted assets with correct headers and rejects unknown, malformed, escaped, staging, orphan, and missing-file cases.
- Complete backup includes exactly media referenced by its SQLite snapshot and cannot publish with missing/mismatched bytes. Replacement and cleanup after snapshot creation cannot remove snapshot-referenced bytes before copy because backup holds shared deployment-state and media-lifecycle locks while cleanup requires shared deployment-state plus exclusive media-lifecycle access. Concurrent restore/recovery cannot switch generations because it requires exclusive deployment-state access.
- Restore validates before replacement and restores DB/media as one restart-recoverable unit. A concurrent backup blocks or fails by configured timeout before snapshot publication, and automatic backup-service restart cannot bypass the exclusive deployment-state lock. Crash injection after every rename/marker boundary followed by recovery converges deterministically to the complete old or complete new generation; corrupt DB, hash mismatch, missing entry, hostile archive, or unrecoverable marker state never permits startup on a mixed pair.
- Runtime generation exclusion is exercised end to end: start a runtime that has opened the database and media-read capability while holding lifetime deployment-state SH; a concurrent restore must block or time out before creating the first live-path rename/marker transition. After workers stop, readers/sessions/engine close, and runtime releases SH, the same restore obtains EX and completes. Forced process termination releases SH through the kernel, allowing the next startup to acquire EX, recover an incomplete marker, then acquire its lifetime SH lease.
- Config/startup verifies the exact numeric text/media/grace bounds, a writable media root under `/data`, distinct usable deployment-state and media-lifecycle lock paths, and `PRAGMA foreign_keys=ON` on every production connection path; unsafe configuration fails before bot/HTTP task startup.
- Compose/process tests render the effective `backup` service configuration and assert that it has only its explicit path/interval/retention/archive variables, no shared application `env_file`, and no Discord, gateway, OAuth, signing, webhook, or other application secret. Backup/restore/recovery tests also prove those CLIs do not construct full `Settings` or runtime clients.

### Regression

Run existing registration, actor resolution, gateway contract, dashboard/static route, migration, backup, configuration, and full behavior suites. Add explicit assertions that profile media storage does not alter:

- gateway `Person` JSON;
- WebFinger, actor IDs, usernames, or keys;
- local-community actor serialization;
- current user followers/outbox placeholder behavior;
- Discord command tree;
- publication recipients or canonical IDs.

### Property and mutation assurance

Generate boundary cases for metadata count/positions, hostile public IDs, storage-key containment, byte/pixel limits, and archive path normalization. Representative mutations must fail if the four-field check, full decode, DB lookup in the route, backup media inclusion, or cleanup liveness recheck is removed.

## Validation Steps

1. Run new profile media storage tests under TDD, then affected registration/migration/HTTP/backup/config tests.
2. Run the full Python suite, repository assurance/generation checks, static/format checks, and compile checks.
3. Run the unchanged gateway TypeScript verification suite to detect actor-contract drift.
4. Build the Python image and start Compose with a temporary volume; verify ownership, health, public media retrieval, complete backup, clean restore, startup recovery from each injected incomplete restore phase, restore exclusion while the bridge holds its lifetime shared generation lease, and successful restore after orderly shutdown releases it.
5. Inspect schema and migration rerun on clean and prerelease databases; verify `ON DELETE RESTRICT`, slot-kind triggers, connection-wide `PRAGMA foreign_keys=ON`, and clean-schema/migration parity.
6. Inspect failure logs for secret, profile text, original filename, Discord URL, signing-key, and private-path leakage.
7. Review the final diff against `AGENTS.md`, orchestrator, umbrella profile media storage, and this plan.

## Regression and Blind-Spot Analysis

- Display name must never flow into `activitypub_username`, actor URL builders, key IDs, or authored-object ownership.
- Adding Python fields must not make the Fedify gateway render them before physical surface mapping.
- Profile media belongs only to registered `User` actors; do not reuse local-community rows or keys.
- No route/proxy may expose `/data`, `.staging`, manifests, SQLite, or orphan objects.
- Storage initialization, reads, and cleanup must reject symlink escape.
- Replacing bytes under an existing storage key is forbidden; every replacement receives a new public ID.
- Restoring DB alone after media exists creates broken references; CLI/docs must distinguish complete and legacy backups. Multi-path restore cannot rely on exception rollback: the durable marker and retained generations must recover process termination at every rename boundary before application startup.
- Cleanup age checks and a final SELECT are insufficient without writer exclusion; the final check, unlink, and row deletion must occur under the exclusive advisory lock plus `BEGIN IMMEDIATE`.
- Snapshot-derived selection is insufficient without pinning the copy window; backup holds shared deployment-state and media-lifecycle locks until publication/failure cleanup. The running bridge also holds deployment-state SH for every open live-state handle. Restore/recovery uses EX, so no archive can be published and no DB/media path can be switched while an application process is still using the old generation.
- Pillow defaults are not product limits; the explicit bridge byte and pixel bounds remain authoritative and are tested at every boundary.
- Test `SimpleNamespace` runtime/settings builders must be updated deliberately so production wiring is not bypassed.
- Foreign keys and slot-kind triggers are mandatory last-line invariants; repository checks alone must never be treated as equivalent.
- Conservative orphan retention grows disk usage; profile media storage should log cleanup counts without user data, while outbox maintenance owns final scheduling/diagnostics.

## Implementation handoff

### Changed contracts

- Registered users have durable profile scalar fields, ordered metadata rows, and optional immutable avatar/header references.
- Python owns a local media store and narrow public media-read route.
- Complete deployment backups include SQLite and referenced media protected by shared deployment-state and media-lifecycle locks. The bridge holds a lifetime shared deployment-state lease around all live DB/media use. Restore/recovery uses exclusive deployment-state exclusion plus a durable phase marker and retained generations so interrupted replacement recovers to one complete DB-plus-media state before startup and cannot switch paths under a running bridge.
- Deployment config defines one durable profile-media root, one deployment-state lock, and one media-lifecycle lock under the existing data volume.
- SQLite enforces media referential integrity with connection-wide foreign keys, `ON DELETE RESTRICT`, and slot-kind triggers.

### Preserved contracts

- Actor JSON, WebFinger, identity URLs, usernames, keys, inbox/outbox/followers behavior, content delivery, Discord commands, and all existing bridge modes remain unchanged.
- Gateway receives no filesystem/media-store access.
- Registration callers continue to create users without profile inputs.

### Follow-on work deliberately untouched

- physical surface mapping: `/account-settings`, Discord upload acquisition, ActivityPub text sanitization/rendering, and actor `name`, `summary`, `icon`, `image`, and `attachment` fields.
- local subscriber synchronization+: Follow lifecycle.
- User Actor profile publishing+: `Update(Person)` and follower delivery/retry.
- personal feed control plane+: personal-feed control/content.
- outbox maintenance: final scheduled orphan cleanup and operational diagnostics.

### Assumptions for physical surface mapping

physical surface mapping may rely on atomic profile mutation, at most four ordered fields, immutable stable media URLs, validated durable bytes, explicit replace/clear operations, and complete DB-plus-media backup. It must not store Discord attachment URLs or bypass these boundaries.

No temporary adapter, duplicate profile API, placeholder path, or broken caller may remain.

## Open Questions

None. The repository, umbrella boundary, and verified external contracts are sufficient for profile media storage planning.
