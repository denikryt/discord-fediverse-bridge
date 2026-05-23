# Fedify Gateway Setup

## Required Env

Copy the root `.env.example` to `.env` and `fedify-gateway/.env.example` to
`fedify-gateway/.env`.

Root `.env` owns shared deployment values:

- `FEDIFY_ORIGIN` — public base URL of this gateway
- `FEDIFY_SHARED_SECRET` — shared secret used for gateway -> Python delivery
- `PYTHON_BRIDGE_EVENTS_URL` — internal Python intake endpoint
- `PUBLIC_DOMAIN` — public hostname used by `nginx-setup.sh`

`fedify-gateway/.env` keeps gateway-local values:

- `DATABASE_URL`
- `FEDIFY_BRIDGE_PRIVATE_KEY_JWK_JSON` / `FEDIFY_BRIDGE_PUBLIC_KEY_JWK_JSON` — generate once with `npm run generate-keys`
- `FEDIFY_PORT`
- optional actor metadata overrides

## Signing Keys

Generate a persistent RSA key pair and write it to `.env`:

```bash
npm run generate-keys
```

Run once. Use `npm run generate-keys:force` to regenerate. Restart the gateway after.

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
# -> {"status":"ok"}
```

## Nginx

The deployment model uses one public host. It installs one public site where ActivityPub/WebFinger routes go to fedify-gateway and registration/OAuth/dashboard routes go to the Python bridge.

```text
/.well-known/webfinger -> fedify-gateway
/inbox                 -> fedify-gateway
/actors/...            -> fedify-gateway
/communities/...       -> fedify-gateway
/c/...                 -> fedify-gateway
/users/...             -> fedify-gateway
/register              -> Python bridge
/auth/discord/...      -> Python bridge
/dashboard             -> Python bridge
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
