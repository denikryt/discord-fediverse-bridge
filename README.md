# Discord-Lemmy Bridge

Python bridge plus a separate Fedify gateway that sync Discord forum threads with Lemmy communities.

## Processes

The project runs as two processes:

1. `Python bridge`
   - Discord bot
   - internal FastAPI server
   - registration backend
   - subscription state and message mappings in SQLite

2. `fedify-gateway`
   - ActivityPub protocol edge
   - local actor documents and WebFinger
   - outbound `Follow` and `Create`
   - inbound federation intake forwarded to Python

## What Is Synced

- Discord forum thread starter -> ActivityPub post
- Discord message inside a mapped thread -> ActivityPub comment
- inbound ActivityPub post -> Discord forum thread + starter message
- inbound ActivityPub comment -> Discord thread message

Only create flows are implemented. Edits, deletes, attachments, and vote sync are not implemented.

## Requirements

- Python 3.12+
- Node.js 20+
- a Discord bot with `message content intent` enabled
- a public Lemmy instance URL
- a running `fedify-gateway`

## Python Bridge Env

Env template: `.env.example`

Required:

- `DISCORD_TOKEN`
- `LEMMY_BASE_URL`
- `FEDIFY_SHARED_SECRET`

Common:

- `FEDIFY_GATEWAY_URL`
- `INTERNAL_HTTP_HOST`
- `INTERNAL_HTTP_PORT`
- `PUBLIC_BRIDGE_BASE_URL`

Needed only for web registration:

- `DISCORD_OAUTH_CLIENT_ID`
- `DISCORD_OAUTH_CLIENT_SECRET`
- `DISCORD_OAUTH_REDIRECT_URI`

## Gateway Env

Env template: `fedify-gateway/.env.example`

Required:

- `FEDIFY_ORIGIN`
- `PYTHON_BRIDGE_SHARED_SECRET`

Common:

- `PYTHON_BRIDGE_EVENTS_URL`
- `FEDIFY_PORT`

## Install

Python:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e vendor/discordops
pip install -e '.[dev]'
```

Gateway:

```bash
cd fedify-gateway
npm install
```

## Run

Start the gateway in one terminal:

```bash
cd fedify-gateway
npm run start
```

Start the Python bridge in another terminal:

```bash
source .venv/bin/activate
python -m src.app
```

## Tests

Python test suite:

```bash
source .venv/bin/activate
.venv/bin/pytest -q
```

Gateway checks:

```bash
cd fedify-gateway
npm run check
npm run verify:actor-layer
npm run verify:python-contract
npm run verify:publish-contract
```

## Registration

Routes served by the Python bridge:

- `GET /register`
- `GET /auth/discord/start`
- `GET /auth/discord/callback`
- `POST /register/complete`
- `GET /register/success`

## Commands

- `/register`
- `/subscribe-channel`
- `/unsubscribe-channel`
- `/list-subscriptions`

## Nginx

The repository includes:

- `fedify-gateway/nginx.conf`
- `fedify-gateway/nginx-setup.sh`
- `fedify-gateway/SETUP.md`
