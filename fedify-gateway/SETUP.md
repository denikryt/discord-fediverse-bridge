# Fedify Gateway Setup

## Required Env

Copy `fedify-gateway/.env.example` to `fedify-gateway/.env` and fill in:

- `FEDIFY_ORIGIN` — public base URL of this gateway
- `PYTHON_BRIDGE_SHARED_SECRET` — must match `FEDIFY_SHARED_SECRET` in the Python bridge `.env`
- `FEDIFY_BRIDGE_PRIVATE_KEY_JWK_JSON` / `FEDIFY_BRIDGE_PUBLIC_KEY_JWK_JSON` — generate once with `npm run generate-keys`

For nginx setup also set `GATEWAY_DOMAIN` and `BRIDGE_DOMAIN`.

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

Run `nginx-setup.sh` to install vhosts from `nginx.conf` and `bridge.conf`. Requires `GATEWAY_DOMAIN` and `BRIDGE_DOMAIN` in `.env`.

## Verification

```bash
npm run check
npm run verify:actor-layer
npm run verify:python-contract
npm run verify:publish-contract
```
