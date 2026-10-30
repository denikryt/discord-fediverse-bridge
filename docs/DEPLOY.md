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

The `fluxer-sidecar` service is disabled by default (`profiles: ["fluxer"]`).
It shares the `bridge` container's network namespace with
`network_mode: "service:bridge"`: its private authenticated RPC listens on
`127.0.0.1:8769`, not a published host port, and forwards authenticated
Gateway observations to Python at `http://127.0.0.1:8080/internal/fluxer/events`.
The Python HTTP listener still binds `0.0.0.0`; the internal ingress path is
protected by its bearer and **must not be routed publicly**. Namespace sharing
alone does not isolate a path on the Python listener.

For an **isolated test deployment only**, configure exactly one trusted origin
and exactly matching connector key/IPC bearer on both services. Set the
following nonsecret values in the bridge's `.env`:

```env
FLUXER_CONNECTORS_JSON=[{"connector_key":"fluxer-primary","origin":"https://trusted.example","rpc_url":"http://127.0.0.1:8769","transport_secret":"same-internal-bearer"}]
FLUXER_IMAGE=ghcr.io/YOUR_GITHUB_OWNER/discord-fediverse-bridge-fluxer
FLUXER_CONNECTOR_KEY=fluxer-primary
FLUXER_ORIGIN=https://trusted.example
FLUXER_APPROVED_API_HOST=api.trusted.example
FLUXER_APPROVED_GATEWAY_HOST=gateway.trusted.example
FLUXER_INTERNAL_BEARER=same-internal-bearer
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

Once a pinned SDK lockfile, image and actual test instance are available:

```bash
docker compose --env-file .env --profile fluxer -f compose.yaml -f compose.build.yaml config
docker compose --env-file .env --profile fluxer up -d
# Observe Node SDK auth, health/readiness and authenticated Python ingress.
docker compose --env-file .env --profile fluxer ps
docker compose --env-file .env --profile fluxer logs --tail=100 bridge fluxer-sidecar
```

Stop in reverse ownership order through Compose (`fluxer-sidecar` depends on
`bridge`). Sidecar restart never restarts the Python Discord client. Recreating
`bridge` must also recreate/rejoin the sidecar's shared network namespace;
`depends_on.restart` requests this for Compose-managed restarts, but host-level
container runtime behavior requires a real two-container test. A port collision
at `127.0.0.1:8769` prevents Node startup rather than routing elsewhere.
Do **not** run two independent sidecars for one configured connector.

**Current acceptance blockers:** There is no verified
`fluxer-connector/package-lock.json` (the npm registry was unreachable),
the official SDK declarations have not been typechecked, no Docker runtime was
available for B20/B21, and no pinned real Fluxer build/credentials were
provided. The `fluxer-connector/Dockerfile` intentionally requires the
lockfile and fails to build without it. Production `main.ts` currently refuses
all Create/Send/Edit/Delete operations; these must not be enabled before live
REST/starter/permission/Gateway contract verification. The Compose layout is
implemented and statically checked but **must not be interpreted as a passed
production deployment or complete issue #12 acceptance**.
