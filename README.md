# Discord-Lemmy Bridge

Python bridge plus a separate Fedify gateway that sync Discord forum threads with federated community actors such as Lemmy communities and compatible Mastodon-facing community flows.

## What This Service Enables

This service bridges Discord forum discussions with fediverse community-style
discussion spaces.

It can:

- connect a Discord forum channel to a remote community actor such as a Lemmy community
- expose a Discord forum channel as a local federated community actor
- publish Discord posts and comments into the fediverse
- mirror fediverse posts and comments back into Discord
- keep multiple subscribed Discord forum channels in sync for the same remote community
- keep one local community in sync across its host forum, local subscriber forums, and remote ActivityPub subscribers

The local community mode is designed to imitate the behavior of a Lemmy
community actor. That is the primary compatibility target. The same community
federation model also works with Mastodon in the supported flows, so the local
community appears there as a community-style actor rather than as an ordinary
single-user account.

## How It Works

The bridge has two main modes.

### 1. Remote community subscription mode

The bridge runs one shared **bridge actor** that follows remote communities on
behalf of Discord subscribers. When a moderator runs `/subscribe-channel`, the
bridge actor sends one shared `Follow` to the target community. Inbound posts
and comments are then fanned out to every subscribed Discord forum channel.

When the last subscribed Discord channel leaves that remote community, the
bridge sends `Undo(Follow)`.

### 2. Local community hosting mode

The bridge can expose a Discord forum as one local ActivityPub `Group` actor.
Remote actors can follow it, and Discord activity in that forum is published as
community content. Host forum messages, local subscriber forum messages, and
remote inbound ActivityPub content can all be synchronized through that local
community.

For outbound authorship, registered Discord users publish through their own
local ActivityPub user identities, so posts and comments stay attributed to the
correct person.

To send messages from Discord into the fediverse, a user must register on the
bridge. Use `/register` and complete the Discord login flow in the browser.

To expose a Discord forum as a local federated community, an allowlisted bot
operator can run `/create_community` with:

- `slug`
- `name`
- `description`
- the target Discord forum channel

Local community discovery is exposed through:

- `acct:!slug@your-gateway-host` for Discord-backed community actors
- `acct:username@your-gateway-host` for registered local Discord users
- `GET /.well-known/discord-fediverse-bridge/communities` for bridge-specific public community discovery used by `/subscribe-channel`

User handles are only local bridge identities for authorship and display in
the fediverse. They are not a remote follow target.

## Access control and federation policy

The bridge supports simple operator-side restrictions:

- you can explicitly list Discord user ids that are allowed to create local communities and subscribe channels to remote communities
- you can explicitly restrict federation to a specific allowlist of remote instance hostnames

These limits are configured through environment variables, so the bridge can be
run either in an open mode or in a tightly controlled mode.

## What Is Synced

Remote community mode:
- Discord forum thread starter → ActivityPub post
- Discord thread message → ActivityPub comment
- inbound federated post → Discord thread
- inbound federated comment → Discord message
- edit / delete propagate in both directions for supported flows

Local community mode:
- host Discord forum thread/message → local federated community content
- local subscriber forum thread/message → same local federated community content
- remote follower post/comment → mirrored into the local community's Discord surfaces
- local community create, edit, and delete fan out to other Discord surfaces and remote ActivityPub subscribers

Vote sync is not implemented.

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
   - outbound `Follow`, `Undo(Follow)`, `Create`, `Update`, `Delete`, and local-community relay delivery
   - inbound federation intake forwarded to Python


## Architecture and navigation

- [docs/architecture/overview.md](docs/architecture/overview.md) — process boundaries and major entry points
- [docs/architecture/bridge-modes.md](docs/architecture/bridge-modes.md) — remote subscriptions vs local communities
- [docs/architecture/event-flows.md](docs/architecture/event-flows.md) — step-by-step behavior traces
- [docs/architecture/http-routes.md](docs/architecture/http-routes.md) — Python/gateway route ownership
- [docs/architecture/database-map.md](docs/architecture/database-map.md) — table ownership and invariants
- [docs/architecture/gateway-python-contract.md](docs/architecture/gateway-python-contract.md) — internal API contract
- [FEDERATION.md](FEDERATION.md) — supported federation profile and protocol scope
- [docs/development/navigation.md](docs/development/navigation.md) — task-oriented reading guide

## Requirements

- Python 3.12+
- Node.js 20+
- a Discord bot with `message content intent` enabled
- a public Lemmy instance URL
- a running `fedify-gateway`
- one public HTTPS domain

In the recommended public-host deployment, nginx owns path routing:

```text
ActivityPub/WebFinger routes -> fedify-gateway
registration/OAuth routes    -> Python bridge
```

## Public host deployment

For a normal deployment, one public hostname can serve both federation and registration UI. Configure the root `.env` like this:

```env
FEDIFY_ORIGIN=https://discord-bridge.example.com
PUBLIC_BRIDGE_BASE_URL=https://discord-bridge.example.com
DISCORD_OAUTH_REDIRECT_URI=https://discord-bridge.example.com/auth/discord/callback
FEDIFY_GATEWAY_URL=http://127.0.0.1:3000
PUBLIC_DOMAIN=discord-bridge.example.com
GATEWAY_UPSTREAM=http://127.0.0.1:3000
PYTHON_BRIDGE_UPSTREAM=http://127.0.0.1:8081
PYTHON_BRIDGE_EVENTS_URL=http://127.0.0.1:8081/internal/activitypub/events
```

`GATEWAY_UPSTREAM` and `PYTHON_BRIDGE_UPSTREAM` are optional overrides for nginx rendering. Keep the defaults unless the gateway or Python bridge listens on a different local address.

The gateway keeps canonical ActivityPub routes such as `/.well-known/webfinger`, `/.well-known/discord-fediverse-bridge/communities`, `/inbox`, `/actors/`, `/communities/`, `/c/`, and `/users/`. Python owns `/`, `/dashboard/...`, `/register`, and `/auth/discord/`. Do not expose `/internal/` publicly through nginx.

Changing `FEDIFY_ORIGIN` on an existing deployment changes ActivityPub actor and object IDs. Treat that as a federation identity migration, not a harmless nginx change.


## Public dashboard

The Python bridge exposes a public dashboard on the root URL `/`. Legacy `/dashboard` links redirect to `/`. The JSON backing endpoint is `/dashboard/data`, and the dashboard web assets live under `/dashboard/static/`.

The dashboard is intentionally public and omits Discord guild/channel IDs, private keys, shared secrets, database paths, and internal service URLs. It does show last-known Discord guild and forum-channel names for hosted local communities, accepted remote subscriptions, and active same-instance local subscribers so operators can see where public routing state lives without exposing raw Discord identifiers.

## Python Bridge Env

Env template: `.env.example`

Required:

- `DISCORD_TOKEN`
- `FEDIFY_SHARED_SECRET`
- `FEDIFY_ORIGIN`
- `DATABASE_URL`

Common:

- `FEDIFY_GATEWAY_URL`
- `INTERNAL_HTTP_HOST`
- `INTERNAL_HTTP_PORT`
- `PUBLIC_BRIDGE_BASE_URL`
- `PUBLIC_DOMAIN`
- `GATEWAY_UPSTREAM`
- `PYTHON_BRIDGE_UPSTREAM`
- `PYTHON_BRIDGE_EVENTS_URL`
- `LOG_LEVEL`

Optional:

- `FEDERATION_ALLOWLIST` — comma-separated Lemmy hostnames to accept; empty means all instances allowed
- `LOCAL_COMMUNITY_OPERATOR_ALLOWLIST` — comma-separated Discord user ids allowed to manage local communities
- `REMOTE_SUBSCRIPTION_OPERATOR_ALLOWLIST` — comma-separated Discord user ids allowed to subscribe channels to remote communities
- `BRIDGE_DISPLAY_PREFIX`

Needed only for web registration:

- `DISCORD_OAUTH_CLIENT_ID`
- `DISCORD_OAUTH_CLIENT_SECRET`
- `DISCORD_OAUTH_REDIRECT_URI`

## Gateway Env

Env template: `fedify-gateway/.env.example`

Required:

- `DATABASE_URL`
- `FEDIFY_BRIDGE_PRIVATE_KEY_JWK_JSON` — generate with `npm run generate-keys`
- `FEDIFY_BRIDGE_PUBLIC_KEY_JWK_JSON` — generate with `npm run generate-keys`

Common:

- `FEDIFY_PORT`

Optional:

- `FEDIFY_ACTOR_IDENTIFIER`
- `FEDIFY_ACTOR_NAME`
- `FEDIFY_ACTOR_SUMMARY`

The gateway also reads shared deployment values from the root `.env`, including
`FEDIFY_ORIGIN`, `FEDIFY_SHARED_SECRET`, `PYTHON_BRIDGE_EVENTS_URL`, and
`LOG_LEVEL`.

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
npm test
```

## Discord bot commands

- `/register`
- `/subscribe-channel`
- `/unsubscribe-channel`
- `/list-subscriptions`
- `/create_community`

## Nginx

The repository includes:

- `fedify-gateway/nginx.conf`
- `fedify-gateway/nginx-setup.sh`
- `fedify-gateway/SETUP.md`

## Known issues

See `notes/known_issues.md` for the current short issue journal and verified behavior notes.
