# Deployment

The Docker stack stores the live SQLite database in the `bridge-data` named volume and automatically writes validated snapshots to a host directory outside that volume.

## Configuration

Create the shared environment file from the repository root:

```bash
cp .env.example .env
```

The bridge actor signing key is initialized automatically in SQLite. Existing deployments may keep `FEDIFY_BRIDGE_PRIVATE_KEY_JWK_JSON` and `FEDIFY_BRIDGE_PUBLIC_KEY_JWK_JSON` in `.env` for the first upgraded start; both values are imported once, and the database row wins on all later starts. New deployments do not generate keys manually.

Configure local backups in the same `.env`:

```env
BACKUP_HOST_DIR=./backups
BACKUP_INTERVAL_SECONDS=86400
BACKUP_RETENTION_COUNT=14
```

The backup directory must be writable by container UID `10001`. Backup files contain all local actor private keys and must remain private.

## Start

```bash
docker compose up -d
```

The `backup` service starts automatically after the bridge becomes healthy, creates an immediate online SQLite snapshot, and repeats at the configured interval.

## Immediate backup

```bash
docker compose run --rm backup \
  python -m src.db.backup backup \
  --database /data/bridge.db \
  --output-dir /backups
```

## Restore

Stop every process that can access the database:

```bash
docker compose stop bridge fedify-gateway backup
```

Restore one validated snapshot:

```bash
docker compose run --rm --no-deps backup \
  python -m src.db.backup restore \
  --database /data/bridge.db \
  --source /backups/discord-fediverse-bridge-YYYYMMDDTHHMMSSZ.sqlite3
```

Then restart the stack and verify health. Compare the bridge actor public key before and after recovery to confirm that the same federation identity was restored.

```bash
docker compose up -d
curl http://127.0.0.1:${BRIDGE_HOST_PORT:-8080}/healthz
curl http://127.0.0.1:${GATEWAY_HOST_PORT:-3000}/healthz
```

Local backups protect against accidental volume deletion and database-file corruption. They do not protect against loss of the whole Docker host; remote backup storage remains future work.
