# SQLite startup migrations

This document defines supported database versions, startup recognition and atomic migration behavior. Application release tags and platform stage numbers do not determine SQLite `user_version`.

## Supported source and target

The current target is **database v1**, the complete 45-table Stage 01 model. The supported legacy source is **unversioned v0** with the released `v0.1.0` (`8ab4403e`) 30-table schema. Startup recognizes its columns, defaults, CHECK/UNIQUE/FK constraints, indexes, triggers and ownership/parent relationships before writes. The release identity cutover is planned from source rows using non-secret public-origin/service-identifier settings; ambiguous namespaces fail before DDL. Unknown and partial sources fail closed.

Back up the database and media before upgrading. Run the current application once: startup supplies `IdentityCutoverConfig` from settings to `Database.migrate(identity=...)`, which upgrades a recognized release v0 directly to current v1. No intermediate application install or manual header stamp is needed. A genuine empty file bootstraps current v1 directly.

Unpublished Stage 00 databases stamped 1 and development databases stamped 2 are unsupported. Changing their headers does not create a supported schema. A current-model database created by schema-only `Database.create_all()` remains unversioned and is not automatically adopted as a legacy release.

## Transaction boundary

Read-only preflight uses a stable SQLite read snapshot. Writing paths reread the header and table inventory under `BEGIN IMMEDIATE`. Fresh initialization keeps foreign-key enforcement ON. The released-source path suspends enforcement before the transaction, normalizes historical publication/profile shapes, converts legacy ActivityPub identities, and applies connector/channel backfill on the same connection. The internal 43-table input is neither committed nor stamped. Each source inspection loads its own historical snapshot: advisory preflight under BEGIN, then authoritative validation under BEGIN IMMEDIATE. Inside the writer transaction, relationship checks, identity planning and normalization share that one unchanged snapshot; normalization copies individual rows. Reads after schema/data changes remain independent validation evidence. There is no snapshot cache across inspections.

Before commit, the runner validates the full target schema, every preserved row/column against the explicit expected identity transformation, removed host projections against their physical claims, `foreign_key_check` and `integrity_check`. It assigns version 1 once. Rebuilds create/copy a replacement before dropping the original, preserving incoming cascade families and indexes/triggers. Errors roll back both DDL/data and the header. The runner verifies physical SQLite rollback even when SQLAlchemy's logical commit failed.

After commit or rollback, suspended enforcement is restored and verified. Failed restoration invalidates the connection. Enforcement-negative SQL tests run after restoration; FK OFF cannot reject invalid writes inside the transaction.

Already-current v1 startup validates through read-only queries, including the seed connector, the complete generated-role column definition and STORED mode, defaults, composite FKs, UNIQUE/CHECK constraints and explicit index/trigger definitions. It performs no repairs or writer reservation. Concurrent initializers validate the winner instead of applying the migration twice.

## Historical evidence and tests

`tests/fixtures/release_v0_1_0_original.sqlite` was created with tagged `Database.create_all()` followed by tagged `migrate()`. Its SQL capture and immutable tagged models provide independent fixture routes. Production uses named semantic evidence and only the 16 definitions needed for normalization in `src/db/legacy_stage00_ddl.py`; it never imports test models.

The obsolete additive/rebuild migration entry point and its seven private helpers have been removed; the atomic registered runner is the only supported migration path.

The historical Stage 00 SQL/model fixture exists solely for isolated private platform/cascade proofs. `tests/test_legacy_v0_to_v1_migration.py` covers the actual supported startup contract, preservation, faults, concurrency, backup/reopen and migrated runtime actions.

## One-time ActivityPub identity transition

Released `/actors/{user}` and `/communities/{slug}` identities become their typed user/community paths. Service actor/key metadata and linked authored content, replies, routing IDs and relay JSON references change together; signing material, Markdown/HTML content, audit payloads, physical Discord IDs, local subscribers and claims remain intact. Source inbox/outbox/followers and service key URLs must belong to the same registered actor in a known historical or typed form; conflicting metadata is rejected before DDL. Canonical bridge-owned object/activity IDs require a registered owner and complete supported object/activity grammar, including already-typed inputs and exact aliases. For publications and outgoing Discord mappings, both local canonical IDs must belong to the row's actor; relay activities must belong to their local_community_id. Inbound mapping actors may be relayers, and a thread/group community is routing context rather than content authorship, so those fields are not imposed as the object owner. Unknown local formats and contradictory explicit owners fail closed. Remote-origin identities are preserved. A signed JSON envelope requiring modification is rejected rather than invalidating its signature.

Incoming remote_subscribers are deleted only for communities whose actor id changes. Shared BridgeActorFollow and channel subscription rows are retained with `failed` and cleared Follow IDs only if their service sender or local target actor changes. Existing IDs, creation times and claims survive. Unchanged actors retain their relationship state. Repeat startup does not apply this reset.

Affected outgoing subscriptions require a normal moderator subscribe retry; `pending` would block that retry without a new Follow. Affected incoming followers must follow the new community id. The transaction sends no network activities. Configuration-backed current-v1 startup rejects incompatible actor metadata without repairs.
