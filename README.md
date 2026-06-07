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

Configure one public URL in the root `.env`:

```env
PUBLIC_BASE_URL=https://discord-bridge.example.com
```

The bridge derives federation actor/object URLs, registration links, the OAuth callback, and the nginx hostname from this value. Docker-only service URLs remain internal Compose details and are not repeated in `.env`. External nginx defaults to the published `BRIDGE_HOST_PORT` and `GATEWAY_HOST_PORT`.

The gateway keeps canonical ActivityPub routes such as `/.well-known/webfinger`, `/.well-known/discord-fediverse-bridge/communities`, `/inbox`, `/actors/`, `/communities/`, `/c/`, and `/users/`. Python owns `/`, `/dashboard/...`, `/register`, and `/auth/discord/`. Do not expose `/internal/` publicly through nginx.

Changing `PUBLIC_BASE_URL` on an existing deployment changes ActivityPub actor and object IDs. Treat that as a federation identity migration, not a harmless nginx change.


## Public dashboard

The Python bridge exposes a public dashboard on the root URL `/`. Legacy `/dashboard` links redirect to `/`. The JSON backing endpoint is `/dashboard/data`, and the dashboard web assets live under `/dashboard/static/`.

The dashboard is intentionally public and omits Discord guild/channel IDs, private keys, shared secrets, database paths, and internal service URLs. It does show last-known Discord guild and forum-channel names for hosted local communities, accepted remote subscriptions, and active same-instance local subscribers so operators can see where public routing state lives without exposing raw Discord identifiers.

## Environment

The root `.env` is shared by the Python bridge, Fedify gateway, systemd, and Docker Compose. Template: `.env.example`.

Required application values:

- `DISCORD_TOKEN`
- `FEDIFY_SHARED_SECRET`
- `PUBLIC_BASE_URL`
- `DATABASE_URL`

Optional application values include federation/operator allowlists, Discord OAuth credentials, actor display metadata, and logging. The OAuth callback defaults to `${PUBLIC_BASE_URL}/auth/discord/callback`; an explicit `DISCORD_OAUTH_REDIRECT_URI` remains available only for unusual deployments.

Docker settings are grouped in the same file: image/version selection, published host ports, data volume, backup retention, and optional bundled-nginx settings. Internal bind addresses and container-to-container URLs are supplied by Compose and do not need operator configuration.

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

## Docker deployment

The supported Docker deployment runs the Python bridge and Fedify gateway as two containers with one shared SQLite volume. The default stack does not include a reverse proxy; expose it through an existing nginx, Caddy, or Traefik installation.

### Prepare configuration

Copy the single environment template and fill in application, gateway, and Docker values:

```bash
cp .env.example .env
```

The Python bridge, Fedify gateway, systemd units, and Docker Compose all read this same root `.env` file. Docker-only image, port, volume, and optional nginx settings are grouped in the Docker section of `.env.example`.

The default deployment expects versioned images named `discord-fediverse-bridge` and `discord-fediverse-bridge-fedify-gateway`. For a local source build, add `compose.build.yaml`.

### Start with an external reverse proxy

```bash
docker compose \
  -f compose.yaml \
  -f compose.build.yaml \
  up -d --build
```

The bridge is bound to `127.0.0.1:8080` and the gateway to `127.0.0.1:3000` by default. Route dashboard, OAuth, registration, health, and other bridge HTTP paths to port 8080. Route WebFinger, actor/object, inbox, and ActivityPub paths to port 3000. Keep one public origin for both services.

Check health and logs:

```bash
curl http://127.0.0.1:8080/healthz
curl http://127.0.0.1:3000/healthz
docker compose logs -f bridge fedify-gateway
```

### Optional bundled nginx

For a self-contained HTTP proxy suitable for local verification or an operator-managed TLS mount:

```bash
docker compose \
  -f compose.yaml \
  -f compose.build.yaml \
  -f compose.nginx.yaml \
  up -d --build
```

The optional nginx service listens on host port 8088 by default and uses `deploy/nginx/default.conf.template`. Certificate issuance is intentionally outside this setup; mount existing files through `NGINX_TLS_DIR` when extending the template for TLS.

### Backup, update, and rollback

Stop writers before copying the SQLite database:

```bash
docker compose stop bridge fedify-gateway
docker run --rm -v discord-fediverse-bridge-data:/data -v "$PWD":/backup alpine \
  cp /data/bridge.db /backup/bridge.db.backup
docker compose start bridge fedify-gateway
```

To update, set `BRIDGE_VERSION` in the root `.env` to the exact release, pull or build both images, and recreate the stack. Verify both `/healthz` endpoints report the selected version. Before rollback, restore the database backup unless the intervening migrations are known to be backward-compatible.

```bash
docker compose pull
docker compose up -d
```

`docker compose down` preserves the named volume. `docker compose down -v` permanently deletes it.

The existing direct Python/npm and systemd deployment remains supported.

## Deployment

See `docs/DEPLOY.md` for Docker startup, automatic backups, and restore instructions.
