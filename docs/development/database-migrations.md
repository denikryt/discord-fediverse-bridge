# SQLite v0→v1 offline release upgrade

This document owns the deployment/operator contract for the bridge's first
versioned SQLite schema. It is **not** a generic rolling-upgrade or disaster
recovery framework. The application version (`v0.1.0` or later) is unrelated
to `PRAGMA user_version` (`0` for supported historical files, `1` for this
release). The `src/db/schema_versions/v1.sql` snapshot is provisional during
development and must be frozen against the ORM and migrated files at CP24.

## Source admission

- **Empty v0**: SQLite has no user tables/indexes/triggers. Create complete v1
  directly inside a single writer transaction, without historical backup.
- **Tagged release v0.1.0**: exact 30-table DDL from the tagged release,
  fixed in `src/db/legacy_v0.sql`; there is no profile-media table, so media
  inventory is empty. Its retained data are copied by explicit column mapping.
- **Known main v0**: exact unversioned main physical DDL, plus row-level
  confirmation of consistently old or consistently typed actor endpoints,
  related keys, and stored object/JSON identities. Selectively convert old
  local identities; never repeat cutover on already typed actors. Old rows
  with unknowable owner/source do not gain fabricated provenance.
- **Ready v1**: full target DDL/index/trigger fingerprint, FK integrity and
  SQLite integrity verified read-only. No `CREATE TABLE`, `UPDATE`, or schema
  stamping happens on repeat startup.
- **Unknown, partially upgraded, corrupt, mixed owner, future version**:
  stop. Do not infer support from the number of tables or one URL prefix.
  Preserve the original file and diagnose from a verified backup.

`src/db/migrations.py` exclusively owns `PRAGMA user_version` and `BEGIN
IMMEDIATE`; `src/db/migration_steps/v0_to_v1.py` owns the irreversible
historical data transfer. Foreign keys are suspended **before** BEGIN only
for the verified table rebuild, checked before commit, then restored after.
The target version is stamped only after full schema and FK validation. The
runner never executes `executescript` on a production migration connection.
An error rolls back schema, rows, and the version stamp.

## Required offline operator gate

**Before starting the v1 application**, stop *every* process capable of
running pre-v1 database code, including old `v0.1.0` or older `main` Python
workers, and disable their automatic restarts. Keep them stopped through the
entire EX→SH window. This is an external operational precondition: POSIX
`flock` does not provide an atomic exclusive-to-shared conversion or prevent
an old binary without v1 admission from starting later. Mixed-version
rolling upgrade is unsupported.

For a systemd deployment using the repository's default unit names:

```sh
./systemd-services.sh stop
./systemd-services.sh disable
sudo systemctl mask --runtime discord-fediverse-bridge.service
sudo systemctl mask --runtime discord-fediverse-bridge-fedify-gateway.service
systemctl is-active discord-fediverse-bridge.service
systemctl is-active discord-fediverse-bridge-fedify-gateway.service
```

Both `is-active` results must show `inactive`, not `active`. Confirm there are
no additional old bridge processes or separately managed supervisors. The
operator may use a different unit name; check `PYTHON_BRIDGE_SERVICE` and
`FEDIFY_GATEWAY_SERVICE` overrides before executing. For Compose deployments,
stop and disable the old stack/autostart according to the host's container
supervisor configuration (`docker compose down` by itself does **not** prove
that no other old container or external supervisor can restart it).

Preserve the original DB and profile-media tree. Ensure the deployment user
can write the DB parent and the protected backup directory. Confirm the
intended version and DB path from configuration before unmasking v1:

```sh
sqlite3 /data/bridge.db 'PRAGMA user_version; PRAGMA integrity_check;'
```

The new bridge startup then performs, under the **existing EX deployment
lock**:

1. Complete any interrupted restore that is already owned by the existing
   recovery protocol.
2. Read-only version/schema/profile admission; for a nonempty supported v0,
   create and verify a source-aware, SQLite+referenced-media complete archive.
3. Publish it without clobber under `<DB parent>/backups/migrations/`.
   Protected migration archives are not processed by normal backup retention.
4. Run the transactional v0→v1 migration and validate final schema/relations.
5. Close its migration DB handle and release EX. Acquire a **new ordinary SH**
   lifetime lock and perform a second read-only v1 admission before
   constructing Discord, Fedify and HTTP runtime objects.

If backup, migration, SH acquisition or either v1 admission fails, startup
stops; do not start an old binary against the partially investigated file.
For a verified v1 file, no migration archive is generated and both admissions
perform only reads. Two independent v1 startups can share the normal lock.
After successful operator verification, unmask **only the new installed
version**, restore the desired supervisor policy, and start the new units:

```sh
sudo systemctl unmask discord-fediverse-bridge.service
sudo systemctl unmask discord-fediverse-bridge-fedify-gateway.service
./systemd-services.sh start
```

## Restore and failure recovery

Stop v1 services and any background writer before restore. Use the exact
migration archive path retained under `/data/backups/migrations/` (when the
SQLite file is `/data/bridge.db`), and the existing complete restore command:

```sh
python -m src.db.backup restore \
  --database /data/bridge.db \
  --media-root /data/profile-media \
  --deployment-lock /data/deployment-state.lock \
  --source /data/backups/migrations/<verified-backup>.tar.gz
```

The restore validates the archive and media. The restored DB is historical
v0; **never** start pre-v1 and v1 concurrently. A subsequent v1 startup
rechecks source evidence and produces a new no-clobber migration backup.
If the source cannot be proven safe, stop and perform manual analysis using
read-only SQLite copies; do not merge duplicate AP objects/owners by guessing.

Historical accepted Lemmy subscriptions with an integer community ID are
**not automatically classified `threadiverse`** if that ID was recorded via
untrusted encoded manual selection. The old `lemmy:` input can carry a user-
supplied numeric ID, so row agreement alone is not independent software
provenance. Ambiguous rows retain `remote_software_profile='unknown'` until
safe discovery; the later publish path must fail closed rather than send a
Lemmy Page to an unverified Group.

## Future v1→v2 boundary

The fixed `src/db/legacy_v0.sql`, `src/db/migration_steps/v0_to_v1.py`, and
`src/db/schema_versions/v1.sql` are independent historical artifacts. A
future merge of the larger platform-independent feature must add an explicit
`1→2` step, compare real v1 user tables and application profiles, and pass
its own backup/data-preservation tests. Successful Git merge is not schema
compatibility evidence. The 47-table feature schema and mixed-version rolling
upgrade are not part of this release.
