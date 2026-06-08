# Fedify Gateway Setup

## Required Env

Copy the root template once:

```bash
cp .env.example .env
```

The gateway reads the same root `.env` as the Python bridge.

Keep these values set:

- `PUBLIC_BASE_URL` — one public origin shared by both services
- `FEDIFY_SHARED_SECRET` — shared secret for authenticated bridge reads and delivery
- `GATEWAY_BIND_PORT` — local gateway port for direct runs
- `BRIDGE_BIND_HOST` / `BRIDGE_BIND_PORT` — local bridge bind defaults for direct runs
- `BRIDGE_GATEWAY_URL` — bridge-side CLI target used by operator commands

The gateway derives its bridge API base from `BRIDGE_EVENTS_URL` when Compose provides it. Otherwise it falls back to `BRIDGE_BIND_HOST` / `BRIDGE_BIND_PORT`.

## Signing Keys

The bridge service actor key is initialized automatically in the shared database. Existing deployments may keep both legacy JWK variables in the root `.env` for one upgraded start so the Python bridge imports the existing identity before the gateway becomes healthy.

## Start

```bash
cd fedify-gateway
npm install
npm run start
```

Development (auto-reload):

```bash
npm run dev
```

## Health Check

```bash
curl http://127.0.0.1:3000/healthz
# -> {"status":"ok","version":"..."}
```

## Nginx

One public host serves both services:

```text
`/`                     -> Python bridge
/.well-known/webfinger -> fedify-gateway
/.well-known/discord-fediverse-bridge/communities -> fedify-gateway
/inbox                 -> fedify-gateway
/actors/...            -> fedify-gateway
/communities/...       -> fedify-gateway
/c/...                 -> fedify-gateway
/users/...             -> fedify-gateway
/register              -> Python bridge
/auth/discord/...      -> Python bridge
/dashboard             -> Python bridge (redirects to `/`)
/dashboard/static/...  -> Python bridge
/dashboard/data        -> Python bridge
```

Set `PUBLIC_BASE_URL` once in the root `.env`. `nginx-setup.sh` derives the hostname from that URL and proxies to `BRIDGE_PUBLISHED_PORT` and `GATEWAY_PUBLISHED_PORT`. Keep `/internal/` private; it is the authenticated gateway-to-Python API and must never be exposed by nginx.

## Verification

```bash
npm run check
npm test
```
