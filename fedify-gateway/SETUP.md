# Fedify Gateway Setup

## What This Process Does

`fedify-gateway` is the public ActivityPub edge for the project.

It is responsible for:

- local actor documents and WebFinger
- signed outbound `Follow`
- signed outbound user-authored `Create`
- inbound federation intake
- forwarding normalized events to the Python bridge

## Required Env

Env template: `fedify-gateway/.env.example`

Required:

- `FEDIFY_ORIGIN`
- `PYTHON_BRIDGE_SHARED_SECRET`

Common:

- `FEDIFY_PORT`
- `PYTHON_BRIDGE_EVENTS_URL`
- `FEDIFY_ACTOR_IDENTIFIER`
- `FEDIFY_ACTOR_NAME`
- `FEDIFY_ACTOR_SUMMARY`

## Example Env

```env
FEDIFY_ORIGIN=https://bot-test.nachitima.com/
FEDIFY_PORT=3000
DATABASE_URL=sqlite:///../bridge.db
PYTHON_BRIDGE_EVENTS_URL=http://127.0.0.1:8081/internal/activitypub/events
PYTHON_BRIDGE_SHARED_SECRET=change-me
FEDIFY_ACTOR_IDENTIFIER=bridge
FEDIFY_ACTOR_NAME=Discord Lemmy Bridge Gateway
FEDIFY_ACTOR_SUMMARY=Receives ActivityPub activities from Lemmy and forwards normalized events to the Python bridge.
```

## Persistent Signing Keys

The gateway signs all outbound ActivityPub requests (Follow, Create) with an RSA key pair.
If no keys are configured, a fresh in-memory pair is generated on every restart — remote servers
that cached the old public key will reject the new signatures.

Generate a persistent pair and write it to `fedify-gateway/.env` in one command:

```bash
npm run generate-keys
```

This adds `FEDIFY_BRIDGE_PRIVATE_KEY_JWK_JSON` and `FEDIFY_BRIDGE_PUBLIC_KEY_JWK_JSON` to `fedify-gateway/.env`.
Run it once; subsequent calls are a no-op unless you need to regenerate:

```bash
npm run generate-keys:force
```

Restart the gateway after running the script.

## Start

```bash
cd fedify-gateway
npm install
npm run start
```

For local development:

```bash
npm run dev
```

## Health Check

Local:

```bash
curl http://127.0.0.1:3000/healthz
```

Behind nginx:

```bash
curl https://bot-test.nachitima.com/healthz
```

Expected response:

```json
{"status":"ok"}
```

## Nginx

Files:

- `nginx.conf`
- `nginx-setup.sh`

## Manual Follow Check

The internal follow endpoint requires the shared secret:

```bash
curl -X POST http://127.0.0.1:3000/follow-community \
  -H "Authorization: Bearer change-me" \
  -H "Content-Type: application/json" \
  -d '{"communityActorUrl":"https://lemmy.example/c/general"}'
```

## Publish Check

The internal publish endpoint also requires the shared secret:

```bash
curl -X POST http://127.0.0.1:3000/publish \
  -H "Authorization: Bearer change-me" \
  -H "Content-Type: application/json" \
  -d '{
    "actorUsername":"alice",
    "communityActorUrl":"https://lemmy.example/c/general",
    "kind":"post",
    "title":"Hello from bridge",
    "bodyMarkdown":"test body",
    "inReplyToObjectId":null
  }'
```

## Verification Scripts

```bash
npm run check
npm run verify:actor-layer
npm run verify:python-contract
npm run verify:publish-contract
```
