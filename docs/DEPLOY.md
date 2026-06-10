# Deployment

## Production: first start

Run these commands from the project root.

### 1. Create the production environment file

```bash
cp .env.example .env
```

Open `.env` and set at least:

```env
COMPOSE_PROJECT_NAME=discord-bridge-prod
COMPOSE_ENV_FILE=.env

BRIDGE_VERSION=0.1.0
BRIDGE_IMAGE=ghcr.io/YOUR_GITHUB_OWNER/discord-fediverse-bridge
GATEWAY_IMAGE=ghcr.io/YOUR_GITHUB_OWNER/discord-fediverse-bridge-gateway

PUBLIC_BASE_URL=https://bridge.example.com
DISCORD_TOKEN=replace-me
FEDIFY_SHARED_SECRET=replace-with-a-long-random-value

BRIDGE_PUBLISHED_PORT=8081
GATEWAY_PUBLISHED_PORT=3000
BACKUP_HOST_DIR=./backups
```

If the GHCR packages are private, log in first:

```bash
echo "$GHCR_TOKEN" | docker login ghcr.io -u YOUR_GITHUB_USER --password-stdin
```

### 2. Pull and start the containers

Use the deploy helper for the selected env file. It prepares `BACKUP_HOST_DIR`, fixes permissions, and starts Compose:

```bash
./scripts/deploy.sh --env-file .env --with-nginx
```

If you start Compose manually instead of using the helper, make sure `BACKUP_HOST_DIR`
exists and is writable by the container user (`10001:10001`) before `docker compose up`:

```bash
mkdir -p "$BACKUP_HOST_DIR"
sudo chown 10001:10001 "$BACKUP_HOST_DIR"
```

If you want to do that from the env file directly without starting Compose yet:

```bash
BACKUP_HOST_DIR="$(grep -E '^BACKUP_HOST_DIR=' .env.sandbox | tail -n1 | cut -d= -f2-)"
mkdir -p "$BACKUP_HOST_DIR"
sudo chown 10001:10001 "$BACKUP_HOST_DIR"
```

### 3. Check the deployment

```bash
docker compose --env-file .env ps
docker compose --env-file .env logs --tail=100 bridge fedify-gateway
curl -fsS http://127.0.0.1:8081/healthz
curl -fsS http://127.0.0.1:3000/healthz
```

The public service should now be available at the URL configured in `PUBLIC_BASE_URL`.

## Development instance on the same machine

The development instance uses a separate domain, Discord bot, ports, Compose project, database volume, and backup directory.

### 1. Create the development environment file

```bash
cp .env.dev.example .env.dev
```

Set at least:

```env
COMPOSE_PROJECT_NAME=discord-bridge-dev
COMPOSE_ENV_FILE=.env.dev

PUBLIC_BASE_URL=https://bridge-dev.example.com
DISCORD_TOKEN=replace-with-a-different-bot-token
FEDIFY_SHARED_SECRET=replace-with-a-different-long-random-value

BRIDGE_PUBLISHED_PORT=8181
GATEWAY_PUBLISHED_PORT=3100
BRIDGE_DATA_VOLUME=discord-fediverse-bridge-dev-data
BACKUP_HOST_DIR=./backups-dev
```

### 2. Build the current working tree and start it

Use the deploy helper for the selected env file. It prepares `BACKUP_HOST_DIR`, fixes permissions, builds the working tree, and starts Compose:

```bash
./scripts/deploy.sh --env-file .env.dev --build --with-nginx
```


If you start Compose manually instead of using the helper, make sure `BACKUP_HOST_DIR`
exists and is writable by the container user (`10001:10001`) before `docker compose up`:

```bash
mkdir -p "$BACKUP_HOST_DIR"
sudo chown 10001:10001 "$BACKUP_HOST_DIR"
```

If you want to do that from the env file directly without starting Compose yet:

```bash
BACKUP_HOST_DIR="$(grep -E '^BACKUP_HOST_DIR=' .env.dev | tail -n1 | cut -d= -f2-)"
mkdir -p "$BACKUP_HOST_DIR"
sudo chown 10001:10001 "$BACKUP_HOST_DIR"
```

### 3. Check the development instance

```bash
docker compose \
  --env-file .env.dev \
  -f compose.yaml \
  -f compose.build.yaml \
  ps

curl -fsS http://127.0.0.1:8181/healthz
curl -fsS http://127.0.0.1:3100/healthz
```

## Update production to another version

Change only `BRIDGE_VERSION` in `.env`, for example:

```env
BRIDGE_VERSION=0.2.0
```

Then run:

```bash
./scripts/deploy.sh --env-file .env --with-nginx
docker compose --env-file .env ps
```

Check the logs after the update:

```bash
docker compose --env-file .env logs --tail=100 bridge fedify-gateway
```

A database backup should exist before changing versions.

## Return production to an earlier version

Set the previous `BRIDGE_VERSION` in `.env`, then run:

```bash
./scripts/deploy.sh --env-file .env --with-nginx
```

If the newer version changed the database schema incompatibly, restore the database backup created before the update as well.

## Create a backup now

Automatic backups are written to `BACKUP_HOST_DIR`. To create one immediately:

```bash
docker compose --env-file .env run --rm backup \
  python -m src.db.backup backup \
  --database /data/bridge.db \
  --output-dir /backups
```

## Stop an instance

Production:

```bash
docker compose --env-file .env down
```

Development:

```bash
docker compose \
  --env-file .env.dev \
  -f compose.yaml \
  -f compose.build.yaml \
  down
```

Do not add `-v` for production. It deletes the database volume.
