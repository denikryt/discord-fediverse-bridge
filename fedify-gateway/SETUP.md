# Fedify Gateway Setup

## Required Env

Copy `fedify-gateway/.env.example` to `fedify-gateway/.env` and fill in:

- `FEDIFY_ORIGIN` — public base URL of this gateway
- `PYTHON_BRIDGE_SHARED_SECRET` — must match `FEDIFY_SHARED_SECRET` in the Python bridge `.env`
- `FEDIFY_BRIDGE_PRIVATE_KEY_JWK_JSON` / `FEDIFY_BRIDGE_PUBLIC_KEY_JWK_JSON` — generate once with `npm run generate-keys`

For nginx setup, prefer `DEPLOYMENT_MODE=single-domain` with `PUBLIC_DOMAIN`. Legacy two-domain setup still works with `DEPLOYMENT_MODE=two-domain`, `GATEWAY_DOMAIN`, and `BRIDGE_DOMAIN`.

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

Single-domain mode is recommended for normal deployments. It installs one public site where ActivityPub/WebFinger routes go to fedify-gateway and registration/OAuth routes go to the Python bridge.

```env
DEPLOYMENT_MODE=single-domain
PUBLIC_DOMAIN=bot.example.com
```

Legacy two-domain mode remains supported:

```env
DEPLOYMENT_MODE=two-domain
GATEWAY_DOMAIN=bot.example.com
BRIDGE_DOMAIN=bridge.bot.example.com
```

Run `nginx-setup.sh` after setting the chosen mode. Public `/healthz` belongs to the gateway in single-domain mode. Keep `/internal/` private; it is for gateway-to-Python delivery only.

## Verification

```bash
npm run check
npm run verify:actor-layer
npm run verify:python-contract
npm run verify:publish-contract
```
