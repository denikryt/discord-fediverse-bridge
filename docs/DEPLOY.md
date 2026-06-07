# Deployment

Docker runs the Python bridge, Fedify gateway, and backup worker as separate containers over one shared SQLite volume.

## Configuration

Copy the template and fill in the required values:

```bash
cp .env.example .env
```

Set these at minimum:

```env
PUBLIC_BASE_URL=https://discord-bridge.example.com
DISCORD_TOKEN=...
FEDIFY_SHARED_SECRET=...
DATABASE_URL=sqlite:///./bridge.db
```

Common local overrides:

```env
BRIDGE_PUBLISHED_PORT=8081
GATEWAY_PUBLISHED_PORT=3000
BACKUP_HOST_DIR=./backups
BACKUP_INTERVAL_SECONDS=86400
BACKUP_RETENTION_COUNT=14
```

`PUBLIC_BASE_URL` defines the public federation identity. Changing it changes actor and object URLs.

## Start

```bash
docker compose -f compose.yaml -f compose.build.yaml up -d --build
```

Check health:

```bash
curl http://127.0.0.1:${BRIDGE_PUBLISHED_PORT:-8080}/healthz
curl http://127.0.0.1:${GATEWAY_PUBLISHED_PORT:-3000}/healthz
```

## Backups

The backup worker writes timestamped SQLite snapshots to `BACKUP_HOST_DIR` and keeps the newest `BACKUP_RETENTION_COUNT` files.

Create one snapshot now:

```bash
docker compose run --rm backup \
  python -m src.db.backup backup \
  --database /data/bridge.db \
  --output-dir /backups
```

The periodic backup worker starts with the stack and repeats every `BACKUP_INTERVAL_SECONDS` seconds.

## Restore

Stop writers:

```bash
docker compose stop bridge fedify-gateway backup
```

Restore one snapshot:

```bash
docker compose run --rm --no-deps backup \
  python -m src.db.backup restore \
  --database /data/bridge.db \
  --source /backups/discord-fediverse-bridge-YYYYMMDDTHHMMSSZ.sqlite3
```

Start again:

```bash
docker compose up -d
```

## Notes

- `docker compose down` keeps the named volume.
- `docker compose down -v` deletes the database volume.
- If you are upgrading an old deployment, keep the legacy bridge-key env vars only for the first start; the database row wins after import.
