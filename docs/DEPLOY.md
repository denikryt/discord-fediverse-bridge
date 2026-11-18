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

BRIDGE_VERSION=replace-with-published-version
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

Change only `BRIDGE_VERSION` in `.env` to the published image version, for example:

```env
BRIDGE_VERSION=replace-with-published-version
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

## Durable user profile media

The bridge stores validated profile images under `/data/profile-media` and keeps their metadata and user references in `/data/bridge.db`. Back up and restore these paths only through `python -m src.db.backup` complete-archive commands; copying or restoring the SQLite file alone is incomplete after profile media exists.

The bridge process holds a shared lock on `/data/deployment-state.lock` for its full lifetime. Complete restore requires the exclusive form of that lock, so it fails or waits while the bridge is running. The backup service uses only explicit database, media, lock, interval, retention, and output paths; it must not receive Discord, OAuth, gateway, or signing secrets.

## Discord Message Content intent and personal feeds

The bot enables Discord's Message Content intent in code. Keep that privileged intent enabled and, where required by Discord, approved in the Discord Developer Portal for every production application. Personal-feed publication requires the complete message body; disabling the portal-side intent leaves binding rows intact but causes text-only roots and replies to arrive without usable content and be ignored. Attachments and embeds without text are not published.

The bot also needs permission in each bound feed channel to view the channel, read message history, create public threads, send messages in threads, and manage only messages it previously rendered when applying remote Update/Delete. A retained inbound comment is not discarded when Discord is unavailable: the worker performs at most three Discord calls and then records a durable terminal failure. Moving or removing a binding intentionally prevents further synchronization to the old channel; retained history is not replayed into a replacement channel in Stage 8.


## Remote Person Feed prerequisites and operations

The bot needs the Message Content intent described above and these **effective** permissions in each assigned guild text channel: `View Channel`, `Read Message History`, `Send Messages`, `Manage Webhooks`, `Create Public Threads`, and `Send Messages in Threads`. `Read Message History` is also required in discussion threads for native Reply and reconciliation. A registered guild user with Discord `Manage Server` can run `/person-feed follow actor channel` and `/person-feed add-channel`; the remaining `/person-feed` operations are restricted to that user's follows through the existing DiscordOps policy.

Top-level posts use an Incoming webhook created by the bot. Keep the original bot-created webhook and its token available in Discord: edits/deletes of previously delivered posts require the **same** webhook, not a replacement. If it was deleted or its token is unavailable, restore that webhook's credentials through an authorized Discord-side recovery process if possible; the bridge records a per-destination failure and will not substitute a new webhook to modify an old post. The token is never stored in SQLite. Check destination delivery states and worker logs when a Discord permission is revoked or a webhook disappears.

If a POST or signed Follow/Undo has an ambiguous HTTP outcome, delivery state remains `uncertain`/`unknown`; it is not safe to reset and resend blindly. Confirm the remote event or original Discord message before explicitly recovering it. Removing the last accepted destination suppresses **unsubmitted** creates; an already-authorized in-flight send may finish. Existing Discord copies still receive authenticated edits/deletes after an unsubscribe. The bridge does not automatically republish historical posts when a new channel is assigned.

## SQLite v2 upgrade

The deployment now uses SQLite schema **v2**. Stop all older bridge processes and restart supervisors before upgrading, and take a complete verified backup of the database and profile media. With the exclusive deployment lock, startup validates the historical v0/v1 physical schema, creates a protected migration backup, and performs the v1→v2 rebuild of the publication-context tables inside one transaction. Fresh deployments create v2 directly. The runner verifies foreign keys, integrity, and the frozen v2 schema fingerprint before admitting the database. An unrecognized or partially migrated schema fails startup; do not run old binaries against a v2 database. Follow the restore procedure below if an upgrade fails.


## Stage 9 maintenance

Use `bridge-user-deliveries` for bounded, database-only diagnostics and explicit retry resets. Bulk retry requires `--username --all-failed --limit` and processes at most 100 rows. Use `bridge-profile-media cleanup` with explicit database, media root, deployment lock, and media lock paths. A complete backup must contain both the SQLite database and profile-media volume; exporting a container image is not a volume backup.

## Upgrading a pre-v1 SQLite deployment (via v1 to v2)

Legacy SQLite deployments require a strictly offline cutover:
stop and disable **all** pre-v1 workers and their restart supervisors before
starting new binaries. Do not mix old and new code against one SQLite file.
The new startup performs source validation, a protected no-clobber migration
backup and an atomic `0→1→2` upgrade under the existing exclusive deployment
lock, then reacquires a shared lifetime lock and validates v2 read-only.
Keep a verified backup of the database and profile media. Supported v0 schemas
are the tagged v0.1.0 schema and the recognised main schema; an unknown schema
stops startup without migration. The automatic migration backup is stored in
`/data/backups/migrations/` when the database is `/data/bridge.db`.

If migration fails, keep the services stopped and investigate a copy of the
database. To restore a verified migration archive, run the backup restore
command in the bridge environment with the correct paths:

```bash
python -m src.db.backup restore \
  --database /data/bridge.db \
  --media-root /data/profile-media \
  --deployment-lock /data/deployment-state.lock \
  --source /data/backups/migrations/<verified-backup>.tar.gz
```

The restored database remains at v0. Start only the intended application
version after confirming that all other database writers remain stopped.

### Development v2 schema alignment for per-Follow/guild destinations

The development v2 schema now rejects a second channel for the same
`(follow_id, discord_guild_id)`. Separate follows may still share channels,
and one Follow may have destinations in different guilds. Fresh v2 databases
and the existing v1→v2 upgrade create this constraint automatically; schema
version remains **2**. An older *physical* v2 database is **not** upgraded
silently: startup rejects its outdated fingerprint.

Before deploying this development revision to an existing v2 volume, **stop
all writers**, obtain the owner's explicit authorization, and take a verified
full database/media backup. Audit a read-only copy for conflicts:

```sql
SELECT follow_id, discord_guild_id, COUNT(*) AS assignments
FROM remote_person_channel_assignments
GROUP BY follow_id, discord_guild_id
HAVING COUNT(*) > 1;
```

If rows are returned, resolve their destination ownership with the owner;
never delete a user's assignment automatically. The owner must then authorize
either the one named unique index on `(follow_id, discord_guild_id)`
or a complete **development** DB reset. The exact DDL is:

```sql
CREATE UNIQUE INDEX uq_remote_person_assignment_follow_guild ON remote_person_channel_assignments (follow_id, discord_guild_id);
```

The strict schema guard requires the exact name and SQL shape. Do not perform
alignment or a reset as an application startup side effect.

On a stopped **development** copy with its complete backup verified, the
explicit offline tool can create a verified output copy with the fixed v2 DDL:

```bash
python -m scripts.align_dev_v2_assignments \
  --source /path/to/stopped-old-v2.db \
  --output /path/to/aligned-v2.db \
  --offline-confirmed
```

The command refuses conflicting Follow/guild rows and unrelated schema
changes. For an original CP21 physical v2 database it adds only the named
index; the recognized CP32 table-constraint variant is rebuilt explicitly
to the same named-index layout. It writes only a new file, preserving all
existing rows and the unchanged source. It checks `PRAGMA user_version=2`, foreign keys, SQLite
integrity and the complete v2 physical schema fingerprint before returning.
Replace the development database only after inspecting the output and with
all writers stopped; keep the original database and media backup for rollback.
No production startup performs this operation, and no v3 migration is added.

After the approved offline operation, check `PRAGMA user_version` (=2),
`PRAGMA integrity_check` (=ok), `PRAGMA foreign_key_check` (empty), and that
startup accepts the frozen v2 physical fingerprint. Verify an immediate
second startup is read-only and does not create a new migration backup.
