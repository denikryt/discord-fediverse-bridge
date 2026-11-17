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
BRIDGE_PLATFORM=discord

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

The deploy helper passes `--env-file` to Compose for variable interpolation. This
matters because `fedify-gateway` receives an explicit environment allowlist,
not the Python bridge's entire env file. For manual Compose commands always use
`docker compose --env-file .env ...` (or `--env-file .env.dev` for development):
`COMPOSE_ENV_FILE` alone does not select the file for `${...}` interpolation.
The gateway receives only the public origin, shared Fedify secret, Actor display
settings, log level, and internal routing details; it must not receive Discord
bot/OAuth credentials. The bridge continues to receive its own env file.

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

## Fluxer trusted-instance opt-in (not yet approved for production writes)

The default Compose project is Discord-only (`BRIDGE_PLATFORM=discord` and no
profiles). An unset `BRIDGE_PLATFORM` selects Discord for compatibility; an
explicitly empty value is invalid and prevents startup. Process environment
values override `.env`, and an explicit Settings constructor value overrides
both. To run Discord and Fluxer side by side, use **two distinct Compose
projects** with separate domains/ActivityPub keys, SQLite data volumes,
published ports, bot tokens, backup directories and environment files.
Do not switch an already initialized Discord database to Fluxer: startup
preflight rejects identity inheritance. The `fluxer-sidecar` service is disabled
by default (`profiles: ["fluxer"]`).
It shares the `bridge` container's network namespace with
`network_mode: "service:bridge"`: its private authenticated RPC listens on
`127.0.0.1:8769`, not a published host port, and forwards authenticated
Gateway observations to Python at `http://127.0.0.1:8080/internal/fluxer/events`.
The Python HTTP listener still binds `0.0.0.0`; the internal ingress path is
protected by its bearer and **must not be routed publicly**. Namespace sharing
alone does not isolate a path on the Python listener.

For an **isolated test deployment only**, use an independent environment
file (for example `.env.fluxer`), not the running Discord service's `.env`.
`BRIDGE_PLATFORM=fluxer` disables all Discord SDK/OAuth owners and requires
exactly one trusted Fluxer connector. A sidecar selected with `--profile fluxer`
while `BRIDGE_PLATFORM=discord` fails its startup mode check; it cannot
silently enable a second connector inside the Discord bridge. Supply a
connector key/IPC bearer that match **both** Python and Node. Set the
following nonsecret values in `.env.fluxer`:

```env
COMPOSE_PROJECT_NAME=fluxer-bridge-test
COMPOSE_ENV_FILE=.env.fluxer
BRIDGE_PLATFORM=fluxer
PUBLIC_BASE_URL=https://fluxer-bridge.example.com
BRIDGE_DATA_VOLUME=fluxer-bridge-test-data
BRIDGE_PUBLISHED_PORT=8082
GATEWAY_PUBLISHED_PORT=3002
BACKUP_HOST_DIR=./backups-fluxer
DISCORD_TOKEN=
DISCORD_OAUTH_CLIENT_ID=
DISCORD_OAUTH_CLIENT_SECRET=
BRIDGE_SUPER_ADMIN_USER_IDS=
FEDIFY_ACTOR_NAME=Fluxer ActivityPub Bridge Gateway
FLUXER_CONNECTORS_JSON=[{"connector_key":"fluxer-primary","origin":"https://trusted.example","rpc_url":"http://127.0.0.1:8769","transport_secret":"same-internal-bearer"}]
FLUXER_IMAGE=ghcr.io/YOUR_GITHUB_OWNER/discord-fediverse-bridge-fluxer
FLUXER_CONNECTOR_KEY=fluxer-primary
FLUXER_ORIGIN=https://trusted.example
FLUXER_APPROVED_API_HOST=api.trusted.example
FLUXER_APPROVED_GATEWAY_HOST=gateway.trusted.example
FLUXER_INTERNAL_BEARER=same-internal-bearer
# Default OFF; opt in only on a dedicated server-build acceptance instance.
FLUXER_VERIFIED_WRITE_CONTRACT=false
FLUXER_BOT_TOKEN_PATH=./secrets/fluxer-bot-token
```

The file referenced by `FLUXER_BOT_TOKEN_PATH` contains the **actual bot
credential**. It is mounted read-only at `/run/secrets/fluxer-bot-token`
**only in the Node sidecar**; neither the file nor its value is a Python
container mount/environment variable. Do not put the bot token in `.env`,
`FLUXER_CONNECTORS_JSON`, Compose interpolation values or logs. A token-bearing
legacy JSON connector configuration is rejected. Set restrictive host
permissions on the token file and do not add it to version control. The IPC
bearer is separate from the bot credential; assign a long random bearer.
Set optional exact forum tag selections in the connector's
`"forum_applied_tags"` map only when confirmed on the actual server.

The pinned SDK lockfile is checked in and validated by CI on Node 22. Once
the image and an actual test instance are available:

```bash
docker compose --env-file .env.fluxer --profile fluxer -f compose.yaml -f compose.build.yaml config
docker compose --env-file .env.fluxer --profile fluxer up -d
# Observe Node SDK auth, health/readiness and authenticated Python ingress.
docker compose --env-file .env.fluxer --profile fluxer ps
docker compose --env-file .env.fluxer --profile fluxer logs --tail=100 bridge fluxer-sidecar
```

Stop in reverse ownership order through Compose (`fluxer-sidecar` depends on
`bridge`). Sidecar restart must not restart the Fluxer-only Python HTTP/SQLite runtime. Recreating
`bridge` must also recreate/rejoin the sidecar's shared network namespace;
`depends_on.restart` requests this for Compose-managed restarts, but host-level
container runtime behavior requires a real two-container test. A port collision
at `127.0.0.1:8769` prevents Node startup rather than routing elsewhere.
Do **not** run two independent sidecars for one configured connector.

**Write acceptance gate:** Node's production write RPCs and positive write
capabilities default to disabled. Before enabling them, record the tested
Fluxer server build, SDK version and forum/tag/ACL/receipt readback results on
an isolated instance. Set `FLUXER_VERIFIED_WRITE_CONTRACT=true` only in that
instance's environment for the controlled write/denial trials, and do not
enable unaccepted builds. This flag never skips SDK-owned checks, fresh
permission evaluation, bot identity admission or SQLite delivery claims.

**Current acceptance blockers:** no Docker runtime was available locally for
B20/B21, and no pinned real Fluxer build/credentials were provided. The SDK
lockfile now supports `npm ci`, and ordinary `npm run check` verifies the actual
SDK write-factory contracts on Node 22. Release validation fails on a missing
or inconsistent lock; it never silently skips the declared Fluxer image. The `fluxer-connector/Dockerfile` intentionally requires the
lockfile and fails to build without it. Production `main.ts` now routes Create/Send/Edit/Delete through the pinned
SDK executors. This candidate has passed SDK-edge and cross-process tests
but requires real-instance REST/starter/permission/Gateway readback before
release acceptance. The Compose layout is
implemented and statically checked but **must not be interpreted as a passed
production deployment or complete issue #12 acceptance**.


### Verify an isolated Fluxer deployment

After starting actual bridge/sidecar images with a pinned test server and test
credentials, run the verifier from the repository root. Always select a separate
Compose project and its environment file explicitly:

```bash
python dev/verify_fluxer_deployment.py --env-file /absolute/path/test.env \
  --project-name fluxer-acceptance --restart-sidecar --stop-services
```

The verifier uses Docker engine state and probes inside the real containers. It
checks the shared namespace, loopback-only RPC listener, unpublished RPC port,
Node-only read-only bot secret, rejected unauthenticated calls, connector-bound
SDK READY identity, independent sidecar restart, and reverse-order clean exits.
After shutdown it opens the same mounted SQLite file under both leases and runs
`PRAGMA integrity_check`. `--stop-services` leaves the selected bridge and
sidecar stopped; volumes are preserved. No physical Fluxer REST write is made.
Raw container configuration, credentials and unfiltered logs are never printed.

The equivalent opt-in pytest requires `FLUXER_COMPOSE_ACCEPTANCE_ENV` and
`FLUXER_COMPOSE_ACCEPTANCE_PROJECT`. Without both, it is explicitly skipped.
Neither static YAML checks nor a local HTTP fixture count as container evidence.
These probes also do not prove Discord message progress or live Fluxer write
contracts; those require the separate live action acceptance checklist.
