# Fedify Gateway Setup

## Required Env

Copy `fedify-gateway/.env.example` to `fedify-gateway/.env` and fill in:

- `FEDIFY_ORIGIN` — public base URL of this gateway
- `PYTHON_BRIDGE_SHARED_SECRET` — must match `FEDIFY_SHARED_SECRET` in the Python bridge `.env`
- `FEDIFY_BRIDGE_PRIVATE_KEY_JWK_JSON` / `FEDIFY_BRIDGE_PUBLIC_KEY_JWK_JSON` — generate once with `npm run generate-keys`

For nginx setup, set `PUBLIC_DOMAIN` for the single public hostname.

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

The deployment model is single-domain. It installs one public site where ActivityPub/WebFinger routes go to fedify-gateway and registration/OAuth/dashboard routes go to the Python bridge.

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
```

Run `nginx-setup.sh` after setting `PUBLIC_DOMAIN`. Public `/healthz` belongs to the gateway. Keep `/internal/` private; it is for gateway-to-Python delivery only.

## Verification

```bash
npm run check
npm run verify:actor-layer
npm run verify:python-contract
npm run verify:publish-contract
```
