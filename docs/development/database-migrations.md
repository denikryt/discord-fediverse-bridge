# SQLite startup migrations

This document defines supported database versions, startup recognition and atomic migration behavior. Application release tags and platform stage numbers do not determine SQLite `user_version`.

## Supported source and target

The current target is **database v1**, the complete 45-table Stage 01 model. The supported legacy source is **unversioned v0** with the released `v0.1.0` (`8ab4403e`) 30-table schema. Startup recognizes its columns, defaults, CHECK/UNIQUE/FK constraints, indexes, triggers and ownership/parent relationships before writes. The source fact is `src/db/legacy_v0.sql`, installed as package data and introspected in a disposable in-memory reference database. The release identity cutover is planned from narrow source metadata using non-secret public-origin/service-identifier settings; ambiguous namespaces fail before DDL. Unknown and partial sources fail closed.

Back up the database and media before upgrading. Run the current application once: startup supplies `IdentityCutoverConfig` from settings to `Database.migrate(identity=...)`, which upgrades a recognized release v0 directly to current v1. No intermediate application install or manual header stamp is needed. A genuine empty file bootstraps current v1 directly.

Unpublished Stage 00 databases stamped 1 and development databases stamped 2 are unsupported. Changing their headers does not create a supported schema. A current-model database created by schema-only `Database.create_all()` remains unversioned and is not automatically adopted as a legacy release.

## Transaction boundary

Read-only preflight uses a stable SQLite read snapshot. Writing paths reread the header and table inventory under `BEGIN IMMEDIATE`. Fresh initialization keeps foreign-key enforcement ON. The released-source path suspends enforcement before the transaction and calls `legacy_v0_to_v1.upgrade_released_v0_to_v1(conn, identity)` directly. It revalidates source schema/data, plans protocol deltas, copies all 30 source tables to transactional SQL TEMP staging, then removes the old main tables. Current `Base.metadata.create_all(conn)` creates all 45 target tables once. Native SQL builds the channel inventory, copies preserved values with INSERT/SELECT and JOIN, and installs typed claims before applying identity deltas. No Stage 00 business schema or persistent intermediate header exists.

Advisory and writer inspections read separate evidence under BEGIN and BEGIN IMMEDIATE. Python evidence contains only fields needed for relationship/identity validation; key material, publication bodies, timestamps and untouched audit/policy payloads stay in SQL staging. Preservation compares complete staged source rows against the target with bidirectional EXCEPT after applying only approved protocol deltas to the staged oracle. TEMP copies are removed before commit. There is no cross-transaction cache. Reads of the changed target remain separate validation evidence.

Before commit, the runner validates the full target schema, every preserved row/column against the explicit expected identity transformation, removed host projections against their physical claims, `foreign_key_check` and `integrity_check`. It assigns version 1 once. Every source table is staged before any source DROP; the target is then created once and populated on the same connection, preserving source rows and dependent mappings. Errors roll back both DDL/data and the header. The runner verifies physical SQLite rollback even when SQLAlchemy's logical commit failed.

After commit or rollback, suspended enforcement is restored and verified. Failed restoration invalidates the connection. Enforcement-negative SQL tests run after restoration; FK OFF cannot reject invalid writes inside the transaction.

Already-current v1 startup validates through read-only queries, including the seed connector, the complete generated-role column definition and STORED mode, defaults, composite FKs, UNIQUE/CHECK constraints and explicit index/trigger definitions. It performs no repairs or writer reservation. Concurrent initializers validate the winner instead of applying the migration twice.

## Historical evidence and tests

`tests/fixtures/release_v0_1_0_original.sqlite` was created with tagged `Database.create_all()` followed by tagged `migrate()`. Its SQL capture and immutable tagged models provide independent fixture routes. Production uses the one pinned SQL source and current ORM target; it never imports test models or stores a parallel historical Python manifest. `INSERT ... SELECT *` is not used for transfer; source staging uses CREATE TEMP TABLE AS SELECT before any DROP.

Both the obsolete helper entry point and the internal Stage 00 normalization/platform pipeline have been removed. The atomic registered direct runner is the only production migration path.

The rejection-only `tests/fixtures/unsupported_stage00_schema.sql` captures the unsupported development shape. No test upgrades it. Released-source preservation/fault scenarios execute production `Database.migrate()`; personal-feed/outbox/mutation features absent from the release are tested on current v1 for restart preservation and actual FK cascades. The historical migration probe and its model fixture have been removed. `tests/test_legacy_v0_to_v1_migration.py` covers the actual supported startup contract, preservation, faults, concurrency, backup/reopen and migrated runtime actions.

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
