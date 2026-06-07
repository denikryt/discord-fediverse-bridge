# Fedify Gateway Setup

## Required Env

Copy the root template once:

```bash
cp .env.example .env
```

The root `.env` is the only environment file for both the Python bridge and the Fedify gateway. It contains shared values, gateway signing keys and actor metadata, and Docker Compose settings.

Important gateway values include:

- `PUBLIC_BASE_URL` — the one public origin shared with the Python bridge
- `FEDIFY_SHARED_SECRET` — shared secret used for gateway -> Python delivery
- `DATABASE_URL` — database used by both processes
- optional actor metadata overrides

The local bridge-to-gateway URL, gateway port, and Python intake URL use built-in defaults for direct runs and Compose-owned internal values in Docker. They are not duplicated as Docker-only values in the operator `.env`.

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

Set `PUBLIC_BASE_URL` once in the root `.env`. `nginx-setup.sh` derives the hostname from that URL and proxies to `BRIDGE_PUBLISHED_PORT` and `GATEWAY_PUBLISHED_PORT`. Advanced one-off upstream overrides may still be exported in the shell, but they are not part of the normal env contract.

Run `nginx-setup.sh` after configuring the root `.env`. Public `/healthz` belongs to the gateway. Keep `/internal/` private; it is for gateway-to-Python delivery only.

## Verification

```bash
npm run check
npm run verify:actor-layer
npm run verify:python-contract
npm run verify:publish-contract
```
