# SQLite startup migrations

This document defines supported database versions, startup recognition and atomic migration behavior. Application release tags and platform milestone labels do not determine SQLite `user_version`.

## Supported source and target

The current target is **database v1**, the evolving 47-table current SQLite schema model. The supported legacy source is **unversioned v0** with the released `v0.1.0` (`8ab4403e`) 30-table schema. Startup recognizes its columns, defaults, CHECK/UNIQUE/FK constraints, indexes, triggers and ownership/parent relationships before writes. The source fact is `src/db/legacy_v0.sql`, installed as package data and introspected in a disposable in-memory reference database. The release identity cutover is planned from narrow source metadata using non-secret public-origin/service-identifier settings; ambiguous namespaces fail before DDL. Unknown and partial sources fail closed.

Back up the database and media before upgrading. Run the current application once: startup supplies `IdentityCutoverConfig` from settings to `Database.migrate(identity=...)`, which upgrades a recognized release v0 directly to current v1. No intermediate application install or manual header stamp is needed. A genuine empty file bootstraps current v1 directly.

Unpublished unsupported prerelease schema databases stamped 1 and development databases stamped 2 are unsupported. Changing their headers does not create a supported schema. A current-model database created by schema-only `Database.create_all()` remains unversioned and is not automatically adopted as a legacy release.

## Transaction boundary

Read-only preflight uses a stable SQLite read snapshot. Writing paths reread the header and table inventory under `BEGIN IMMEDIATE`. Fresh initialization keeps foreign-key enforcement ON. The released-source path suspends enforcement before the transaction and calls `legacy_v0_to_v1.upgrade_released_v0_to_v1(conn, identity)` directly. It revalidates source schema/data, plans protocol deltas, copies all 30 source tables to transactional SQL TEMP staging, then removes the old main tables. Current `Base.metadata.create_all(conn)` creates all 47 target tables once. Native SQL builds the channel inventory, copies preserved values with INSERT/SELECT and JOIN, and installs typed claims before applying identity deltas. No unsupported prerelease schema business schema or persistent intermediate header exists.

Advisory and writer inspections read separate evidence under BEGIN and BEGIN IMMEDIATE. Python evidence contains only fields needed for relationship/identity validation; key material, publication bodies, timestamps and untouched audit/policy payloads stay in SQL staging. Preservation compares complete staged source rows against the target with bidirectional EXCEPT after applying only approved protocol deltas to the implementationd oracle. TEMP copies are removed before commit. There is no cross-transaction cache. Reads of the changed target remain separate validation evidence.

Before commit, the runner validates the full target schema, every preserved row/column against the explicit expected identity transformation, removed host projections against their physical claims, `foreign_key_check` and `integrity_check`. It assigns version 1 once. Every source table is staged before any source DROP; the target is then created once and populated on the same connection, preserving source rows and dependent mappings. Errors roll back both DDL/data and the header. The runner verifies physical SQLite rollback even when SQLAlchemy's logical commit failed.

After commit or rollback, suspended enforcement is restored and verified. Failed restoration invalidates the connection. Enforcement-negative SQL tests run after restoration; FK OFF cannot reject invalid writes inside the transaction.

Already-current v1 startup validates through read-only queries, including the seed connector, the complete generated-role column definition and STORED mode, defaults, composite FKs, UNIQUE/CHECK constraints and explicit index/trigger definitions. It performs no repairs or writer reservation. Concurrent initializers validate the winner instead of applying the migration twice.

## Stage 04 additions to the evolving v1 target

Checkpoint 56 adds `platform_delivery_intents` and
`local_community_authored_announce_deliveries` directly to current ORM v1.
It adds canonical `content_revision`, nullable latest-content/last-update evidence
and `deleted_at` to four existing Local/Remote Community canonical tables, and
nullable `applied_revision` plus per-copy mutation claim/state to their four
existing delivery/surface tables. Source object dedup stores the immediate
physical container separately from a parent forum claim. User Actor outbound
endpoint deliveries gain optional bounded direct-recipient evidence alongside
independent accepted-follower rows.

Released-v0 physical copies are not newly sent by the migration: `applied_revision`
and authored delivery intents remain unknown/empty. No source-body snapshot,
original accepted Follow snapshot, CreateAttempt receipt or canonical deletion
can be inferred from a missing historical record. The same complete target ORM
is used for fresh bootstrap, released-v0 upgrade and read-only current-v1
validation. No v1→v2 migration or development-v1 upgrade is added.

## Historical evidence and tests

`tests/fixtures/release_v0_1_0_original.sqlite` was created with tagged `Database.create_all()` followed by tagged `migrate()`. Its SQL capture and immutable tagged models provide independent fixture routes. Production uses the one pinned SQL source and current ORM target; it never imports test models or stores a parallel historical Python manifest. `INSERT ... SELECT *` is not used for transfer; source staging uses CREATE TEMP TABLE AS SELECT before any DROP.

Both the obsolete helper entry point and the internal unsupported prerelease schema normalization/platform pipeline have been removed. The atomic registered direct runner is the only production migration path.

The rejection-only `tests/fixtures/unsupported_prerelease_schema.sql` captures the unsupported development shape. No test upgrades it. Released-source preservation/fault scenarios execute production `Database.migrate()`; personal-feed/outbox/mutation features absent from the release are tested on current v1 for restart preservation and actual FK cascades. The historical migration probe and its model fixture have been removed. `tests/test_legacy_v0_to_v1_migration.py` covers the actual supported startup contract, preservation, faults, concurrency, backup/reopen and migrated runtime actions.

## One-time ActivityPub identity transition

Released `/actors/{user}` and `/communities/{slug}` identities become their typed user/community paths. Service actor/key metadata and linked authored content, replies, routing IDs and relay JSON references change together; signing material, Markdown/HTML content, audit payloads, physical Discord IDs, local subscribers and claims remain intact. Source inbox/outbox/followers and service key URLs must belong to the same registered actor in a known historical or typed form; conflicting metadata is rejected before DDL. Canonical bridge-owned object/activity IDs require a registered owner and complete supported object/activity grammar, including already-typed inputs and exact aliases. For publications and outgoing Discord mappings, both local canonical IDs must belong to the row's actor; relay activities must belong to their local_community_id. Inbound mapping actors may be relayers, and a thread/group community is routing context rather than content authorship, so those fields are not imposed as the object owner. Unknown local formats and contradictory explicit owners fail closed. Remote-origin identities are preserved. A signed JSON envelope requiring modification is rejected rather than invalidating its signature.

Incoming remote_subscribers are deleted only for communities whose actor id changes. Shared BridgeActorFollow and channel subscription rows are retained with `failed` and cleared Follow IDs only if their service sender or local target actor changes. Existing IDs, creation times and claims survive. Unchanged actors retain their relationship state. Repeat startup does not apply this reset.

Affected outgoing subscriptions require a normal moderator subscribe retry; `pending` would block that retry without a new Follow. Affected incoming followers must follow the new community id. The transaction sends no network activities. Configuration-backed current-v1 startup rejects incompatible actor metadata without repairs.

The direct SQL scenarios, physical-window rollback and malformed affinity checks are in `tests/test_direct_sql_v0_to_v1_migration.py`. The 147 A/B comparison uses the committed 146 baseline on byte-identical populated tag/model files, with legacy and already-typed identities; only timestamps of newly created connector/surface rows are excluded.

Current-v1 admission constructs an independent in-memory schema from the ORM and retained deployment indexes/triggers, then compares complete normalized table DDL and native SQLite column/FK/index/trigger evidence. Complete generated expressions, STORED mode, defaults, collation and index predicates participate; there is no separate Python model manifest or column/CHECK parser. Unknown DDL differences remain unsupported. Fixed released relationships and channel sources are explicit SQL in `src/db/legacy_v0_sql.py`; the live runner executes individual statements without executescript.

## Migration diagnostics

The `src.db.migrations` and `src.db.legacy_v0_to_v1` loggers emit INFO phase messages for preflight, writer startup, source validation, data transfer, ActivityPub identity cutover, integrity checks and successful commit. Rollback is logged at WARNING only after the rollback operation succeeds. A current-v1 startup validates its schema without reporting another transfer/cutover or commit. Logs contain phase/version names, never record bodies, JSON, signing keys, SQL parameters or exception tracebacks.

SQL failures are contextual RuntimeErrors with the original SQLAlchemy exception retained as their cause. Table/check boundaries identify staging, DROP, copy, claim, identity and schema/integrity failures; transaction failures identify stamp/commit. Bound parameters are hidden when rendering StatementError tracebacks. Inspect the cause for the underlying driver error rather than parsing log text. Semantic validation errors retain their existing type and identify only selected relationship/channel/thread/row/parent IDs, without dumping associated records. A commit message reports the completed transaction even if subsequent FK restoration or read validation fails.

Before introducing database v2, freeze the v1 target used by historical v0→v1 independently of current ORM metadata; this is tracked in `dev/future_tasks.md` and is not implemented by the diagnostics change.

## Incomplete legacy message sources

After migration, an old message mapping may have an unknown immediate container.
SQLite UNIQUE does not deduplicate NULL containers. The repository reserves the
writer and rejects another AP object for the same potentially identical source,
including incoming typed evidence that cannot disambiguate the older row.
Exact immutable receipt retries remain idempotent. Fully scoped sources from
different containers or connectors retain independent identities.


## First mutations of legacy Community objects

Migration preserves unknown content and ownership as unknown. It does not
reconstruct `current_content_json`, invent an applied revision or infer comment
authorship from physical copy roles.

The first authenticated Update/Delete may use independent evidence already in
SQLite: a matching `published_activity_objects` entry (exact object, original
Create activity, object kind and community), or a Local Community original
Create in `local_community_relay_source_activities` whose envelope, object,
actor and community agree. All available evidence must identify the same author.
These reads occur inside the canonical writer transaction. The incoming event's
author alone, a URL prefix, a mapping's relay actor or a forum role is insufficient.
Unknown/conflicting evidence returns an explicit provenance/operator-review
refusal without advancing the canonical revision. No historical body is invented.
Update still requires a timezone-aware source version and stable activity ID;
once admitted, the usual revision/replay checks apply. Delete remains monotonic.

Before each physical edit/delete, recovery fetches the exact `MessageRef` and
requires a positively identified bridge-owned author. Missing source coordinates
never grant permission to edit a human message. Unverified ownership is recorded
as `failed_definite` with an `unverified physical ownership:` reason and no applied
revision; that revision remains blocked until operator inspection or a newer
canonical revision. Read failures remain provably unsent; a lost write ACK stays
`unresolved` across restart. A successful mirror ACK advances only that copy.

`tests/behavior/test_stage04_tagged_legacy_mutations.py` executes the authentic
`v0.1.0` Database and ORM in a subprocess, migrates the populated database and
enters real Local/Remote Update/Delete handlers. It covers independent author
proof, missing/conflicting evidence, host/subscriber human sources, nested
comments, lost ACKs and restart. It requires the release tag in the local checkout.
