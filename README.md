# Discord-Lemmy Bridge

Python bridge plus a separate Fedify gateway that sync Discord forum threads with Lemmy communities.

## What This Service Enables

This service lets one Lemmy community act as a shared discussion space across
Discord and Lemmy.

It allows you to:

- connect a Discord forum channel to one Lemmy community
- expose a Discord forum channel as one local federated community that Lemmy
  users can follow
- send a message in one subscribed Discord channel and have it appear in all
  other subscribed Discord channels for the same community
- publish Discord posts and comments to Lemmy
- mirror Lemmy posts and comments back into Discord
- subscribe multiple Discord forum channels from different guilds to the same Lemmy community
- keep those subscribed Discord channels in sync so posts and comments appear in all of them

The bridge keeps a single ActivityPub presence for the Lemmy community side and
creates per-user ActivityPub identities for Discord users who publish content.
It can also host a Discord forum channel as one local community actor with the
`/create_community` command.

## How It Works

The bridge runs a single **bridge actor** - one ActivityPub identity that
follows Lemmy communities on behalf of all subscribers. When a moderator runs
`/subscribe-channel`, the bridge actor sends a `Follow` to the target
community. Inbound posts and comments are delivered to this actor and then
fanned out to every subscribed Discord channel. The `Follow` is shared across
channels: if multiple channels subscribe to the same community, only one AP
follow is sent.

When the last subscribed Discord channel leaves one remote community, the
bridge sends `Undo(Follow)` for the shared bridge actor. If that remote cleanup
fails, the bridge keeps the shared follow row so operators can retry instead of
silently losing federation state.

For outbound traffic, each registered Discord user gets their own **individual
AP identity**. These identities are used to publish posts and comments so they
appear attributed to the correct person in the fediverse. They are not used for
`Follow` activities - that is handled exclusively by the bridge actor.

Discord channels from different guilds can subscribe to the same Lemmy community. Inbound posts are delivered to all of them.

To send messages from Discord into Lemmy, a user must register on the bridge.
Use the `/register` command - the bot replies with a registration link.
Following it opens a web page where the user logs in with their Discord account
and chooses a username.

To expose a Discord forum as a local federated community, an allowlisted bot
operator can run `/create_community` with:

- `slug`
- `name`
- `description`
- the target Discord forum channel

Local community discovery is exposed through:

- `acct:!slug@your-gateway-host` for Discord-backed community actors
- `acct:username@your-gateway-host` for registered local Discord users

User handles are only local bridge identities for authorship and display in
the fediverse. They are not a remote follow target.

## What Is Synced

Discord → Lemmy + sibling Discord channels (registration required — messages are not forwarded without it):
- forum thread starter → ActivityPub post, mirrored to all other subscribed channels
- thread message → ActivityPub comment, mirrored to all sibling threads
- edit / delete propagated to Lemmy and all sibling mirrors

Lemmy → all subscribed Discord channels:
- inbound post → thread created in every subscribed forum channel
- inbound comment → message in each mapped thread
- edit / delete propagated to Discord

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
   - outbound `Follow` and `Create`
   - inbound federation intake forwarded to Python


## Architecture and navigation

- [docs/architecture/overview.md](docs/architecture/overview.md) — process boundaries and major entry points
- [docs/architecture/bridge-modes.md](docs/architecture/bridge-modes.md) — remote subscriptions vs local communities
- [docs/architecture/event-flows.md](docs/architecture/event-flows.md) — step-by-step behavior traces
- [docs/architecture/http-routes.md](docs/architecture/http-routes.md) — Python/gateway route ownership
- [docs/architecture/database-map.md](docs/architecture/database-map.md) — table ownership and invariants
- [docs/architecture/gateway-python-contract.md](docs/architecture/gateway-python-contract.md) — internal API contract
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

The gateway keeps canonical ActivityPub routes such as `/.well-known/webfinger`, `/inbox`, `/actors/`, `/communities/`, `/c/`, and `/users/`. Python owns `/register` and `/auth/discord/`. Do not expose `/internal/` publicly through nginx.

Changing `FEDIFY_ORIGIN` on an existing deployment changes ActivityPub actor and object IDs. Treat that as a federation identity migration, not a harmless nginx change.


## Public dashboard

The Python bridge exposes a public dashboard at `/dashboard`. It shows bridge statistics, local communities, subscriber counts, bridge-actor follows, and the effective federation policy. The JSON backing endpoint is `/dashboard/data`.

The dashboard is intentionally public and omits Discord guild/channel IDs, private keys, shared secrets, database paths, and internal service URLs.

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
npm run verify:actor-layer
npm run verify:python-contract
npm run verify:publish-contract
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

- Mastodon replies to Lemmy-origin posts in the bridge community can bypass the bridge inbox.
- Lemmy may receive those replies, but Discord will not see them because the gateway never receives the `Create(Note)`.
