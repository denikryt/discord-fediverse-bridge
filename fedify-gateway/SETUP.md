# Fedify Gateway Setup

## Required Env

Copy the root template once:

```bash
cp .env.example .env
```

The root `.env` is the only environment file for both the Python bridge and the Fedify gateway. It contains shared values, gateway signing keys and actor metadata, and Docker Compose settings.

Important gateway values include:

- `FEDIFY_ORIGIN` — public base URL of this gateway
- `FEDIFY_SHARED_SECRET` — shared secret used for gateway -> Python delivery
- `PYTHON_BRIDGE_EVENTS_URL` — internal Python intake endpoint
- `DATABASE_URL` — database used by both processes
- `FEDIFY_PORT` — local gateway port
- optional actor metadata overrides

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

The deployment model uses one public host. It installs one public site where ActivityPub/WebFinger routes go to fedify-gateway and the root dashboard, registration, and OAuth routes go to the Python bridge.

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

```env
PUBLIC_DOMAIN=discord-bridge.example.com
GATEWAY_UPSTREAM=http://127.0.0.1:3000
PYTHON_BRIDGE_UPSTREAM=http://127.0.0.1:8081
```

`GATEWAY_UPSTREAM` and `PYTHON_BRIDGE_UPSTREAM` are optional overrides. Leave them at the defaults unless nginx must proxy to a different local bind or port.

Run `nginx-setup.sh` after setting those values in the root `.env`. Public `/healthz` belongs to the gateway. Keep `/internal/` private; it is for gateway-to-Python delivery only.

## Verification

```bash
npm run check
npm run verify:actor-layer
npm run verify:python-contract
npm run verify:publish-contract
```
